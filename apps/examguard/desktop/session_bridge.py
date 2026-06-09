import json
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CHROME_EXTENSION_ORIGIN = re.compile(r'^chrome-extension://[a-z]{32}$')


class PairingBridge:
    def __init__(self, host='127.0.0.1', port=17843):
        self.host = host
        self.port = port
        self._payload = None
        self._lock = threading.Lock()
        self._server = None

    def update(self, pairing_code, exam_id, expires_at):
        try:
            expires_at_epoch = datetime.fromisoformat(
                str(expires_at).replace('Z', '+00:00')
            ).timestamp()
        except (TypeError, ValueError):
            expires_at_epoch = time.time() + 120
        with self._lock:
            self._payload = {
                'pairingCode': pairing_code,
                'examId': exam_id,
                'expiresAt': expires_at,
                'expiresAtEpoch': expires_at_epoch,
            }

    def clear(self):
        with self._lock:
            self._payload = None

    def current_payload(self):
        with self._lock:
            payload = dict(self._payload) if self._payload else None
        if not payload:
            return None
        if payload.get('expiresAtEpoch', float('inf')) <= time.time():
            self.clear()
            return None
        payload.pop('expiresAtEpoch', None)
        return payload

    def serve_forever(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != '/session':
                    self.send_error(404)
                    return

                origin = self.headers.get('Origin', '')
                if not CHROME_EXTENSION_ORIGIN.fullmatch(origin):
                    self.send_error(403)
                    return

                payload = bridge.current_payload()
                if not payload:
                    self.send_response(204)
                    self._send_common_headers(origin)
                    self.end_headers()
                    return

                body = json.dumps(payload).encode('utf-8')
                self.send_response(200)
                self._send_common_headers(origin)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                origin = self.headers.get('Origin', '')
                if not CHROME_EXTENSION_ORIGIN.fullmatch(origin):
                    self.send_error(403)
                    return
                self.send_response(204)
                self._send_common_headers(origin)
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.end_headers()

            def _send_common_headers(self, origin):
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')

            def log_message(self, _format, *_args):
                return

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
            self._server.serve_forever()
        except OSError as exc:
            print(f'[Agent] Eklenti köprüsü başlatılamadı: {exc}')

    def stop(self):
        if self._server:
            self._server.shutdown()
