#!/usr/bin/env python3
"""
AH Performance — Production Server
Deployable to Render.com (or any platform that runs Python).

Serves the app files AND provides a shared JSON data store.

Local:   python3 app.py
Deploy:  Push to GitHub → connect to Render → auto-deploys.

DATA PERSISTENCE:
  Option 1 (recommended): Set RENDER_DISK_PATH env var on Render and add a Disk.
  Option 2: Data file lives alongside app (ephemeral on free tier, wiped on deploy).
  The server keeps an in-memory copy and NEVER overwrites richer data with emptier data.
"""

from flask import Flask, request, jsonify, send_from_directory, send_file, g
import json
import os
import copy
import uuid
import time
import secrets
import hashlib
import glob as globmod
import shutil
import smtplib
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# SECURITY: static_folder=None — Flask's built-in static handler would serve
# EVERY file in this directory (server code, data files, spreadsheets).
# All static serving goes through the allowlisted static_files() route below.
app = Flask(__name__, static_folder=None)

# ─── Persistent storage path ───
# If RENDER_DISK_PATH is set (e.g. /var/data), use it — survives deploys.
# Otherwise fall back to app directory (ephemeral on Render free tier).
DISK_PATH = os.environ.get('RENDER_DISK_PATH', '')
if DISK_PATH and os.path.isdir(DISK_PATH):
    DATA_FILE = os.path.join(DISK_PATH, 'ah-sync-data.json')
    print(f'  Data persistence: Using Render Disk at {DATA_FILE}')
else:
    DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ah-sync-data.json')
    print(f'  Data persistence: Using local file at {DATA_FILE} (ephemeral — add a Render Disk for persistence)')

# ─── Backup system ───
# Rolling backups: every save creates a timestamped copy. Keep last 50.
MAX_BACKUPS = 50
if DISK_PATH and os.path.isdir(DISK_PATH):
    BACKUP_DIR = os.path.join(DISK_PATH, 'backups')
else:
    BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)
print(f'  Backup storage: {BACKUP_DIR}')

def _create_backup(data, reason='save'):
    """Create a timestamped backup before any state change."""
    try:
        ts = int(time.time() * 1000)
        client_count = len(data.get('clients', [])) if isinstance(data, dict) else 0
        filename = f'backup_{ts}_{reason}_c{client_count}.json'
        filepath = os.path.join(BACKUP_DIR, filename)
        with open(filepath, 'w') as f:
            json.dump(data, f)
        # Prune old backups — keep most recent MAX_BACKUPS
        backups = sorted(globmod.glob(os.path.join(BACKUP_DIR, 'backup_*.json')))
        while len(backups) > MAX_BACKUPS:
            os.remove(backups.pop(0))
        print(f'  BACKUP: {filename} ({client_count} clients)')
    except Exception as e:
        print(f'  Warning: Backup failed: {e}')

def _list_backups():
    """List available backups, newest first."""
    try:
        files = sorted(globmod.glob(os.path.join(BACKUP_DIR, 'backup_*.json')), reverse=True)
        result = []
        for f in files:
            fname = os.path.basename(f)
            size = os.path.getsize(f)
            result.append({'filename': fname, 'size': size, 'path': f})
        return result
    except Exception:
        return []

def _restore_backup(filename):
    """Restore state from a backup file."""
    global _state_cache
    filepath = os.path.join(BACKUP_DIR, secure_filename(filename))
    if not os.path.exists(filepath):
        return None, 'Backup file not found'
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get('savedAt'):
            return None, 'Invalid backup file'
        # Backup current state before restoring
        if _state_cache:
            _create_backup(_state_cache, 'pre-restore')
        _save_to_disk(data)
        return data, None
    except Exception as e:
        return None, str(e)

# ─── In-memory state cache ───
# Keeps the last-known-good state in memory so we never lose data between
# file writes, and can protect against writing emptier data over richer data.
_state_cache = None

def _load_from_disk():
    """Load state from disk file, return parsed dict or None."""
    global _state_cache
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                if data and isinstance(data, dict) and data.get('savedAt'):
                    _state_cache = data
                    return data
    except Exception as e:
        print(f'  Warning: Failed to load state from disk: {e}')
    return None

def _save_to_disk(data, backup_reason='save'):
    """Save state to disk file with automatic backup."""
    global _state_cache
    try:
        # ALWAYS backup the CURRENT state before overwriting
        if _state_cache and isinstance(_state_cache, dict) and _state_cache.get('savedAt'):
            _create_backup(_state_cache, backup_reason)
        os.makedirs(os.path.dirname(DATA_FILE) or '.', exist_ok=True)
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f)
        _state_cache = data
    except Exception as e:
        print(f'  Warning: Failed to save state to disk: {e}')
        # Still keep in memory even if disk write fails
        _state_cache = data

def _count_clients(state):
    """Count the number of clients in a state dict."""
    if not state or not isinstance(state, dict):
        return 0
    clients = state.get('clients', [])
    return len(clients) if isinstance(clients, list) else 0

# Load state from disk on startup
_load_from_disk()
if _state_cache:
    print(f'  Loaded {_count_clients(_state_cache)} clients from disk')
else:
    print(f'  No existing state file found — starting fresh')

# ─── Email config (set via Render environment variables) ───
SMTP_HOST = os.environ.get('SMTP_HOST', '')       # e.g. smtp-relay.brevo.com
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', '')        # e.g. your Brevo login
SMTP_PASS = os.environ.get('SMTP_PASS', '')        # e.g. your Brevo SMTP key
SMTP_FROM = os.environ.get('SMTP_FROM', 'adam@ahperformance.co.uk')
SMTP_FROM_NAME = os.environ.get('SMTP_FROM_NAME', 'AH Performance')

# ─── Web Push (VAPID) config ───
# SECURITY: keys MUST come from environment variables (Render → Environment).
# There are deliberately no fallback values — the old hardcoded keys were
# exposed in the public repo and have been rotated.
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_CLAIMS_EMAIL = os.environ.get('VAPID_CLAIMS_EMAIL', 'mailto:adam@ahperformance.co.uk')
if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
    print('  WARNING: VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY not set — push notifications disabled until configured.')

# Push subscriptions stored on disk alongside state
PUSH_SUBS_FILE = os.path.join(DISK_PATH, 'push_subscriptions.json') if DISK_PATH and os.path.isdir(DISK_PATH) else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'push_subscriptions.json')

def _load_push_subs():
    try:
        if os.path.exists(PUSH_SUBS_FILE):
            with open(PUSH_SUBS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}  # { clientId: [ {endpoint, keys: {p256dh, auth}} ] }

def _save_push_subs(subs):
    try:
        with open(PUSH_SUBS_FILE, 'w') as f:
            json.dump(subs, f)
    except Exception as e:
        print(f'Failed to save push subscriptions: {e}')

_push_subs = _load_push_subs()

# ═══════════════════════════════════════════════════════════════
#  AUTHENTICATION & SESSIONS
#  All /api/* routes (except login / password-reset / vapid key)
#  require a valid session. Sessions persist across restarts.
# ═══════════════════════════════════════════════════════════════

