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
import json
import os
import shutil

HERE = Path(__file__).resolve().parent           # /path/to/website/tv
WEBSITE = HERE.parent                            # /path/to/website
MENU_PATH = HERE / 'data' / 'menu.json'
BACKUP_DIR = HERE / 'data' / 'backups'

ADMIN_USER = os.environ.get('BSG_ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('BSG_ADMIN_PASS', 'changeme')

app = Flask(__name__)


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

        # Backup existing file
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if MENU_PATH.exists():
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            shutil.copy2(MENU_PATH, BACKUP_DIR / f'menu-{stamp}.json')
            # Keep only the 30 most-recent backups
            backups = sorted(BACKUP_DIR.glob('menu-*.json'))
            for old in backups[:-30]:
                try: old.unlink()
                except OSError: pass

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
