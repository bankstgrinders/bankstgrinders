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
    return Response(
        'Admin login required.', 401,
        {'WWW-Authenticate': 'Basic realm="Bank St. Grinders Admin"'}
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
    print(f'  Admin:  http://0.0.0.0:{port}/tv/admin.html  (user: {ADMIN_USER})')
    app.run(host='0.0.0.0', port=port, debug=False)
