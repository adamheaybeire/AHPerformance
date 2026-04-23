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

from flask import Flask, request, jsonify, send_from_directory, send_file
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__, static_folder='.', static_url_path='')

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

def _save_to_disk(data):
    """Save state to disk file."""
    global _state_cache
    try:
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

# ─── API: Shared data sync ───

@app.route('/api/state', methods=['GET'])
def get_state():
    """Return the current state. Uses in-memory cache if disk file is missing."""
    # Try disk first (may have been updated by another process)
    disk_state = None
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                disk_state = json.load(f)
    except Exception:
        pass

    # Use whichever has MORE clients (disk vs memory cache)
    global _state_cache
    best = None
    if disk_state and isinstance(disk_state, dict) and disk_state.get('savedAt'):
        if _state_cache and _count_clients(_state_cache) > _count_clients(disk_state):
            best = _state_cache  # Memory has richer data
        else:
            best = disk_state
            _state_cache = disk_state  # Update cache from disk
    elif _state_cache:
        best = _state_cache  # Disk is empty but memory has data — use memory

    if best:
        return json.dumps(best), 200, {'Content-Type': 'application/json'}
    return '{}', 200, {'Content-Type': 'application/json'}

@app.route('/api/state', methods=['POST'])
def save_state():
    """Save state, but NEVER allow fewer clients to overwrite more clients."""
    global _state_cache
    try:
        data = request.get_json(force=True)
        incoming_clients = _count_clients(data)
        current_clients = _count_clients(_state_cache) if _state_cache else 0

        # SAFETY: Never allow a save that would REDUCE the number of clients
        # unless the current state has 0 or 1 clients (initial/demo state)
        if current_clients > 1 and incoming_clients < current_clients:
            print(f'  BLOCKED save: incoming has {incoming_clients} clients, current has {current_clients}. Refusing to lose data.')
            return jsonify({
                'ok': False,
                'error': f'Blocked: would reduce clients from {current_clients} to {incoming_clients}',
                'currentClients': current_clients
            }), 409  # Conflict

        _save_to_disk(data)
        return jsonify({'ok': True, 'clients': incoming_clients})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/state/info', methods=['GET'])
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

# ─── API: Send email ───

@app.route('/api/send-email', methods=['POST'])
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
    return send_from_directory('.', filename)

# ─── Run ───

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    print(f'\n  AH Performance running on port {port}')
    print(f'  Open: http://localhost:{port}\n')
    app.run(host='0.0.0.0', port=port, debug=debug)
