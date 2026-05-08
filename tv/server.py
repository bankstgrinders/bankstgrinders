"""
Bank St. Grinders TV menu server.

Serves:
  - The full website (/, /index.html, /tv/grinders.html, etc.) as static files
  - The menu data at GET /api/menu (public — display pages fetch this)
  - Menu editing at POST /api/menu (requires admin auth)
  - The admin UI at /tv/admin.html (gated with HTTP Basic auth)

Config via env vars (or defaults):
  BSG_ADMIN_USER   — admin username (default: admin)
  BSG_ADMIN_PASS   — admin password (default: changeme — CHANGE THIS)
  BSG_PORT         — listen port (default: 8080)
"""

from flask import Flask, request, jsonify, send_file, Response, abort
from pathlib import Path
from functools import wraps
from datetime import datetime
from werkzeug.utils import secure_filename
from contextlib import contextmanager
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent           # /path/to/website/tv
WEBSITE = HERE.parent                            # /path/to/website
MENU_PATH = HERE / 'data' / 'menu.json'
PLAYLIST_PATH = HERE / 'playlist.json'
SLIDES_DIR = HERE / 'slides'
BACKUP_DIR = HERE / 'data' / 'backups'

ALLOWED_UPLOAD_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm', '.mov', '.m4v'}
VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v'}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB per file
TRANSCODE_TIMEOUT_SECS = 600  # 10 min ceiling for an upload's transcode
FFMPEG_BIN = os.environ.get('BSG_FFMPEG', 'ffmpeg')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES

ADMIN_USER = os.environ.get('BSG_ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('BSG_ADMIN_PASS', 'changeme')


# ---------- AUTH ----------

def check_auth(auth):
    return auth is not None and auth.username == ADMIN_USER and auth.password == ADMIN_PASS


def auth_required():
    # Use a non-standard scheme name so browsers don't pop their native
    # HTTP Basic dialog when a fetch() returns 401. Both admin pages render
    # their own login form on 401 (clearAuth() + showLogin()).
    return Response(
        'Admin login required.', 401,
        {'WWW-Authenticate': 'BSGAdmin realm="bsg-admin"'}
    )


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not check_auth(request.authorization):
            return auth_required()
        return f(*args, **kwargs)
    return wrapped


# ---------- API ----------

@app.route('/api/menu', methods=['GET'])
def get_menu():
    if not MENU_PATH.exists():
        abort(404)
    resp = send_file(str(MENU_PATH), mimetype='application/json')
    # No-cache so display pages pick up edits immediately
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/menu', methods=['POST'])
@require_auth
def post_menu():
    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'expected JSON object'}), 400
        # Minimal structure check
        for key in ('info', 'sandwiches', 'breakfast', 'catering'):
            if key not in data:
                return jsonify({'ok': False, 'error': f'missing top-level key: {key}'}), 400

        _backup(MENU_PATH, 'menu')

        # Atomic write: write to .tmp then rename
        MENU_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MENU_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(MENU_PATH)
        return jsonify({'ok': True})
    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'invalid JSON: {e}'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ---------- PLAYLIST ----------

def _backup(path: Path, prefix: str, keep: int = 30):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        shutil.copy2(path, BACKUP_DIR / f'{prefix}-{stamp}{path.suffix}')
        backups = sorted(BACKUP_DIR.glob(f'{prefix}-*{path.suffix}'))
        for old in backups[:-keep]:
            try: old.unlink()
            except OSError: pass


@app.route('/api/playlist', methods=['GET'])
def get_playlist():
    if not PLAYLIST_PATH.exists():
        return jsonify({'slides': []})
    resp = send_file(str(PLAYLIST_PATH), mimetype='application/json')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


SLIDE_GC_GRACE_SECS = 24 * 3600
SLIDE_DELETE_MARKERS = SLIDES_DIR / '.delete-markers'


def _gc_orphan_slide_files(playlist_data: dict):
    """Trash uploaded slide files no longer referenced by the playlist.

    Mirrors the Pi #2 sync model: a file missing from the playlist on TWO
    consecutive saves more than 24h apart is moved to trash. This avoids
    racing user uploads — a file uploaded but not yet added to a slide
    won't be reaped until the next save 24h later, giving the user a
    grace window to finish their edit.

    Bundled .html slides are never touched.
    """
    referenced = set()
    for s in playlist_data.get('slides', []) or []:
        src = s.get('src', '')
        if isinstance(src, str) and src.startswith('slides/'):
            name = src.split('/', 1)[1]
            if name and '/' not in name and '..' not in name:
                referenced.add(name)

    try:
        SLIDE_DELETE_MARKERS.mkdir(parents=True, exist_ok=True)
    except OSError:
        return  # markers dir unavailable — skip GC silently

    now = datetime.now().timestamp()
    for p in SLIDES_DIR.iterdir():
        if not p.is_file(): continue
        if p.name.startswith('.'): continue       # skip the markers dir / dotfiles
        if p.suffix.lower() == '.html': continue  # never touch bundled slides
        marker = SLIDE_DELETE_MARKERS / p.name
        if p.name in referenced:
            # Cancel any pending deletion if the file is back in use
            if marker.exists():
                try: marker.unlink()
                except OSError: pass
            continue
        if not marker.exists():
            try: marker.touch()
            except OSError: pass
        elif now - marker.stat().st_mtime > SLIDE_GC_GRACE_SECS:
            if _trash_slide(p) is not None:
                try: marker.unlink()
                except OSError: pass