SESSIONS_FILE = os.path.join(DISK_PATH, 'sessions.json') if DISK_PATH and os.path.isdir(DISK_PATH) else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions.json')
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
SESSION_COOKIE = 'ah_session'

def _load_sessions():
    try:
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_sessions():
    try:
        with open(SESSIONS_FILE, 'w') as f:
            json.dump(_sessions, f)
    except Exception as e:
        print(f'  Warning: failed to persist sessions: {e}')

_sessions = _load_sessions()

def _prune_sessions():
    now = time.time()
    stale = [t for t, s in _sessions.items() if s.get('expires', 0) < now]
    for t in stale:
        del _sessions[t]
    if stale:
        _save_sessions()

# ── Login throttling (per email+IP, in-memory) ──
_login_fails = {}  # key -> {count, lockedUntil}
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 900  # 15 minutes

def _throttle_key():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()
    email = ''
    try:
        email = (request.get_json(force=True, silent=True) or {}).get('email', '')
    except Exception:
        pass
    return f'{ip}|{(email or "").lower()}'

def _is_locked_out():
    rec = _login_fails.get(_throttle_key())
    return bool(rec and rec.get('lockedUntil', 0) > time.time())

def _record_login_fail():
    k = _throttle_key()
    rec = _login_fails.setdefault(k, {'count': 0, 'lockedUntil': 0})
    rec['count'] += 1
    if rec['count'] >= LOGIN_MAX_FAILS:
        rec['lockedUntil'] = time.time() + LOGIN_LOCK_SECONDS
        rec['count'] = 0
        print(f'  AUTH: locked out {k.split("|")[0]} for {LOGIN_LOCK_SECONDS}s')

def _clear_login_fails():
    _login_fails.pop(_throttle_key(), None)

# ── Password storage ──
# Users carry 'passwordHash' (werkzeug). Legacy plaintext 'password' fields
# are migrated to hashes on startup and on every save. Plaintext is never
# stored and hashes are never sent to any browser.

COMPROMISED_DEFAULTS = {'coach2026'}  # old hardcoded password — force change on next login

def _ensure_password_hashes(state):
    """Migrate any plaintext passwords in state['users'] to hashes. Returns True if changed."""
    if not state or not isinstance(state, dict):
        return False
    changed = False
    for u in state.get('users', []):
        if not isinstance(u, dict):
            continue
        plain = u.pop('password', None)
        if plain is not None:
            changed = True
            existing = u.get('passwordHash')
            if not existing or not check_password_hash(existing, plain):
                u['passwordHash'] = generate_password_hash(plain)
            if plain in COMPROMISED_DEFAULTS:
                u['mustChangePassword'] = True
    return changed

def _strip_user_secrets(users):
    """Return copies of user dicts safe to send to a browser."""
    out = []
    for u in users or []:
        if not isinstance(u, dict):
            continue
        c = {k: v for k, v in u.items() if k not in ('passwordHash', 'password')}
        out.append(c)
    return out

def _best_state():
    """Current best-known state (disk vs memory), same rule as get_state."""
    global _state_cache
    disk_state = None
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                disk_state = json.load(f)
    except Exception:
        pass
    if disk_state and isinstance(disk_state, dict) and disk_state.get('savedAt'):
        if _state_cache and _count_clients(_state_cache) > _count_clients(disk_state):
            return _state_cache
        _state_cache = disk_state
        return disk_state
    return _state_cache

# Migrate plaintext passwords at startup
if _state_cache and _ensure_password_hashes(_state_cache):
    _save_to_disk(_state_cache, 'password-hash-migration')
    print('  AUTH: migrated plaintext passwords to hashes')

def _session_from_request():
    _prune_sessions()
    token = request.cookies.get(SESSION_COOKIE, '')
    if not token:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
    if not token:
        return None
    s = _sessions.get(token)
    if not s or s.get('expires', 0) < time.time():
        return None
    return s

