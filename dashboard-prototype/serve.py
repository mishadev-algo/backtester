"""Loopback-only prototype, fixed read-only SQL over an existing SSH session.

No credentials, raw database snapshots, or remote changes are persisted.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import argparse
import json
import os
import subprocess
import threading
import time
import logs_api

ROOT = Path(__file__).resolve().parent
SOCKET = os.environ.get('ATLAS_SSH_SOCKET', '/private/tmp/atlas-vps-readonly.sock')
COMMAND = ['ssh', '-S', SOCKET, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
           '-o', 'StrictHostKeyChecking=yes', 'root@46.225.132.195',
           'sudo -u postgres psql -X --set=ON_ERROR_STOP=1 -qAt -d dashboard']
FILES = {'/logs.js': ('logs.js', 'text/javascript'), '/mapping-model.js': ('mapping-model.js', 'text/javascript'), '/mapping-ui.js': ('mapping-ui.js', 'text/javascript'), '/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
         '/journey.js': ('journey.js', 'text/javascript'),
         '/journey.css': ('journey.css', 'text/css'),
         '/real.js': ('real.js', 'text/javascript'), '/real.css': ('real.css', 'text/css')}


class Snapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.loaded = 0

    def get(self, refresh=False):
        with self.lock:
            if self.data is not None and (not refresh or time.monotonic() - self.loaded < 10):
                return self.data
            # ControlMaster connection must already exist. Never request a password here.
            result = subprocess.run(COMMAND, input=(ROOT / 'read_snapshot.sql').read_text(),
                                    capture_output=True, text=True, timeout=25)
            if result.returncode:
                raise RuntimeError('SSH недоступен. Восстановите локальное SSH-соединение и повторите обновление.')
            data = json.loads(result.stdout)
            for field in ('accounts', 'deals', 'positions', 'backtests', 'backtest_days', 'hosts', 'terminal_status'):
                if not isinstance(data.get(field), list):
                    raise ValueError('Unexpected snapshot schema')
            self.data, self.loaded = data, time.monotonic()
            return data


snapshot = Snapshot()


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, mime='application/json'):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        origin = self.headers.get('Origin')
        if host not in allowed or (origin and origin not in {'http://' + x for x in allowed}):
            return self.respond(403, {'error': 'Local access only'})
        path = urlsplit(self.path)
        if path.path == '/api/logs':
            try:
                filters = logs_api.parse_filters(parse_qs(path.query, keep_blank_values=True))
            except (ValueError, TypeError):
                return self.respond(400, {'error': 'Проверьте фильтры и даты.'})
            try:
                return self.respond(200, logs_api.read_logs(filters, COMMAND))
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                return self.respond(503, {'error': 'Не удалось прочитать логи. Повторите обновление.'})
        if path.path == '/api/data':
            try:
                return self.respond(200, snapshot.get(parse_qs(path.query).get('refresh') == ['1']))
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                return self.respond(503, {'error': 'Не удалось прочитать VPS. Последний загруженный снимок остаётся на экране; проверьте SSH и повторите обновление.'})
        if path.path in FILES:
            name, mime = FILES[path.path]
            return self.respond(200, (ROOT / name).read_bytes(), mime)
        return self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        self.respond(405, {'error': 'Read-only prototype'})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt, *args):
        pass  # Keep account data and URLs out of terminal logs.


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18840)
    args = parser.parse_args()
    print(f'ATLAS read-only preview: http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
