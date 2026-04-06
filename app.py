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

app = Flask(__name__, static_folder='.', static_url_path='')

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ah-sync-data.json')

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
