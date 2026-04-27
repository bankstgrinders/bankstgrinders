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

HERE = Path(__file__).resolve().parent           # /path/to/website/tv
WEBSITE = HERE.parent                            # /path/to/website
MENU_PATH = HERE / 'data' / 'menu.json'
PLAYLIST_PATH = HERE / 'playlist.json'
SLIDES_DIR = HERE / 'slides'
BACKUP_DIR = HERE / 'data' / 'backups'

ALLOWED_UPLOAD_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm', '.mov', '.m4v'}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB per file

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
            if s['type'] in ('image', 'video', 'html') and not isinstance(s.get('src'), str):
                return jsonify({'ok': False, 'error': f'slide {i}: missing src'}), 400
            if s['type'] == 'text' and not isinstance(s.get('title'), str):
                return jsonify({'ok': False, 'error': f'slide {i}: text slide needs title'}), 400

        _backup(PLAYLIST_PATH, 'playlist')
        tmp = PLAYLIST_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(PLAYLIST_PATH)
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


@app.route('/api/slides', methods=['GET'])
@require_auth
def list_slides():
    SLIDES_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(SLIDES_DIR.iterdir()):
        if not p.is_file(): continue
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

    SLIDES_DIR.mkdir(parents=True, exist_ok=True)
    dest = SLIDES_DIR / name
    # Avoid clobbering existing files: foo.jpg -> foo-2.jpg, foo-3.jpg, ...
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        n = 2
        while True:
            candidate = SLIDES_DIR / f'{stem}-{n}{suffix}'
            if not candidate.exists():
                dest = candidate
                break
            n += 1

    f.save(str(dest))
    return jsonify({'ok': True, 'name': dest.name, 'src': f'slides/{dest.name}'})


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
    target.unlink()
    return jsonify({'ok': True})


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