def _validate_slide_src(src: str, slide_index: int):
    """Make sure a slide's src points to a real file inside the tv/ folder.

    rotate.html resolves slide srcs relative to itself (tv/rotate.html), so
    'slides/breakfast.html' means tv/slides/breakfast.html on disk.

    Returns (ok, error_message). Rejects path traversal, absolute paths,
    and references to files that don't exist.
    """
    if not isinstance(src, str) or not src:
        return False, f'slide {slide_index}: missing src'
    if src.startswith('/') or '..' in src.replace('\\', '/').split('/'):
        return False, f'slide {slide_index}: src must be a relative path inside tv/'
    target = (HERE / src).resolve()
    try:
        target.relative_to(HERE.resolve())
    except ValueError:
        return False, f'slide {slide_index}: src escapes the tv/ folder'
    if not target.is_file():
        return False, f"slide {slide_index}: file '{src}' does not exist (was it uploaded yet?)"
    return True, None


@app.route('/api/playlist', methods=['POST'])
@require_auth
def post_playlist():
    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict) or not isinstance(data.get('slides'), list):
            return jsonify({'ok': False, 'error': 'expected { "slides": [...] }'}), 400
        # Validate each slide has the required shape
        for i, s in enumerate(data['slides']):
            if not isinstance(s, dict) or s.get('type') not in ('image', 'video', 'html', 'text'):
                return jsonify({'ok': False, 'error': f'slide {i}: bad type'}), 400
            if s['type'] in ('image', 'video', 'html'):
                ok, err = _validate_slide_src(s.get('src', ''), i)
                if not ok:
                    return jsonify({'ok': False, 'error': err}), 400
            if s['type'] == 'text' and not isinstance(s.get('title'), str):
                return jsonify({'ok': False, 'error': f'slide {i}: text slide needs title'}), 400

        _backup(PLAYLIST_PATH, 'playlist')
        tmp = PLAYLIST_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(PLAYLIST_PATH)
        # GC orphaned uploads — fail-quiet so a marker/disk hiccup never
        # turns a successful save into an error response.
        try: _gc_orphan_slide_files(data)
        except Exception: pass
        return jsonify({'ok': True})
    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'invalid JSON: {e}'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ---------- SLIDE FILE UPLOADS ----------

def _safe_slide_name(raw: str) -> str:
    """secure_filename + collision-safe; restricts to slides/ dir."""
    base = secure_filename(raw) or 'upload'
    # Replace any non [A-Za-z0-9._-] just in case
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
    return base


# Reject files smaller than this — a real photo/video is many KB, but a tiny
# stub crafted just to pass magic-byte detection isn't useful media.
MIN_UPLOAD_BYTES = 256

# Each entry is a list of options. An option is a list of (offset, bytes)
# pairs — ALL must match. Catches files with a faked extension (e.g.
# evil.exe renamed to evil.jpg) before they reach disk.
_MAGIC_SIGNATURES = {
    '.jpg':  [[(0, b'\xff\xd8\xff')]],
    '.jpeg': [[(0, b'\xff\xd8\xff')]],
    '.png':  [[(0, b'\x89PNG\r\n\x1a\n')]],
    '.gif':  [[(0, b'GIF87a')], [(0, b'GIF89a')]],
    '.webp': [[(0, b'RIFF'), (8, b'WEBP')]],
    # ISO BMFF (MP4/MOV/M4V): require 'ftyp' at offset 4 AND a recognized
    # brand at offset 8. Brand whitelist covers everything an iPad/iPhone
    # camera roll exports.
    '.mp4':  [[(4, b'ftyp'), (8, brand)] for brand in
              (b'isom', b'iso2', b'iso5', b'iso6', b'mp41', b'mp42', b'avc1', b'dash', b'M4V ')],
    '.m4v':  [[(4, b'ftyp'), (8, brand)] for brand in (b'M4V ', b'mp42', b'isom')],
    '.mov':  [[(4, b'ftyp'), (8, brand)] for brand in (b'qt  ', b'M4V ', b'mp42', b'isom')],
    # WebM uses EBML header (Matroska family)
    '.webm': [[(0, b'\x1a\x45\xdf\xa3')]],
}


def _file_matches_extension(file_storage, ext: str) -> bool:
    """Sniff the stream and compare against expected magic bytes for ext.

    Returns False on any IO error or empty/truncated stream so the caller
    can produce a clean 400 instead of leaking an HTTP 500. Rewinds the
    stream so the caller can still f.save() it afterward.
    """
    sigs = _MAGIC_SIGNATURES.get(ext.lower())
    if not sigs:
        # Extension not in our magic-byte table — refuse rather than fall
        # back to extension-only trust.
        return False
    try:
        stream = file_storage.stream
        head = stream.read(32)
        # Compute size by seeking to end, then rewind for f.save()
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
    except (OSError, ValueError, AttributeError):
        return False
    if size < MIN_UPLOAD_BYTES or len(head) < 12:
        return False
    for option in sigs:
        if all(head[off:off + len(b)] == b for off, b in option):
            return True
    return False