def require_auth(role=None):
    """Decorator: require a valid session; optionally a specific role ('pt')."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            s = _session_from_request()
            if not s:
                return jsonify({'error': 'Not authenticated'}), 401
            if role and s.get('role') != role:
                return jsonify({'error': 'Not authorised'}), 403
            g.session = s
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def _set_session_cookie(resp, token):
    secure = request.headers.get('X-Forwarded-Proto', request.scheme) == 'https'
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
                    samesite='Lax', secure=secure, path='/')
    return resp

@app.route('/api/login', methods=['POST'])
def api_login():
    if _is_locked_out():
        return jsonify({'error': 'Too many failed attempts. Try again in 15 minutes.'}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password:
        return jsonify({'error': 'Email and password are required.'}), 400

    state = _best_state() or {}
    user = next((u for u in state.get('users', []) if isinstance(u, dict) and (u.get('email') or '').lower() == email), None)

    ok = False
    if user:
        if user.get('passwordHash'):
            ok = check_password_hash(user['passwordHash'], password)
        elif user.get('password'):  # legacy plaintext not yet migrated
            ok = secrets.compare_digest(str(user['password']), password)
            if ok:
                user['passwordHash'] = generate_password_hash(password)
                user.pop('password', None)
                if password in COMPROMISED_DEFAULTS:
                    user['mustChangePassword'] = True
                _save_to_disk(state, 'password-upgrade')
    if not ok:
        _record_login_fail()
        return jsonify({'error': 'Invalid email or password.'}), 401

    _clear_login_fails()
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        'email': email,
        'role': user.get('role', 'client'),
        'clientId': user.get('clientId'),
        'name': user.get('name', ''),
        'created': time.time(),
        'expires': time.time() + SESSION_TTL
    }
    _save_sessions()
    safe_user = _strip_user_secrets([user])[0]
    resp = jsonify({'ok': True, 'user': safe_user})
    return _set_session_cookie(resp, token)

@app.route('/api/logout', methods=['POST'])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE, '')
    if token and token in _sessions:
        del _sessions[token]
        _save_sessions()
    resp = jsonify({'ok': True})
    resp.delete_cookie(SESSION_COOKIE, path='/')
    return resp

@app.route('/api/me', methods=['GET'])
def api_me():
    s = _session_from_request()
    if not s:
        return jsonify({'error': 'Not authenticated'}), 401
    state = _best_state() or {}
    user = next((u for u in state.get('users', []) if isinstance(u, dict) and (u.get('email') or '').lower() == s['email']), None)
    if not user:
        return jsonify({'error': 'Account no longer exists'}), 401
    return jsonify({'ok': True, 'user': _strip_user_secrets([user])[0]})

@app.route('/api/change-password', methods=['POST'])
@require_auth()
def api_change_password():
    data = request.get_json(force=True, silent=True) or {}
    current = data.get('current') or ''
    new = data.get('password') or ''
    if len(new) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400
    state = _best_state() or {}
    user = next((u for u in state.get('users', []) if isinstance(u, dict) and (u.get('email') or '').lower() == g.session['email']), None)
    if not user:
        return jsonify({'error': 'Account not found.'}), 404
    # Require the current password unless this is a forced change of a compromised default
    if not user.get('mustChangePassword'):
        if not user.get('passwordHash') or not check_password_hash(user['passwordHash'], current):
            return jsonify({'error': 'Current password is incorrect.'}), 403
    user['passwordHash'] = generate_password_hash(new)
    user.pop('password', None)
    user.pop('mustChangePassword', None)
    _save_to_disk(state, 'password-change')
    return jsonify({'ok': True})

# ── Public new-client onboarding ──
# The landing-page onboarding form is used by people who don't have an
# account yet, so it can't go through /api/state. This endpoint creates
# the client + account server-side with strict rules:
#   • an email that already has a user OR client record is rejected
#     (prevents hijacking an existing client's data by knowing their email)
#   • the server assigns the client id
#   • the password is hashed, a session is issued (auto-login)
_onboard_hits = {}  # ip -> [timestamps]

@app.route('/api/onboard', methods=['POST'])
def api_onboard():
    # Rate limit: max 5 onboardings per IP per hour
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()
    now = time.time()
    hits = [t for t in _onboard_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 5:
        return jsonify({'error': 'Too many sign-ups from this connection. Try again later.'}), 429
    hits.append(now)
    _onboard_hits[ip] = hits

    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or '').strip()
    password = data.get('password') or ''
    client_rec = data.get('client') if isinstance(data.get('client'), dict) else {}
    submission = data.get('submission') if isinstance(data.get('submission'), dict) else None

    if not email or '@' not in email or not name:
        return jsonify({'error': 'Name and a valid email are required.'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400

    master = copy.deepcopy(_best_state() or {})
    master.setdefault('clients', [])
    master.setdefault('users', [])

    if any((u.get('email') or '').lower() == email for u in master['users'] if isinstance(u, dict)):
        return jsonify({'error': 'An account with this email already exists. Log in instead, or use "Forgot password".'}), 409
    if any((c.get('email') or '').lower() == email for c in master['clients'] if isinstance(c, dict)):
        return jsonify({'error': 'These details are already registered. Please contact your coach.'}), 409

    # Server assigns the id — never trust the client-side one
    new_id = max([c.get('id', 0) for c in master['clients'] if isinstance(c, dict)] + [0]) + 1
    client_rec['id'] = new_id
    client_rec['email'] = email
    client_rec.setdefault('name', name)
    client_rec.setdefault('status', 'new')
    master['clients'].append(client_rec)

    # Init per-client stores
    for f in ('workoutLog', 'checkinLog', 'clientNotifications'):
        master.setdefault(f, {})[str(new_id)] = []
    master.setdefault('progressData', {})[str(new_id)] = {'strength': [], 'cardio': [], 'measurements': [], 'wearable': [], 'weekly': []}

    if submission:
        submission.setdefault('id', 'ob' + str(int(now * 1000)))
        master.setdefault('onboardingSubmissions', []).append(submission)

    master['users'].append({
        'email': email, 'passwordHash': generate_password_hash(password),
        'role': 'client', 'name': name, 'clientId': new_id
    })

    master.setdefault('ptNotifications', []).insert(0, {
        'id': 'n' + format(int(now * 1000), 'x'),
        'type': 'programme', 'clientId': new_id, 'client': name,
        'message': 'New client onboarded via form — review their details',
        'time': 'Just now', 'read': False
    })

    master['savedAt'] = int(now * 1000)
    _save_to_disk(master, 'onboard')

    # Tell the coach (email + push), best-effort
    try:
        if SMTP_HOST and SMTP_USER:
            msg = MIMEMultipart('alternative')
            msg['From'] = f'{SMTP_FROM_NAME} <{SMTP_FROM}>'
            msg['To'] = 'adam@ahperformance.ie'
            msg['Subject'] = f'New Client Onboarded: {name}'
            msg.attach(MIMEText(f'Hi Adam,\n\n{name} has just completed their onboarding form.\n\nEmail: {email}\n\nLog in to review their details and assign a programme.\n\nAH Performance', 'plain'))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
    except Exception as e:
        print(f'  Onboard: coach email failed: {e}')

    # Auto-login the new client
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        'email': email, 'role': 'client', 'clientId': new_id,
        'name': name, 'created': now, 'expires': now + SESSION_TTL
    }
    _save_sessions()
    resp = jsonify({'ok': True, 'user': {'email': email, 'role': 'client', 'name': name, 'clientId': new_id}})
    return _set_session_cookie(resp, token)

# ═══════════════════════════════════════════════════════════════
#  PER-ROLE DATA ISOLATION
# ═══════════════════════════════════════════════════════════════

# Dict fields keyed by clientId
PER_CLIENT_DICT_FIELDS = [
    'workoutLog', 'checkinLog', 'clientProgrammes', 'programmeWeeks',
    'progressData', 'clientNotifications', 'cycleLog', 'clientNutrition',
    'foodLog', 'foodRecipes', 'neuroData', 'neuroEpisodes'
]
# List fields where each item carries a clientId
PER_CLIENT_LIST_FIELDS = ['wellbeingData', 'ptProgrammes']
# Fields a client must never receive
PT_ONLY_FIELDS = ['ptNotifications', 'emailQueue', 'onboardingSubmissions', '_deletedClientIds', '_deletedEmailIds']

def _cv_matches(cv, cid_s, client_name):
    if not isinstance(cv, dict):
        return False
    if str(cv.get('clientId')) == cid_s:
        return True
    return bool(client_name) and cv.get('name') == client_name

def _client_slice(state, cid, email):
    """Build the filtered state a client account is allowed to see."""
    cid_s = str(cid)
    own_client = next((c for c in state.get('clients', []) if isinstance(c, dict) and str(c.get('id')) == cid_s), None)
    client_name = own_client.get('name') if own_client else None
    out = {
        'clients': [own_client] if own_client else [],
        'users': _strip_user_secrets([u for u in state.get('users', []) if isinstance(u, dict) and (u.get('email') or '').lower() == email]),
        'savedAt': state.get('savedAt')
    }
    for f in PER_CLIENT_DICT_FIELDS:
        d = state.get(f)
        out[f] = {k: v for k, v in d.items() if str(k) == cid_s} if isinstance(d, dict) else {}
    for f in PER_CLIENT_LIST_FIELDS:
        lst = state.get(f)
        out[f] = [x for x in lst if isinstance(x, dict) and str(x.get('clientId')) == cid_s] if isinstance(lst, list) else []
    out['ptConversations'] = [cv for cv in state.get('ptConversations', []) if _cv_matches(cv, cid_s, client_name)]
    for f in PT_ONLY_FIELDS:
        out[f] = []
    return out

def _merge_log_arrays(cur_arr, inc_arr):
    """Server-side guard: never let a shorter incoming log wipe a longer one."""
    cur_len = len(cur_arr) if isinstance(cur_arr, list) else 0
    inc_len = len(inc_arr) if isinstance(inc_arr, list) else 0
    return cur_arr if inc_len < cur_len else inc_arr

# ── Conversation merging ──
# Chat threads sync as part of the state blob. If a device saves a thread
# copy that is even 30 seconds stale, wholesale replacement would UN-SEND
# the other side's newest messages. Messages are therefore unioned by
# identity (from|text|time) — no device can delete another device's message.

def _msg_key(m):
    return f"{m.get('from')}|{m.get('text')}|{m.get('time')}"

def _merge_thread_messages(cur_msgs, inc_msgs):
    cur = [m for m in (cur_msgs or []) if isinstance(m, dict)]
    out = list(cur)
    index = {_msg_key(m): i for i, m in enumerate(cur)}
    for m in (inc_msgs or []):
        if not isinstance(m, dict):
            continue
        k = _msg_key(m)
        if k in index:
            out[index[k]] = m  # incoming copy wins — carries read-state updates
        else:
            out.append(m)
    return out

def _refresh_thread_meta(cv):
    """Keep the preview in step with the merged message list."""
    msgs = cv.get('messages') or []
    if msgs and isinstance(msgs[-1], dict):
        text = msgs[-1].get('text') or ''
        cv['preview'] = text[:40] + ('...' if len(text) > 40 else '')
    return cv

def _merge_conversations(master_list, incoming_list, deleted_client_ids=None):
    """Merge incoming conversation threads into master, per-message.
    Master threads absent from incoming are kept (a stale device must not
    drop someone's thread) unless their client was explicitly deleted."""
    deleted = {str(x) for x in (deleted_client_ids or [])}
    merged = []
    used_master = set()
    master_list = [cv for cv in (master_list or []) if isinstance(cv, dict)]
    for inc in (incoming_list or []):
        if not isinstance(inc, dict):
            continue
        m_idx = next((i for i, cv in enumerate(master_list)
                      if (cv.get('clientId') is not None and cv.get('clientId') == inc.get('clientId'))
                      or (cv.get('name') and cv.get('name') == inc.get('name'))), None)
        if m_idx is not None:
            used_master.add(m_idx)
            base = dict(inc)  # incoming metadata (unread, time) wins
            base['messages'] = _merge_thread_messages(master_list[m_idx].get('messages'), inc.get('messages'))
            merged.append(_refresh_thread_meta(base))
        else:
            merged.append(_refresh_thread_meta(dict(inc)))
    for i, cv in enumerate(master_list):
        if i not in used_master and str(cv.get('clientId')) not in deleted:
            merged.append(cv)
    return merged

def _client_scoped_save(incoming, cid, email):
    """Merge ONLY a client's own records into the master state."""
    cid_s = str(cid)
    master = copy.deepcopy(_best_state() or {})
    if not master.get('clients'):
        master.setdefault('clients', [])

    # 1. Own client record (replace by id; a client cannot add or remove clients).
    #    COACH-OWNED FIELDS on the record are preserved from the master copy —
    #    a client device (or a stale sync) can never modify or wipe them.
    COACH_OWNED_CLIENT_FIELDS = ('coaching', 'notes')
    inc_client = next((c for c in incoming.get('clients', []) if isinstance(c, dict) and str(c.get('id')) == cid_s), None)
    if inc_client:
        idx = next((i for i, c in enumerate(master['clients']) if isinstance(c, dict) and str(c.get('id')) == cid_s), None)
        if idx is not None:
            for f in COACH_OWNED_CLIENT_FIELDS:
                if f in master['clients'][idx]:
                    inc_client[f] = master['clients'][idx][f]
                elif f in inc_client:
                    del inc_client[f]
            master['clients'][idx] = inc_client
    client_name = inc_client.get('name') if inc_client else None

    # 2. Own user record (name changes etc.) — server-side hash always wins
    inc_user = next((u for u in incoming.get('users', []) if isinstance(u, dict) and (u.get('email') or '').lower() == email), None)
    if inc_user:
        m_user = next((u for u in master.get('users', []) if isinstance(u, dict) and (u.get('email') or '').lower() == email), None)
        if m_user:
            preserved_hash = m_user.get('passwordHash')
            preserved_flag = m_user.get('mustChangePassword')
            for k, v in inc_user.items():
                if k in ('password', 'passwordHash', 'role', 'email', 'clientId', 'mustChangePassword'):
                    continue  # a client cannot change their own role/identity via sync
                m_user[k] = v
            if preserved_hash:
                m_user['passwordHash'] = preserved_hash
            if preserved_flag:
                m_user['mustChangePassword'] = preserved_flag

    # 3. Per-client dict fields — own key only, with log-shrink protection
    for f in PER_CLIENT_DICT_FIELDS:
        inc_d = incoming.get(f)
        if not isinstance(inc_d, dict):
            continue
        own = next((v for k, v in inc_d.items() if str(k) == cid_s), None)
        if own is None:
            continue
        master.setdefault(f, {})
        cur = master[f].get(cid_s, master[f].get(cid) if not isinstance(cid, str) else None)
        if isinstance(own, list) and isinstance(cur, list):
            merged = _merge_log_arrays(cur, own)
            if merged is cur:
                print(f'  SCOPED MERGE: kept existing {f}[{cid_s}] ({len(cur)} entries) over incoming ({len(own)})')
            master[f][cid_s] = merged
        else:
            master[f][cid_s] = own
        # normalise: drop a duplicate int key if present
        if cid_s != cid and cid in master[f]:
            del master[f][cid]

    # 4. Per-client list fields — replace own items, keep everyone else's
    for f in PER_CLIENT_LIST_FIELDS:
        inc_l = incoming.get(f)
        if not isinstance(inc_l, list):
            continue
        own_items = [x for x in inc_l if isinstance(x, dict) and str(x.get('clientId')) == cid_s]
        others = [x for x in (master.get(f) or []) if not (isinstance(x, dict) and str(x.get('clientId')) == cid_s)]
        # PROGRAMME PROTECTION: a stale athlete device must never drop the
        # coach's programme records for this client. Records with builderData
        # missing from the incoming slice are preserved (programmes are never
        # deleted in-app, so absence always means staleness).
        if f == 'ptProgrammes':
            cur_own = [x for x in (master.get(f) or [])
                       if isinstance(x, dict) and str(x.get('clientId')) == cid_s]
            inc_ids = {x.get('id') for x in own_items}
            preserved = [x for x in cur_own if x.get('builderData') and x.get('id') not in inc_ids]
            if preserved:
                print(f'  SCOPED MERGE: preserved {len(preserved)} ptProgrammes record(s) for client {cid_s} missing from incoming save')
            own_items = own_items + preserved
        master[f] = others + own_items

    # 5. Own conversation thread — merged per-message so a stale copy from
    #    this device can never un-send a message the coach just wrote
    inc_cvs = [cv for cv in incoming.get('ptConversations', []) if _cv_matches(cv, cid_s, client_name)]
    if inc_cvs:
        own_master = [cv for cv in (master.get('ptConversations') or []) if _cv_matches(cv, cid_s, client_name)]
        others = [cv for cv in (master.get('ptConversations') or []) if not _cv_matches(cv, cid_s, client_name)]
        master['ptConversations'] = others + _merge_conversations(own_master, inc_cvs)

    # 6. PT notifications & email queue — append-only (client actions notify the coach)
    for f in ('ptNotifications', 'emailQueue'):
        inc_l = incoming.get(f)
        if not isinstance(inc_l, list):
            continue
        master.setdefault(f, [])
        known = {x.get('id') for x in master[f] if isinstance(x, dict)}
        for x in inc_l:
            if isinstance(x, dict) and x.get('id') not in known:
                master[f].insert(0, x)

    master['savedAt'] = int(time.time() * 1000)
    _ensure_password_hashes(master)
    _save_to_disk(master, 'client-scoped-save')
    return master

# ─── API: Shared data sync ───

@app.route('/api/state', methods=['GET'])
@require_auth()
def get_state():
    """Return the current state — full for PT, own slice for clients."""
    best = _best_state()
    if not best:
        return '{}', 200, {'Content-Type': 'application/json'}

    if g.session.get('role') == 'client':
        sliced = _client_slice(best, g.session.get('clientId'), g.session['email'])
        return json.dumps(sliced), 200, {'Content-Type': 'application/json'}

    # PT: full state, but passwords/hashes never leave the server
    out = copy.deepcopy(best)
    out['users'] = _strip_user_secrets(out.get('users', []))
    return json.dumps(out), 200, {'Content-Type': 'application/json'}

@app.route('/api/state', methods=['POST'])
@require_auth()
def save_state():
    """Save state. PT: full merge. Client: scoped merge of own records only."""
    global _state_cache
    try:
        data = request.get_json(force=True)

        # ── Client role: strictly scoped save ──
        if g.session.get('role') == 'client':
            if g.session.get('clientId') is None:
                return jsonify({'error': 'No client profile linked to this account'}), 403
            merged = _client_scoped_save(data if isinstance(data, dict) else {},
                                         g.session['clientId'], g.session['email'])
            return jsonify({'ok': True, 'clients': len(merged.get('clients', []))})

        # ── PT role: full-state merge (existing protections) ──
        incoming_clients = data.get('clients', []) if isinstance(data, dict) else []
        current_clients = _state_cache.get('clients', []) if _state_cache and isinstance(_state_cache, dict) else []
        current_count = len(current_clients)

        # Respect intentional deletions — don't merge back deleted clients
        deleted_ids = set(data.get('_deletedClientIds', []))

        # MERGE: combine clients from both sides so no one gets lost
        if current_clients and incoming_clients is not None:
            incoming_ids = {c.get('id') for c in incoming_clients if isinstance(c, dict)}
            # Add any server-side clients missing from incoming data (unless intentionally deleted)
            for cc in current_clients:
                if isinstance(cc, dict) and cc.get('id') not in incoming_ids and cc.get('id') not in deleted_ids:
                    incoming_clients.append(cc)
                    print(f'  MERGE: preserved client {cc.get("name", "?")} (id={cc.get("id")}) from server')
            # Also merge users so new accounts aren't lost
            incoming_users = data.get('users', []) if isinstance(data, dict) else []
            current_users = _state_cache.get('users', []) if _state_cache and isinstance(_state_cache, dict) else []
            if current_users and incoming_users is not None:
                incoming_emails = {u.get('email') for u in incoming_users if isinstance(u, dict)}
                for cu in current_users:
                    if isinstance(cu, dict) and cu.get('email') not in incoming_emails:
                        incoming_users.append(cu)
                        print(f'  MERGE: preserved user {cu.get("email", "?")} from server')
                data['users'] = incoming_users
            data['clients'] = incoming_clients

        final_count = len(data.get('clients', []))

        # SAFETY GUARD: reject saves that drop more than 1 client without explicit deletion
        # This prevents accidental data wipes from bad syncs
        if current_count > 0 and final_count < current_count:
            dropped = current_count - final_count
            if dropped > len(deleted_ids):
                print(f'  BLOCKED SAVE: would drop {dropped} clients but only {len(deleted_ids)} explicitly deleted. Current: {current_count}, Incoming: {final_count}')
                # Still create a backup of what was attempted, for debugging
                _create_backup(data, 'blocked-save')
                return jsonify({
                    'ok': False,
                    'error': f'Save blocked: would lose {dropped} client(s) without explicit deletion',
                    'clients': current_count
                }), 409

        # Also merge onboarding submissions — NEVER lose an onboarding
        incoming_submissions = data.get('onboardingSubmissions', []) if isinstance(data, dict) else []
        current_submissions = _state_cache.get('onboardingSubmissions', []) if _state_cache and isinstance(_state_cache, dict) else []
        if current_submissions:
            incoming_sub_ids = {s.get('id') for s in incoming_submissions if isinstance(s, dict)}
            for cs in current_submissions:
                if isinstance(cs, dict) and cs.get('id') not in incoming_sub_ids:
                    incoming_submissions.append(cs)
                    print(f'  MERGE: preserved onboarding submission (id={cs.get("id")}) from server')
            data['onboardingSubmissions'] = incoming_submissions

        # LOG FIELD PROTECTION: never let a stale device's smaller per-client log
        # (workouts, check-ins, cycle log, nutrition, food log, neuro data) overwrite
        # a richer log already on the server. Per-client array merge by length.
        if _state_cache and isinstance(_state_cache, dict):
            for field in ('workoutLog', 'checkinLog', 'cycleLog', 'clientNutrition', 'foodLog', 'neuroData', 'neuroEpisodes'):
                current_field = _state_cache.get(field)
                if not isinstance(current_field, dict):
                    continue
                incoming_field = data.get(field) if isinstance(data, dict) else None
                if not isinstance(incoming_field, dict):
                    incoming_field = {}
                merged_field = dict(incoming_field)
                for k, cur_arr in current_field.items():
                    inc_arr = merged_field.get(k)
                    cur_len = len(cur_arr) if isinstance(cur_arr, list) else 0
                    inc_len = len(inc_arr) if isinstance(inc_arr, list) else 0
                    if inc_len < cur_len:
                        print(f'  MERGE: kept existing {field}[{k}] ({cur_len} entries) over incoming ({inc_len} entries)')
                        merged_field[k] = cur_arr
                data[field] = merged_field

        # ── CONVERSATION PROTECTION: merge chat threads per-message so a
        # stale coach device can't un-send a client's newest messages either.
        if _state_cache and isinstance(_state_cache, dict) and isinstance(data, dict):
            data['ptConversations'] = _merge_conversations(
                _state_cache.get('ptConversations'),
                data.get('ptConversations'),
                deleted_client_ids=data.get('_deletedClientIds'))

        # ── PROGRAMME PROTECTION: never let a stale device's ptProgrammes list
        # silently drop programme records the server already holds. Programmes
        # are never deleted in-app, so a record with builderData that is missing
        # from an incoming save means the device had a stale copy — keep it.
        if _state_cache and isinstance(_state_cache, dict) and isinstance(data, dict):
            cur_pp = _state_cache.get('ptProgrammes')
            if isinstance(cur_pp, list) and cur_pp:
                inc_pp = data.get('ptProgrammes')
                if not isinstance(inc_pp, list):
                    inc_pp = []
                inc_ids = {p.get('id') for p in inc_pp if isinstance(p, dict)}
                preserved = [p for p in cur_pp
                             if isinstance(p, dict) and p.get('builderData') and p.get('id') not in inc_ids]
                if preserved:
                    print(f'  MERGE: preserved {len(preserved)} ptProgrammes record(s) missing from incoming save')
                data['ptProgrammes'] = inc_pp + preserved

        # ── PASSWORD PRESERVATION: browsers never receive hashes, so re-attach
        # them here. A plaintext 'password' field (new account / password change)
        # is hashed; otherwise the existing server-side hash is kept.
        server_users = {(u.get('email') or '').lower(): u
                        for u in (_state_cache.get('users', []) if _state_cache and isinstance(_state_cache, dict) else [])
                        if isinstance(u, dict)}
        for u in data.get('users', []):
            if not isinstance(u, dict):
                continue
            existing = server_users.get((u.get('email') or '').lower())
            plain = u.pop('password', None)
            if plain is not None:
                if existing and existing.get('passwordHash') and check_password_hash(existing['passwordHash'], plain):
                    u['passwordHash'] = existing['passwordHash']  # unchanged — avoid rehash churn
                    if existing.get('mustChangePassword'):
                        u['mustChangePassword'] = True
                else:
                    u['passwordHash'] = generate_password_hash(plain)
                    if plain in COMPROMISED_DEFAULTS:
                        u['mustChangePassword'] = True
            elif existing:
                if existing.get('passwordHash'):
                    u['passwordHash'] = existing['passwordHash']
                if existing.get('mustChangePassword'):
                    u['mustChangePassword'] = True

        _save_to_disk(data, 'save')
        return jsonify({'ok': True, 'clients': final_count})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/state/info', methods=['GET'])
@require_auth('pt')
def state_info():
    """Quick endpoint to check how many clients the server has (for debugging)."""
    clients = _count_clients(_state_cache) if _state_cache else 0
    saved_at = _state_cache.get('savedAt') if _state_cache else None
    return jsonify({
        'clients': clients,
        'savedAt': saved_at,
        'diskPath': DATA_FILE,
        'persistent': bool(DISK_PATH and os.path.isdir(DISK_PATH))
    })

# ─── API: Backup management ───

@app.route('/api/backups', methods=['GET'])
@require_auth('pt')
def list_backups_api():
    """List all available backups with client counts."""
    backups = _list_backups()
    result = []
    for b in backups:
        # Parse filename: backup_{timestamp}_{reason}_c{count}.json
        fname = b['filename']
        parts = fname.replace('.json', '').split('_')
        ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        reason = parts[2] if len(parts) > 2 else '?'
        count_str = parts[-1] if parts[-1].startswith('c') else 'c0'
        client_count = int(count_str[1:]) if count_str[1:].isdigit() else 0
        result.append({
            'filename': fname,
            'timestamp': ts,
            'reason': reason,
            'clients': client_count,
            'size': b['size'],
            'date': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts / 1000)) if ts else '?'
        })
    return jsonify({'ok': True, 'backups': result, 'total': len(result)})

