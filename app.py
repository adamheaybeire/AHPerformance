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
import uuid
import time
import secrets
import hashlib
import glob as globmod
import shutil
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.utils import secure_filename

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
    """Save state, merging clients so no device can accidentally remove another device's clients."""
    global _state_cache
    try:
        data = request.get_json(force=True)
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

        _save_to_disk(data, 'save')
        return jsonify({'ok': True, 'clients': final_count})
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

# ─── API: Backup management ───

@app.route('/api/backups', methods=['GET'])
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
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters.'}), 400

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

    user['password'] = new_password
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
def serve_photo(filename):
    """Serve a saved progress photo."""
    safe = secure_filename(filename)
    filepath = os.path.join(PHOTO_DIR, safe)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Photo not found'}), 404
    return send_from_directory(PHOTO_DIR, safe)

@app.route('/api/photos', methods=['GET'])
def list_photos():
    """List photos for a given client (optional filter by clientId query param)."""
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
    return send_from_directory('.', filename)

# ─── Run ───

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    print(f'\n  AH Performance running on port {port}')
    print(f'  Open: http://localhost:{port}\n')
    app.run(host='0.0.0.0', port=port, debug=debug)