def _ffmpeg_available() -> bool:
    """Check if ffmpeg is installed and runnable.

    Cached for the process lifetime so we don't fork+exec on every upload.
    """
    if hasattr(_ffmpeg_available, '_result'):
        return _ffmpeg_available._result
    try:
        r = subprocess.run([FFMPEG_BIN, '-version'], capture_output=True, timeout=5)
        _ffmpeg_available._result = (r.returncode == 0)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _ffmpeg_available._result = False
    return _ffmpeg_available._result


def _transcode_video(src: Path, dest: Path) -> tuple[bool, str]:
    """Re-encode a video to 1080p H.264 ~5Mbps + AAC stereo for Pi 4 playback.

    Caps long-side at 1920px, preserving aspect ratio. Drops audio entirely
    (the rotating display is muted in kiosk mode anyway, so this saves CPU
    and disk). Returns (ok, message).
    """
    # Tuned for Pi 4: 'veryfast' keeps libx264 close to real-time on
    # software encode, so a 60s clip transcodes in ~30-60s instead of
    # several minutes. Slightly larger files at the same bitrate are an
    # acceptable tradeoff vs the kiosk being CPU-pinned.
    cmd = [
        FFMPEG_BIN, '-y',
        '-i', str(src),
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-profile:v', 'high',
        '-level', '4.0',
        '-pix_fmt', 'yuv420p',
        '-vf', "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
        '-b:v', '5M',
        '-maxrate', '6M',
        '-bufsize', '10M',
        '-movflags', '+faststart',
        '-an',  # drop audio — display is muted
        str(dest),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=TRANSCODE_TIMEOUT_SECS)
        if r.returncode != 0:
            # Last few lines of ffmpeg's stderr usually contain the actual reason
            tail = r.stderr.decode('utf-8', errors='replace').strip().splitlines()[-3:]
            return False, ' / '.join(tail) or 'ffmpeg failed'
        if not dest.exists() or dest.stat().st_size == 0:
            return False, 'ffmpeg produced no output'
        return True, 'ok'
    except subprocess.TimeoutExpired:
        return False, f'transcode took longer than {TRANSCODE_TIMEOUT_SECS}s — file too long?'
    except OSError as e:
        return False, f'ffmpeg error: {e}'


@app.route('/api/slides', methods=['GET'])
@require_auth
def list_slides():
    SLIDES_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(SLIDES_DIR.iterdir()):
        if not p.is_file(): continue
        if p.name.startswith('.'): continue  # hide dotfiles like .delete-markers
        if p.suffix.lower() == '.tmp': continue
        files.append({
            'name': p.name,
            'size': p.stat().st_size,
            'ext': p.suffix.lower(),
        })
    return jsonify({'files': files})


