#!/usr/bin/env python3
"""
AH Performance — Local Dev Server
Serves the app AND provides a shared JSON data store so laptop + phone stay in sync.

Usage:  python3 server.py
Then open:  http://<your-ip>:8000/AH-Performance-App.html
"""

import http.server
import json
import os

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ah-sync-data.json')

class AHHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/state':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r') as f:
                    self.wfile.write(f.read().encode())
            else:
                self.wfile.write(b'{}')
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == '/api/state':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                # Validate it's proper JSON
                data = json.loads(body)
                with open(DATA_FILE, 'w') as f:
                    json.dump(data, f)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"Invalid JSON"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = http.server.HTTPServer(('', 8000), AHHandler)
    print('\n  AH Performance Server running on port 8000')
    print('  Open on this computer:  http://localhost:8000/AH-Performance-App.html')
    print('  Open on your phone:     http://<your-ip>:8000/AH-Performance-App.html\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServer stopped.')