@app.route('/api/backups/restore', methods=['POST'])
@require_auth('pt')
def restore_backup_api():
    """Restore state from a specific backup file."""
    data = request.get_json(force=True)
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'error': 'Missing filename'}), 400
    restored, error = _restore_backup(filename)
    if error:
        return jsonify({'error': error}), 400
    client_count = len(restored.get('clients', []))
    print(f'  RESTORED from {filename} ({client_count} clients)')
    return jsonify({'ok': True, 'clients': client_count, 'restored_from': filename})

@app.route('/api/backups/<filename>', methods=['GET'])
@require_auth('pt')
def preview_backup(filename):
    """Preview a backup — shows clients and users without restoring."""
    safe = secure_filename(filename)
    filepath = os.path.join(BACKUP_DIR, safe)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Backup not found'}), 404
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        clients = [{'id': c.get('id'), 'name': c.get('name'), 'email': c.get('email')} for c in data.get('clients', []) if isinstance(c, dict)]
        users = [{'email': u.get('email'), 'role': u.get('role'), 'name': u.get('name')} for u in data.get('users', []) if isinstance(u, dict)]
        submissions = [{'id': s.get('id'), 'name': s.get('personal', {}).get('firstName', '') + ' ' + s.get('personal', {}).get('lastName', ''), 'date': s.get('date')} for s in data.get('onboardingSubmissions', []) if isinstance(s, dict)]
        return jsonify({
            'ok': True,
            'filename': safe,
            'clients': clients,
            'users': users,
            'onboardingSubmissions': submissions,
            'savedAt': data.get('savedAt')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── API: Send email ───

@app.route('/api/send-email', methods=['POST'])
@require_auth('pt')
def send_email():
    if not SMTP_HOST or not SMTP_USER:
        return jsonify({'error': 'Email not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASS in Render environment variables.'}), 503

    try:
        data = request.get_json(force=True)
        to_email = data.get('to', '')
        to_name = data.get('toName', '')
        subject = data.get('subject', 'AH Performance')
        body = data.get('body', '')

        if not to_email or not subject:
            return jsonify({'error': 'Missing to or subject'}), 400

        msg = MIMEMultipart('alternative')
        msg['From'] = f'{SMTP_FROM_NAME} <{SMTP_FROM}>'
        msg['To'] = f'{to_name} <{to_email}>' if to_name else to_email
        msg['Subject'] = subject
        msg['Reply-To'] = SMTP_FROM

        # Plain text version
        msg.attach(MIMEText(body, 'plain'))

        # Simple HTML version
        html_body = body.replace('\n', '<br>')
        html = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 32px 24px; color: #333;">
            <div style="text-align: center; margin-bottom: 24px;">
                <span style="display: inline-block; width: 40px; height: 40px; border: 2px solid #E8612D; border-radius: 10px; line-height: 40px; font-weight: 700; color: #E8612D; font-size: 16px;">AH</span>
            </div>
            <div style="font-size: 15px; line-height: 1.6;">{html_body}</div>
            <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
            <div style="font-size: 12px; color: #999; text-align: center;">AH Performance · Personal Training</div>
        </div>
        """
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        return jsonify({'ok': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Password Reset (secure token-based) ───

# In-memory token store: { token_hash: { email, expires } }
_reset_tokens = {}

@app.route('/api/request-password-reset', methods=['POST'])
def request_password_reset():
    """Generate a secure reset token, email a link to the user."""
    if not SMTP_HOST or not SMTP_USER:
        return jsonify({'error': 'Email not configured on server.'}), 503

    data = request.get_json(force=True)
    email = (data.get('email') or '').strip().lower()
    if not email:
        return jsonify({'error': 'Email is required.'}), 400

    # Check user exists in state
    state = _state_cache or {}
    users = state.get('users', [])
    user = next((u for u in users if (u.get('email') or '').lower() == email), None)
    # Always return success (don't reveal whether email exists)
    if not user:
        return jsonify({'ok': True, 'message': 'If that email is registered, a reset link has been sent.'})

    # Generate secure token (32 bytes = 64 hex chars)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Store with 30-minute expiry
    _reset_tokens[token_hash] = {
        'email': email,
        'expires': time.time() + 1800  # 30 minutes
    }

    # Clean up expired tokens
    now = time.time()
    expired = [k for k, v in _reset_tokens.items() if v['expires'] < now]
    for k in expired:
        del _reset_tokens[k]

    # Build reset URL — use the request host so it works on any domain
    base_url = request.host_url.rstrip('/')
    reset_url = f"{base_url}/AH-Performance-App.html?reset_token={token}"

    # Send email
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = f'{SMTP_FROM_NAME} <{SMTP_FROM}>'
        msg['To'] = email
        msg['Subject'] = 'Reset Your Password — AH Performance'
        msg['Reply-To'] = SMTP_FROM

        plain_body = f"Hi,\n\nYou requested a password reset for your AH Performance account.\n\nClick this link to reset your password:\n{reset_url}\n\nThis link expires in 30 minutes.\n\nIf you didn't request this, you can safely ignore this email.\n\n— AH Performance"
        msg.attach(MIMEText(plain_body, 'plain'))

        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 32px 24px; color: #333;">
            <div style="text-align: center; margin-bottom: 24px;">
                <span style="display: inline-block; width: 40px; height: 40px; border: 2px solid #E8612D; border-radius: 10px; line-height: 40px; font-weight: 700; color: #E8612D; font-size: 16px;">AH</span>
            </div>
            <h2 style="font-size: 18px; text-align: center; margin-bottom: 16px;">Reset Your Password</h2>
            <p style="font-size: 14px; line-height: 1.6;">You requested a password reset for your AH Performance account. Click the button below to set a new password:</p>
            <div style="text-align: center; margin: 28px 0;">
                <a href="{reset_url}" style="display: inline-block; background: #E8612D; color: white; padding: 14px 32px; border-radius: 10px; text-decoration: none; font-weight: 600; font-size: 14px;">Reset Password</a>
            </div>
            <p style="font-size: 12px; color: #888; text-align: center;">This link expires in 30 minutes. If you didn't request this, you can safely ignore this email.</p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
            <div style="font-size: 12px; color: #999; text-align: center;">AH Performance · Personal Training</div>
        </div>
        """
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

    except Exception as e:
        print(f'Password reset email failed: {e}')
        return jsonify({'error': 'Failed to send email. Try again later.'}), 500

    return jsonify({'ok': True, 'message': 'If that email is registered, a reset link has been sent.'})


@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    """Validate token and update password."""
    data = request.get_json(force=True)
    token = (data.get('token') or '').strip()
    new_password = data.get('password', '')

    if not token or not new_password:
        return jsonify({'error': 'Token and password are required.'}), 400
    if len(new_password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    record = _reset_tokens.get(token_hash)

    if not record:
        return jsonify({'error': 'Invalid or expired reset link. Please request a new one.'}), 400
    if time.time() > record['expires']:
        del _reset_tokens[token_hash]
        return jsonify({'error': 'This reset link has expired. Please request a new one.'}), 400

    email = record['email']

    # Update user password in state
    global _state_cache
    state = _state_cache or {}
    users = state.get('users', [])
    user = next((u for u in users if (u.get('email') or '').lower() == email), None)

    if not user:
        return jsonify({'error': 'Account not found.'}), 404

    user['passwordHash'] = generate_password_hash(new_password)
    user.pop('password', None)
    user.pop('mustChangePassword', None)
    _state_cache = state

    # Persist to disk
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f'Failed to persist password change: {e}')

    # Remove used token (one-time use)
    del _reset_tokens[token_hash]

    return jsonify({'ok': True, 'message': 'Password updated successfully.'})


# ─── Web Push Notifications ───

@app.route('/api/push/vapid-public-key', methods=['GET'])
def get_vapid_public_key():
    """Return the public VAPID key so clients can subscribe."""
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})


