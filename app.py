#!/usr/bin/env python3
"""
AH Performance — Production Server
Deployable to Render.com (or any platform that runs Python).

Serves the app files AND provides a shared JSON data store.

Local:   python3 app.py
Deploy:  Push to GitHub → connect to Render → auto-deploys.
"""

from flask import Flask, request, jsonify, send_from_directory, send_file
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__, static_folder='.', static_url_path='')

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ah-sync-data.json')

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
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return f.read(), 200, {'Content-Type': 'application/json'}
    return '{}', 200, {'Content-Type': 'application/json'}

@app.route('/api/state', methods=['POST'])
def save_state():
    try:
        data = request.get_json(force=True)
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

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