@app.route('/api/slides/upload', methods=['POST'])
@require_auth
def upload_slide():
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'no file field in upload'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'ok': False, 'error': 'empty filename'}), 400

    name = _safe_slide_name(f.filename)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        return jsonify({'ok': False, 'error': f'extension {ext} not allowed'}), 400

    # Verify file content actually matches the claimed extension.
    if not _file_matches_extension(f, ext):
        return jsonify({
            'ok': False,
            'error': f"file content doesn't look like a real {ext} — refusing to save"
        }), 400

    dest = None
    try:
        SLIDES_DIR.mkdir(parents=True, exist_ok=True)
        # Atomically reserve a unique filename. O_CREAT|O_EXCL fails with
        # FileExistsError if anyone else (or a parallel request) holds the
        # name, so two concurrent uploads of "promo.mov" can't clobber.
        stem, suffix = Path(name).stem, Path(name).suffix
        dest = SLIDES_DIR / name
        n = 1
        while True:
            try:
                fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                n += 1
                dest = SLIDES_DIR / f'{stem}-{n}{suffix}'
                if n > 1000:  # absurd ceiling; would mean ~1000 collisions
                    raise OSError('too many name collisions')
                continue
            try:
                os.close(fd)
            except OSError:
                # Close failed — try once more so the kernel definitely
                # releases the fd, then unlink the empty file so the name
                # isn't permanently consumed.
                try: os.close(fd)
                except OSError: pass
                try: dest.unlink()
                except OSError: pass
                raise
            break
        f.save(str(dest))
    except OSError as e:
        # On any save failure — including a short write or a mid-stream
        # I/O error that left a partial file — remove what we wrote so we
        # don't leave a corrupt or zero-byte slide behind that consumes
        # the name and shows up in /api/slides.
        if dest is not None and dest.exists():
            try: dest.unlink()
            except OSError: pass
        # Surface a clean JSON error instead of letting the exception
        # become an unhandled 500. Use 507 only for storage-exhaustion
        # cases so the UI doesn't tell uncle "disk full" for what is
        # actually a permissions / config problem.
        import errno as _errno
        is_disk_full = e.errno in (_errno.ENOSPC, _errno.EDQUOT, _errno.EFBIG)
        status = 507 if is_disk_full else 500
        msg = 'not enough space on the Pi to save this file' if is_disk_full else 'could not save the file (server problem)'
        app.logger.warning('upload save failed: %s', e)
        return jsonify({'ok': False, 'error': msg}), status

    transcoded = False
    transcode_note = None
    if ext in VIDEO_EXTS and _ffmpeg_available():
        # Re-encode to a Pi-friendly 1080p H.264 .mp4. Strategy:
        #  - Atomically allocate a unique final .mp4 path with O_EXCL (so
        #    two concurrent uploads of "promo.mov" can't both target
        #    "promo.mp4" and clobber each other).
        #  - Run ffmpeg into a guaranteed-unique temp file (mkstemp gives
        #    us a randomized name; no collision possible).
        #  - On success: rename temp over the placeholder, then unlink the
        #    original. On any failure: clean up temp + placeholder, leave
        #    the original alone.
        final_stem = dest.stem
        final_path = SLIDES_DIR / f'{final_stem}.mp4'
        n = 1
        while True:
            try:
                placeholder_fd = os.open(str(final_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if final_path == dest:
                    # The placeholder is the input itself — fine, ffmpeg
                    # will write to a temp and we'll replace dest later.
                    break
                n += 1
                final_path = SLIDES_DIR / f'{final_stem}-{n}.mp4'
                if n > 1000:
                    transcode_note = 'too many name collisions — could not optimize video, kept original'
                    break
                continue
            # Claimed; close the fd carefully so a close failure doesn't
            # leak the descriptor or the empty placeholder.
            try:
                os.close(placeholder_fd)
            except OSError as e:
                try: os.close(placeholder_fd)
                except OSError: pass
                try: final_path.unlink()
                except OSError: pass
                app.logger.warning('placeholder close failed during transcode setup: %s', e)
                transcode_note = 'could not optimize video, kept original'
            break

        if transcode_note is None:
            # mkstemp creates the temp atomically and returns a guaranteed
            # unique name — no race possible regardless of concurrent uploads.
            tmp_out = None
            tmp_fd = None
            tmp_str = None
            try:
                tmp_fd, tmp_str = tempfile.mkstemp(prefix='.transcoding-', suffix='.mp4', dir=str(SLIDES_DIR))
                os.close(tmp_fd)
                tmp_fd = None
                tmp_out = Path(tmp_str)  # ffmpeg -y will overwrite the empty file
            except OSError as e:
                transcode_note = 'could not optimize video, kept original'
                app.logger.warning('mkstemp/close failed during transcode setup: %s', e)
                # Clean up whatever survived
                if tmp_str:
                    try: os.unlink(tmp_str)
                    except OSError: pass
                if tmp_fd is not None:
                    try: os.close(tmp_fd)
                    except OSError: pass
                if final_path != dest:
                    try: final_path.unlink()
                    except OSError: pass

            if tmp_out is not None:
                ok, msg = _transcode_video(dest, tmp_out)
                if ok:
                    try:
                        # Atomically replace the placeholder with the real output.
                        tmp_out.replace(final_path)
                        # If the original was at a different path (e.g. .mov),
                        # remove it now. Original orphan is benign if unlink fails.
                        if dest != final_path and dest.exists():
                            try: dest.unlink()
                            except OSError: pass
                        dest = final_path
                        transcoded = True
                    except OSError as e:
                        app.logger.warning('transcode rename failed: %s', e)
                        try: tmp_out.unlink()
                        except OSError: pass
                        # Free the placeholder so it doesn't show up as a 0-byte file
                        if final_path != dest:
                            try: final_path.unlink()
                            except OSError: pass
                        transcode_note = 'could not optimize video, kept original'
                else:
                    app.logger.warning('ffmpeg transcode failed for %s: %s', dest.name, msg)
                    try: tmp_out.unlink()
                    except OSError: pass
                    if final_path != dest:
                        try: final_path.unlink()
                        except OSError: pass
                    transcode_note = 'could not optimize video, kept original'

    resp = {'ok': True, 'name': dest.name, 'src': f'slides/{dest.name}', 'transcoded': transcoded}
    if transcode_note:
        resp['warning'] = transcode_note
    return jsonify(resp)


SLIDE_TRASH_DIR = BACKUP_DIR / 'slides'
SLIDE_TRASH_KEEP = 30  # most-recent deletes kept around for recovery


def _trash_slide(target: Path):
    """Move a deleted slide file into data/backups/slides/<timestamp>-<name>.

    Mirrors the menu/playlist backup model: stay reversible by default, with
    a rolling cap so the SD card doesn't slowly fill from accidental deletes.
    Returns the trash path on success, None on failure (caller can decide
    whether to fall back to unlink).
    """
    try:
        SLIDE_TRASH_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        trashed = SLIDE_TRASH_DIR / f'{stamp}-{target.name}'
        # Avoid clobbering if two deletes happen in the same second
        if trashed.exists():
            n = 2
            while True:
                cand = SLIDE_TRASH_DIR / f'{stamp}-{n}-{target.name}'
                if not cand.exists():
                    trashed = cand
                    break
                n += 1
        shutil.move(str(target), str(trashed))
        # Roll: keep only the most recent SLIDE_TRASH_KEEP files
        all_trashed = sorted(SLIDE_TRASH_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
        for old in all_trashed[:-SLIDE_TRASH_KEEP]:
            try: old.unlink()
            except OSError: pass
        return trashed
    except OSError:
        return None


@app.route('/api/slides/<path:filename>', methods=['DELETE'])
@require_auth
def delete_slide(filename):
    name = _safe_slide_name(filename)
    target = (SLIDES_DIR / name).resolve()
    try:
        target.relative_to(SLIDES_DIR.resolve())
    except ValueError:
        abort(404)
    if not target.is_file():
        return jsonify({'ok': False, 'error': 'not found'}), 404
    # Don't allow deleting the bundled slide HTML files (they're part of the repo).
    if target.suffix.lower() == '.html':
        return jsonify({'ok': False, 'error': 'cannot delete bundled HTML slides'}), 400

    # Clear any stale GC marker so a future re-upload of the same name
    # doesn't get instantly trashed by inheriting an old grace clock.
    stale_marker = SLIDE_DELETE_MARKERS / target.name
    if stale_marker.exists():
        try: stale_marker.unlink()
        except OSError: pass

    trashed = _trash_slide(target)
    if trashed is None:
        # Trash directory unavailable (disk full, perms) — fall back to a
        # straight unlink so the user can still finish their delete.
        try:
            target.unlink()
        except OSError as e:
            app.logger.warning('slide delete failed: %s', e)
            return jsonify({'ok': False, 'error': 'could not delete the file (server problem)'}), 500
        return jsonify({'ok': True, 'recoverable': False})
    return jsonify({'ok': True, 'recoverable': True})


# ---------- SITE ADMIN (public website editing) ----------
#
# Edits the published website (index.html etc.) via marker-comment regions.
# Save commits locally on the Pi; a separate Publish action does `git push`,
# which Netlify auto-deploys. Splitting save and publish keeps "go live" as
# a deliberate second click.

INDEX_HTML_PATH = WEBSITE / 'index.html'
HOURS_DROP_START = '<!-- HOURS:DROPDOWN:START - generated by site-admin -->'
HOURS_DROP_END = '<!-- HOURS:DROPDOWN:END -->'
HOURS_JS_START = '// HOURS:JS:START - generated by site-admin'
HOURS_JS_END = '// HOURS:JS:END'
ANNOUNCE_START = '<!-- ANNOUNCEMENT:START - generated by site-admin -->'
ANNOUNCE_END = '<!-- ANNOUNCEMENT:END -->'
SITE_LOCK_PATH = HERE / 'data' / '.site-admin.lock'
DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
# JS Date.getDay(): 0=Sun, 1=Mon, ... 6=Sat.
JS_DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
ANNOUNCEMENT_MAX_LEN = 500

GIT_AUTHOR_NAME = os.environ.get('BSG_GIT_NAME', 'Bank St. Grinders Admin')
GIT_AUTHOR_EMAIL = os.environ.get('BSG_GIT_EMAIL', 'admin@bankstgrinders.com')


def _esc_html(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _esc_attr(s):
    return str(s).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


def _unesc_html(s):
    """Inverse of _esc_html. Order matters — handle &amp; last so other
    entities aren't double-decoded."""
    return (str(s)
            .replace('&quot;', '"')
            .replace('&gt;', '>')
            .replace('&lt;', '<')
            .replace('&amp;', '&'))


@contextmanager
def _site_admin_lock():
    """Serialize site-admin writes across requests so concurrent saves can't
    clobber each other's edits."""
    SITE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(SITE_LOCK_PATH, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try: fcntl.flock(f, fcntl.LOCK_UN)
        except OSError: pass
        f.close()


def _atomic_write(path: Path, content: str):
    """Write to a unique tmp file in the same dir, then rename. Avoids the
    fixed-name `.tmp` race where two requests stomp the same staging file.
    Preserves the original file's mode so os.replace doesn't drop us to
    mkstemp's restrictive 0600 perms."""
    fd, tmpname = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        if path.exists():
            shutil.copymode(str(path), tmpname)
        os.replace(tmpname, str(path))
    except Exception:
        try: os.unlink(tmpname)
        except OSError: pass
        raise


def _hours_block_re():
    return re.compile(
        re.escape(HOURS_DROP_START) + r'(.*?)' + re.escape(HOURS_DROP_END),
        re.DOTALL,
    )


def _hours_js_block_re():
    return re.compile(
        re.escape(HOURS_JS_START) + r'(.*?)' + re.escape(HOURS_JS_END),
        re.DOTALL,
    )


def _announce_block_re():
    return re.compile(
        re.escape(ANNOUNCE_START) + r'(.*?)' + re.escape(ANNOUNCE_END),
        re.DOTALL,
    )


def _format_expiry_display(iso_str):
    """ISO 'YYYY-MM-DD' -> 'Saturday, March 15'. Empty/invalid -> ''."""
    if not iso_str:
        return ''
    try:
        d = datetime.strptime(str(iso_str), '%Y-%m-%d').date()
    except ValueError:
        return ''
    return f'{d.strftime("%A, %B")} {d.day}'


def _read_announcement_from_index():
    if not INDEX_HTML_PATH.exists():
        return None
    html = INDEX_HTML_PATH.read_text(encoding='utf-8')
    m = _announce_block_re().search(html)
    if not m:
        return None
    inner = m.group(1)
    text_m = re.search(r'<div class="announcement-text">(.*?)</div>', inner, re.DOTALL)
    message = _unesc_html(text_m.group(1)) if text_m else ''
    expires_m = re.search(r'data-expires="([^"]*)"', inner)
    expires = expires_m.group(1) if expires_m else ''
    return {'message': message, 'expires': expires}


def _render_announcement_block(message, expires_iso):
    """Returns the HTML to put between the announcement markers. Empty
    message -> empty content (no .announcement div rendered, banner hidden)."""
    if not message:
        return '\n  '
    expires_iso = (expires_iso or '').strip()
    expiry_display = _format_expiry_display(expires_iso)
    expires_attr = f' data-expires="{_esc_attr(expires_iso)}"' if expires_iso else ''
    expiry_html = (
        f'\n    <div class="announcement-expiry">Through {_esc_html(expiry_display)}</div>'
        if expiry_display else ''
    )
    return (
        f'\n  <div class="announcement" id="announcement"{expires_attr}>\n'
        f'    <div class="announcement-label">Heads Up</div>\n'
        f'    <div class="announcement-text">{_esc_html(message)}</div>'
        f'{expiry_html}\n'
        f'  </div>\n  '
    )


def _read_hours_from_index():
    if not INDEX_HTML_PATH.exists():
        return None
    html = INDEX_HTML_PATH.read_text(encoding='utf-8')
    m = _hours_block_re().search(html)
    if not m:
        return None
    block = m.group(1)
    days = []
    for row in re.finditer(
        r'<div class="widget-hours-row(?:\s+closed-row)?">'
        r'<span>([^<]+)</span><span>([^<]+)</span></div>',
        block,
    ):
        name = row.group(1).strip()
        text = row.group(2).strip()
        if text.lower() == 'closed':
            days.append({'name': name, 'open': '', 'close': '', 'closed': True})
        else:
            parts = re.split(r'\s*[–—\-]\s*', text, maxsplit=1)
            if len(parts) == 2:
                days.append({'name': name, 'open': parts[0].strip(), 'close': parts[1].strip(), 'closed': False})
            else:
                days.append({'name': name, 'open': text, 'close': '', 'closed': False})
    return days


def _render_hours_dropdown_block(days):
    rows = []
    for d in days:
        name = _esc_html(d.get('name', ''))
        if d.get('closed'):
            rows.append(f'        <div class="widget-hours-row closed-row"><span>{name}</span><span>Closed</span></div>')
        else:
            o = _esc_html(d.get('open', '').strip())
            c = _esc_html(d.get('close', '').strip())
            text = f'{o} – {c}' if o and c else (o or c)
            rows.append(f'        <div class="widget-hours-row"><span>{name}</span><span>{text}</span></div>')
    return (
        '\n      <div class="widget-hours-dropdown" id="hoursDropdown">\n'
        + '\n'.join(rows)
        + '\n      </div>\n      '
    )


def _render_hours_js_block(days_mon_first):
    """Render the WEEKLY_HOURS array. Input order is Mon..Sun (display); JS
    array is Sun..Sat to match Date.getDay(). Open/close are minutes since
    midnight (or null for closed)."""
    by_name = {d['name']: d for d in days_mon_first}
    js_rows = []
    for js_idx, name in enumerate(JS_DAY_NAMES):
        d = by_name.get(name, {'closed': True})
        if d.get('closed') or d.get('open_min') is None:
            o, c = 'null', 'null'
        else:
            o, c = str(d['open_min']), str(d['close_min'])
        comma = ',' if js_idx < 6 else ''
        js_rows.append(f'      {{ open: {o:<5}, close: {c:<5} }}{comma} // {name}')
    return (
        '\n    const WEEKLY_HOURS = [\n'
        + '\n'.join(js_rows)
        + '\n    ];\n    '
    )


def _parse_12h_to_minutes(text):
    """'9:00 AM' -> 540, '6:30 PM' -> 1110, '12:00 PM' -> 720, '12:00 AM' -> 0.
    Returns minutes-since-midnight (0..1439) or None on parse failure.

    Returning minutes (instead of just the hour) is what lets half-hour
    times like '6:30 PM' actually drive the open/closed indicator on the
    homepage — otherwise the dropdown would say 6:30 but the indicator
    would behave as if it were 6:00."""
    if not isinstance(text, str):
        return None
    t = re.sub(r'\s+', '', text).upper()
    m = re.match(r'^(\d{1,2})(?::(\d{2}))?(AM|PM)$', t)
    if not m:
        return None
    h = int(m.group(1))
    minutes = int(m.group(2)) if m.group(2) else 0
    if h < 1 or h > 12 or minutes < 0 or minutes > 59:
        return None
    if h == 12:
        h = 0
    if m.group(3) == 'PM':
        h += 12
    return h * 60 + minutes


GIT_LOCAL_TIMEOUT = 30          # seconds — purely local ops (status, log, commit)
GIT_NETWORK_TIMEOUT = 120       # seconds — push/fetch can hit the network


def _git_env():
    env = os.environ.copy()
    env['GIT_AUTHOR_NAME'] = GIT_AUTHOR_NAME
    env['GIT_AUTHOR_EMAIL'] = GIT_AUTHOR_EMAIL
    env['GIT_COMMITTER_NAME'] = GIT_AUTHOR_NAME
    env['GIT_COMMITTER_EMAIL'] = GIT_AUTHOR_EMAIL
    # Stop git from prompting for credentials when its credential helper
    # comes up empty — without this, `git push` can hang an HTTP request
    # forever waiting on stdin that no one will ever type into.
    env['GIT_TERMINAL_PROMPT'] = '0'
    return env


def _git(*args, timeout=None):
    return subprocess.run(
        ['git', '-C', str(WEBSITE), *args],
        capture_output=True, text=True, check=True,
        timeout=timeout or GIT_LOCAL_TIMEOUT,
        env=_git_env(),
    )


def _is_path_dirty(rel_path: str) -> bool:
    """True if the given file has uncommitted changes vs HEAD."""
    return bool(_git('status', '--porcelain', rel_path).stdout.strip())


def _git_commit(paths, message):
    """Stage paths and commit. Returns 'committed' | 'no-changes'."""
    if not _git('status', '--porcelain', *paths).stdout.strip():
        return 'no-changes'
    _git('add', *paths)
    _git('commit', '-m', message)
    return 'committed'


def _git_unpushed_count():
    """Returns int (>=0) or None if there's no upstream tracking branch."""
    try:
        r = _git('rev-list', '--count', '@{u}..HEAD')
        return int(r.stdout.strip() or '0')
    except subprocess.CalledProcessError:
        return None


def _git_behind_count():
    """Returns int (>=0) or None if there's no upstream. Reflects whatever
    the local @{u} is — does NOT fetch first, so it only catches divergence
    that we already know about. Cheap; doesn't touch the network."""
    try:
        r = _git('rev-list', '--count', 'HEAD..@{u}')
        return int(r.stdout.strip() or '0')
    except subprocess.CalledProcessError:
        return None


def _git_last_commit():
    try:
        return _git('log', '-1', '--format=%h %s', 'HEAD').stdout.strip()
    except subprocess.CalledProcessError:
        return ''


def _site_state():
    """Common status payload — unpushed commits, publishability, last commit."""
    n = _git_unpushed_count()
    return {
        'unpublished': 0 if n is None else n,
        'publishable': n is not None,
        'lastCommit': _git_last_commit(),
    }


def _validate_hours(days):
    """Validates and normalizes 7-day hours payload. Returns (cleaned, error_str_or_None)."""
    if not isinstance(days, list) or len(days) != 7:
        return None, 'expected 7 days'
    cleaned = []
    for i, d in enumerate(days):
        if not isinstance(d, dict):
            return None, f'day {i} not an object'
        name = str(d.get('name') or DAY_NAMES[i]).strip() or DAY_NAMES[i]
        closed = bool(d.get('closed'))
        o = str(d.get('open') or '').strip()
        c = str(d.get('close') or '').strip()
        entry = {'name': name, 'open': o, 'close': c, 'closed': closed,
                 'open_min': None, 'close_min': None}
        if not closed:
            if not o or not c:
                return None, f'{name}: open and close times are required when the day is open'
            oh = _parse_12h_to_minutes(o)
            ch = _parse_12h_to_minutes(c)
            if oh is None:
                return None, f"{name}: open time must look like '9:00 AM' or '6:30 PM'"
            if ch is None:
                return None, f"{name}: close time must look like '9:00 AM' or '6:30 PM'"
            if oh >= ch:
                return None, f'{name}: open time must be before close time'
            entry['open_min'] = oh
            entry['close_min'] = ch
        cleaned.append(entry)
    return cleaned, None


def _validate_announcement(payload):
    """Returns ({message, expires}, error_str_or_None)."""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return None, 'announcement must be an object'
    message = str(payload.get('message') or '').strip()
    expires = str(payload.get('expires') or '').strip()
    if len(message) > ANNOUNCEMENT_MAX_LEN:
        return None, f'announcement is too long (max {ANNOUNCEMENT_MAX_LEN} characters)'
    if expires:
        # Require zero-padded canonical ISO so the value round-trips back into
        # <input type="date"> cleanly (the input only accepts YYYY-MM-DD).
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', expires):
            return None, "expiry date must be in YYYY-MM-DD format"
        try:
            parsed = datetime.strptime(expires, '%Y-%m-%d').date()
        except ValueError:
            return None, "expiry date is not a real calendar date"
        expires = parsed.isoformat()  # canonicalize, defensive
    if not message:
        # No message means the banner is cleared. Drop any expiry.
        expires = ''
    return {'message': message, 'expires': expires}, None


@app.route('/api/site/state', methods=['GET'])
@require_auth
def site_get_state():
    days = _read_hours_from_index()
    if days is None:
        return jsonify({'ok': False, 'error': 'Could not find hours markers in index.html'}), 500
    announcement = _read_announcement_from_index() or {'message': '', 'expires': ''}
    return jsonify({
        'ok': True,
        'hours': {'days': days},
        'announcement': announcement,
    })


@app.route('/api/site/save', methods=['POST'])
@require_auth
def site_save():
    try:
        data = request.get_json(force=True, silent=False) or {}

        # Validate inputs (cheap, do before taking the lock).
        cleaned_days, err = _validate_hours((data.get('hours') or {}).get('days'))
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        ann, err = _validate_announcement(data.get('announcement'))
        if err:
            return jsonify({'ok': False, 'error': err}), 400

        with _site_admin_lock():
            if not INDEX_HTML_PATH.exists():
                return jsonify({'ok': False, 'error': 'index.html not found'}), 500
            if _is_path_dirty('index.html'):
                return jsonify({'ok': False, 'error': 'index.html has uncommitted changes outside the admin. Resolve them on the Pi (or commit them) before saving.'}), 409

            html = INDEX_HTML_PATH.read_text(encoding='utf-8')
            for needed in (HOURS_DROP_START, HOURS_DROP_END, HOURS_JS_START, HOURS_JS_END,
                           ANNOUNCE_START, ANNOUNCE_END):
                if needed not in html:
                    return jsonify({'ok': False, 'error': f'marker missing from index.html: {needed}'}), 500

            new_html, n = _hours_block_re().subn(
                lambda _m: HOURS_DROP_START + _render_hours_dropdown_block(cleaned_days) + HOURS_DROP_END,
                html, count=1,
            )
            if n != 1:
                return jsonify({'ok': False, 'error': 'failed to update hours dropdown block'}), 500

            new_html, n = _hours_js_block_re().subn(
                lambda _m: HOURS_JS_START + _render_hours_js_block(cleaned_days) + HOURS_JS_END,
                new_html, count=1,
            )
            if n != 1:
                return jsonify({'ok': False, 'error': 'failed to update hours JS block'}), 500

            new_html, n = _announce_block_re().subn(
                lambda _m: ANNOUNCE_START + _render_announcement_block(ann['message'], ann['expires']) + ANNOUNCE_END,
                new_html, count=1,
            )
            if n != 1:
                return jsonify({'ok': False, 'error': 'failed to update announcement block'}), 500

            _atomic_write(INDEX_HTML_PATH, new_html)

            try:
                outcome = _git_commit(['index.html'], 'site-admin: update site content')
            except subprocess.CalledProcessError as e:
                return jsonify({'ok': False, 'error': f'git error: {(e.stderr or e.stdout or "").strip()}'}), 500

        return jsonify({
            'ok': True,
            'committed': outcome == 'committed',
            **_site_state(),
        })
    except json.JSONDecodeError as e:
        return jsonify({'ok': False, 'error': f'invalid JSON: {e}'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/site/status', methods=['GET'])
@require_auth
def site_status():
    return jsonify({'ok': True, **_site_state()})


@app.route('/api/site/publish', methods=['POST'])
@require_auth
def site_publish():
    try:
        if _git_unpushed_count() is None:
            return jsonify({'ok': False, 'error': 'No upstream branch on the Pi. Run tv/setup-github-auth.sh once to configure GitHub credentials.'}), 500

        # Refresh @{u} so the behind-count reflects the actual remote, not
        # whatever the Pi happened to last sync. Network op; bounded timeout.
        try:
            _git('fetch', '--quiet', 'origin', timeout=GIT_NETWORK_TIMEOUT)
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or e.stdout or '').strip() or 'git fetch failed'
            return jsonify({'ok': False, 'error': f'Could not check GitHub for updates: {msg}'}), 502
        except subprocess.TimeoutExpired:
            return jsonify({'ok': False, 'error': 'Timed out talking to GitHub. Try again in a minute.'}), 504

        behind = _git_behind_count()
        if behind and behind > 0:
            return jsonify({
                'ok': False,
                'error': f'Remote has {behind} new commit{"s" if behind != 1 else ""} that this Pi does not have. Pull/rebase on the Pi before publishing.',
            }), 409

        n = _git_unpushed_count()
        if n == 0:
            return jsonify({'ok': True, 'pushed': 0, 'message': 'already up to date', **_site_state()})

        push = _git('push', 'origin', 'HEAD', timeout=GIT_NETWORK_TIMEOUT)
        return jsonify({
            'ok': True,
            'pushed': n,
            'message': (push.stdout + push.stderr).strip(),
            **_site_state(),
        })
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or '').strip()
        return jsonify({'ok': False, 'error': msg or 'git push failed'}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'error': 'git push timed out. Network problem on the Pi?'}), 504


# ---------- STATIC FILES ----------

@app.route('/')
def root():
    return send_file(str(WEBSITE / 'index.html'))


@app.route('/<path:filename>')
def serve_static(filename):
    # All static files public. The admin UI itself has no secrets; all
    # data access + writes go through /api/menu which IS auth-gated.
    # This avoids the browser-native HTTP Basic prompt and lets admin.js
    # render its own login form.
    full = (WEBSITE / filename).resolve()
    try:
        full.relative_to(WEBSITE.resolve())
    except ValueError:
        abort(404)

    if not full.is_file():
        abort(404)

    return send_file(str(full))


# ---------- STARTUP ----------

if __name__ == '__main__':
    port = int(os.environ.get('BSG_PORT', '8080'))
    if ADMIN_PASS == 'changeme':
        print('⚠  ADMIN PASSWORD IS THE DEFAULT ("changeme"). Set BSG_ADMIN_PASS before deploying to the deli.')
    print(f'Bank St. Grinders TV server on http://0.0.0.0:{port}')
    print(f'  Menu admin:  http://0.0.0.0:{port}/tv/admin.html       (user: {ADMIN_USER})')
    print(f'  Site admin:  http://0.0.0.0:{port}/tv/site-admin.html  (user: {ADMIN_USER})')
    app.run(host='0.0.0.0', port=port, debug=False)