@app.route('/api/push/debug', methods=['GET'])
@require_auth('pt')
def push_debug():
    """Debug: show which client IDs have push subscriptions stored."""
    summary = {}
    for cid, subs in _push_subs.items():
        summary[cid] = len(subs)
    return jsonify({'subscriptions': summary, 'file': PUSH_SUBS_FILE, 'diag': _push_diag[-20:]})


_push_diag = []

@app.route('/api/push/diag', methods=['POST'])
@require_auth()
def push_diag():
    """Receive diagnostic messages from client-side push subscription."""
    data = request.get_json(force=True)
    msg = data.get('msg', '')
    ua = data.get('ua', '')[:100]
    _push_diag.append({'msg': msg, 'ua': ua, 'time': __import__('datetime').datetime.now().isoformat()})
    print(f'PUSH DIAG: {msg} | UA: {ua}')
    if len(_push_diag) > 50:
        _push_diag.pop(0)
    return jsonify({'ok': True})


@app.route('/api/push/subscribe', methods=['POST'])
@require_auth()
def push_subscribe():
    """Register a push subscription. Identity comes from the SESSION, not the request body."""
    global _push_subs
    data = request.get_json(force=True)
    subscription = data.get('subscription')

    # Session decides who this subscription belongs to (client id, or 'pt' for the coach)
    if g.session.get('role') == 'pt':
        client_id = 'pt'
    else:
        client_id = str(g.session.get('clientId', ''))

    if not client_id or not subscription or not subscription.get('endpoint'):
        return jsonify({'error': 'subscription required'}), 400

    if client_id not in _push_subs:
        _push_subs[client_id] = []

    # Avoid duplicates (same endpoint)
    existing_endpoints = [s['endpoint'] for s in _push_subs[client_id]]
    if subscription['endpoint'] not in existing_endpoints:
        _push_subs[client_id].append(subscription)
        _save_push_subs(_push_subs)

    return jsonify({'ok': True})


