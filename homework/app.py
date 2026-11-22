from flask import Flask, Response, render_template, jsonify, request, session, redirect, url_for, stream_with_context
import base64
import hashlib
import json
import logging
import os
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from mva_canvas.errors import CanvasAuthError, CanvasError
from homework_data import SessionCache
from tutor import Tutor, TutorError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

APP_VERSION = os.environ.get('APP_VERSION', 'dev')

app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.environ.get('REMEMBER_DAYS', '365'))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'),
)

# The password is kept in the (signed) session cookie so the login survives
# container restarts; encrypt it so the cookie alone never reveals it.
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(app.secret_key.encode()).digest()))

cache = SessionCache()

SAVED_LOGIN_REJECTED = ("MVA didn't accept your saved login. Try refreshing; if your MVA password "
                        "has changed, log out and sign in again.")

# The homework helper chat. It's on when the claude-broker folder is mounted at
# BROKER_DIR (see README). tutor.start() serves its tools: gunicorn calls it after
# forking (gunicorn.conf.py), and the dev server calls it below.
tutor = Tutor(lambda: cache)

logger.info(f"Homework app v{APP_VERSION} initialized")


def _encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode()


def _decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()


def current_credentials():
    """(username, password) from the session, or None if not logged in."""
    username = session.get('username')
    token = session.get('password')
    if not username or not token:
        return None
    try:
        return username, _decrypt(token)
    except InvalidToken:
        return None


def login_required(f):
    """Decorator to require authentication for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_credentials() is None:
            session.clear()
            return redirect(url_for('login', next=request.full_path if request.query_string else request.path))
        return f(*args, **kwargs)
    return decorated_function


def _safe_next(target):
    """Only follow relative redirects back into this site."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith('/'):
        return None
    return target


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page: credentials are checked against MVA Canvas."""
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if not username or not password:
            return render_template('login.html', error='Enter your MVA username and password', username=username)
        try:
            cache.login(username, password)
        except CanvasAuthError as e:
            logger.info("MVA login rejected: %s", e)
            return render_template('login.html', error='MVA rejected the username or password', username=username)
        except (CanvasError, OSError) as e:
            logger.warning("MVA login failed: %s", e)
            return render_template('login.html', error='Could not reach MVA. Please try again shortly.', username=username)

        session.clear()
        session.permanent = True
        session['username'] = username
        session['password'] = _encrypt(password)
        return redirect(_safe_next(request.args.get('next')) or url_for('index'))

    if current_credentials() is not None:
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout and clear session"""
    username = session.get('username')
    if username:
        cache.forget(username)
        tutor.chats.forget_user(username)
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    """This week's homework, with overdue and upcoming work."""
    username, password = current_credentials()
    try:
        report = cache.report(username, password, refresh=bool(request.args.get('refresh')))
    except CanvasAuthError:
        # Keep the saved login: MVA also refuses logins while it's down or under
        # maintenance. Only Logout removes it, so a changed password is fixed there.
        cache.forget(username)
        return render_template('index.html', report=None, error=SAVED_LOGIN_REJECTED), 502
    except (CanvasError, OSError) as e:
        logger.warning("Fetching homework failed: %s", e)
        return render_template('index.html', report=None, error='Could not load homework from MVA. Try refreshing.'), 502
    if request.args.get('refresh'):
        return redirect(url_for('index'))
    return render_template('index.html', report=report, error=None, tutor_enabled=tutor.enabled)


@app.route('/api/ask', methods=['POST'])
def ask():
    """Ask the homework helper about an item. Streams NDJSON events:
    {"chat_id"}, then {"status"} / {"delta"} / {"reset"}, then {"done", "resources"} or {"error"}."""
    creds = current_credentials()
    if creds is None:
        return jsonify({'error': 'Please sign in again.'}), 401
    # JSON only: a cross-site form can't send it, so this also guards against CSRF.
    if not request.is_json:
        return jsonify({'error': 'Expected JSON'}), 415
    if not tutor.enabled:
        return jsonify({'error': "The homework helper isn't switched on."}), 503
    body = request.get_json(silent=True) or {}
    question = str(body.get('question') or '')
    username, password = creds
    try:
        chat = tutor.chats.get(str(body.get('chat_id') or ''), username) if body.get('chat_id') else None
        if chat is None:
            if not body.get('item_key'):
                return jsonify({'error': 'That chat has expired. Close it and press Ask again.'}), 404
            chat = tutor.open_chat(username, password, str(body['item_key']))
    except TutorError as e:
        return jsonify({'error': str(e)}), e.status
    except CanvasAuthError:
        cache.forget(username)
        return jsonify({'error': SAVED_LOGIN_REJECTED}), 502
    except (CanvasError, OSError) as e:
        logger.warning("Opening a chat failed: %s", e)
        return jsonify({'error': 'Could not load homework from MVA. Try again.'}), 502

    def events():
        yield json.dumps({'chat_id': chat.id}) + '\n'
        for event in tutor.ask(chat, question):
            yield json.dumps(event) + '\n'

    return Response(stream_with_context(events()), mimetype='application/x-ndjson',
                    headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'version': APP_VERSION})


if __name__ == '__main__':
    # For development only - use gunicorn in production
    tutor.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