@app.route('/api/push/unsubscribe', methods=['POST'])
@require_auth()
def push_unsubscribe():
    """Remove a push subscription (own identity only)."""
    global _push_subs
    data = request.get_json(force=True)
    client_id = 'pt' if g.session.get('role') == 'pt' else str(g.session.get('clientId', ''))
    endpoint = data.get('endpoint', '')

    if client_id in _push_subs:
        _push_subs[client_id] = [s for s in _push_subs[client_id] if s['endpoint'] != endpoint]
        _save_push_subs(_push_subs)

    return jsonify({'ok': True})


@app.route('/api/push/send', methods=['POST'])
@require_auth()
def push_send():
    """Send a push notification. PT can target any client; a client can only notify the coach ('pt')."""
    data = request.get_json(force=True)
    client_id = str(data.get('clientId', ''))
    title = data.get('title', 'AH Performance')
    body = data.get('body', '')
    url = data.get('url', '/AH-Performance-App.html')
    tag = data.get('tag', 'ah-notification')

    if not client_id:
        return jsonify({'error': 'clientId required'}), 400
    if g.session.get('role') != 'pt' and client_id != 'pt':
        return jsonify({'error': 'Clients can only notify the coach'}), 403

    subs = _push_subs.get(client_id, [])
    print(f'Push send: clientId={client_id}, stored IDs={list(_push_subs.keys())}, subs_count={len(subs)}')
    if not subs:
        return jsonify({'ok': True, 'sent': 0, 'reason': 'No subscriptions for this client'})

    payload = json.dumps({
        'title': title,
        'body': body,
        'icon': '/icon-192.png',
        'badge': '/icon-192.png',
        'url': url,
        'tag': tag
    })

    sent = 0
    failed_endpoints = []

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        # Fallback: if pywebpush not installed, skip silently
        return jsonify({'ok': True, 'sent': 0, 'reason': 'pywebpush not installed on server'})

    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={'sub': VAPID_CLAIMS_EMAIL}
            )
            sent += 1
        except WebPushException as e:
            # 410 Gone or 404 = subscription expired, remove it
            if hasattr(e, 'response') and e.response and e.response.status_code in (404, 410):
                failed_endpoints.append(sub['endpoint'])
            else:
                print(f'Push failed for client {client_id}: {e}')
                failed_endpoints.append(sub['endpoint'])
        except Exception as e:
            print(f'Push error: {e}')

    # Clean up expired subscriptions
    if failed_endpoints:
        _push_subs[client_id] = [s for s in _push_subs[client_id] if s['endpoint'] not in failed_endpoints]
        _save_push_subs(_push_subs)

    return jsonify({'ok': True, 'sent': sent})


# ─── Photo uploads ───
# Store photos on Render persistent disk if available, else local directory.
if DISK_PATH and os.path.isdir(DISK_PATH):
    PHOTO_DIR = os.path.join(DISK_PATH, 'photos')
else:
    PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'photos')

os.makedirs(PHOTO_DIR, exist_ok=True)
print(f'  Photo storage: {PHOTO_DIR}')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'heic'}
MEDIA_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'heic', 'mp4', 'mov', 'webm', 'mp3', 'ogg', 'wav', 'm4a', 'aac'}
MAX_MEDIA_SIZE = 50 * 1024 * 1024  # 50 MB

if DISK_PATH and os.path.isdir(DISK_PATH):
    MEDIA_DIR = os.path.join(DISK_PATH, 'media')
else:
    MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'media')
os.makedirs(MEDIA_DIR, exist_ok=True)

def _allowed_media(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in MEDIA_EXTENSIONS
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB

def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/upload-photo', methods=['POST'])
@require_auth()
def upload_photo():
    """Accept a progress photo, save to persistent disk, return URL."""
    if 'photo' not in request.files:
        return jsonify({'error': 'No photo file provided'}), 400

    file = request.files['photo']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    if not _allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed. Use PNG, JPG, JPEG, or WEBP.'}), 400

    # Read file to check size
    file_data = file.read()
    if len(file_data) > MAX_PHOTO_SIZE:
        return jsonify({'error': 'File too large. Maximum 10 MB.'}), 400

    # Generate unique filename: clientId_pose_timestamp.ext
    # Clients can only upload photos under their OWN id — session wins over form data.
    if g.session.get('role') == 'client':
        client_id = str(g.session.get('clientId', 'unknown'))
    else:
        client_id = request.form.get('clientId', 'unknown')
    pose = request.form.get('pose', 'photo')  # front, side, back
    ext = file.filename.rsplit('.', 1)[1].lower()
    if ext == 'heic':
        ext = 'jpg'  # HEIC will be served as-is but named .jpg for compatibility
    timestamp = int(time.time() * 1000)
    unique_name = f"{client_id}_{pose}_{timestamp}.{ext}"
    safe_name = secure_filename(unique_name)

    filepath = os.path.join(PHOTO_DIR, safe_name)
    with open(filepath, 'wb') as f:
        f.write(file_data)

    photo_url = f"/api/photos/{safe_name}"
    print(f'  Photo saved: {safe_name} ({len(file_data)} bytes)')
    return jsonify({'ok': True, 'url': photo_url, 'filename': safe_name})

@app.route('/api/photos/<filename>', methods=['GET'])
@require_auth()
def serve_photo(filename):
    """Serve a saved progress photo — clients can only access their own."""
    safe = secure_filename(filename)
    if g.session.get('role') == 'client' and not safe.startswith(f"{g.session.get('clientId')}_"):
        return jsonify({'error': 'Not authorised'}), 403
    filepath = os.path.join(PHOTO_DIR, safe)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Photo not found'}), 404
    return send_from_directory(PHOTO_DIR, safe)

@app.route('/api/photos', methods=['GET'])
@require_auth()
def list_photos():
    """List photos for a given client (clients are locked to their own)."""
    if g.session.get('role') == 'client':
        client_id = str(g.session.get('clientId', ''))
    else:
        client_id = request.args.get('clientId', '')
    try:
        all_files = os.listdir(PHOTO_DIR)
        if client_id:
            files = [f for f in all_files if f.startswith(f"{client_id}_")]
        else:
            files = all_files
        # Sort newest first
        files.sort(reverse=True)
        photos = [{'filename': f, 'url': f'/api/photos/{f}'} for f in files if _allowed_file(f)]
        return jsonify({'ok': True, 'photos': photos})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Media uploads (chat: photo, video, voice) ───

@app.route('/api/upload-media', methods=['POST'])
@require_auth()
def upload_media():
    """Accept photo/video/audio for chat messages."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400
    if not _allowed_media(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400
    file_data = file.read()
    if len(file_data) > MAX_MEDIA_SIZE:
        return jsonify({'error': 'File too large. Maximum 50 MB.'}), 400
    media_type = request.form.get('type', 'photo')
    sender = request.form.get('sender', 'unknown')
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'bin'
    timestamp = int(time.time() * 1000)
    safe_name = secure_filename(f"{media_type}_{sender}_{timestamp}.{ext}")
    filepath = os.path.join(MEDIA_DIR, safe_name)
    with open(filepath, 'wb') as f:
        f.write(file_data)
    url = f'/api/media/{safe_name}'
    print(f'  Media saved: {safe_name} ({len(file_data)} bytes)')
    return jsonify({'ok': True, 'url': url, 'filename': safe_name, 'type': media_type})

@app.route('/api/media/<filename>', methods=['GET'])
@require_auth()
def serve_media(filename):
    safe = secure_filename(filename)
    filepath = os.path.join(MEDIA_DIR, safe)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Media not found'}), 404
    return send_from_directory(MEDIA_DIR, safe)

# ─── Serve the app ───

@app.route('/')
def index():
    return send_file('AH-Performance-App.html')

@app.route('/manifest.json')
def manifest():
    return send_file('manifest.json')

@app.route('/sw.js')
def service_worker():
    return send_file('sw.js', mimetype='application/javascript')

@app.route('/<path:filename>')
def static_files(filename):
    # SECURITY: only serve genuine front-end assets. Data files, server code,
    # spreadsheets and backups must never be reachable over HTTP.
    lower = filename.lower()
    blocked_names = {'ah-sync-data.json', 'push_subscriptions.json', 'sessions.json',
                     'requirements.txt', 'render.yaml', 'deploy-guide.md'}
    blocked_prefixes = ('backups/', 'photos/', 'media/', '.', '__pycache__/', 'deploy-to-github/')
    allowed_ext = ('.html', '.htm', '.css', '.js', '.png', '.jpg', '.jpeg', '.webp',
                   '.svg', '.ico', '.pdf', '.woff', '.woff2')
    if (os.path.basename(lower) in blocked_names
            or lower.startswith(blocked_prefixes)
            or lower.endswith(('.py', '.pyc', '.xlsx', '.json', '.md', '.docx', '.tmp'))
            or not lower.endswith(allowed_ext)):
        return jsonify({'error': 'Not found'}), 404
    return send_from_directory('.', filename)

# ─── Run ───

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    print(f'\n  AH Performance running on port {port}')
    print(f'  Open: http://localhost:{port}\n')
    app.run(host='0.0.0.0', port=port, debug=debug)
