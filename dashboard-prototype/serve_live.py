"""Authenticated Caddy upstream: loopback only, fixed read-only PostgreSQL snapshot.

Run as the existing dashboard OS user; PostgreSQL peer authentication requires
no credentials. Public access MUST go through the existing authenticated proxy.
"""
import argparse
import json
import subprocess
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import serve
import logs_api

LIVE_FILES = dict(serve.FILES, **{'/live-config.js': ('live-config.js', 'text/javascript')})


class LiveSnapshot(serve.Snapshot):
    def get(self, refresh=False):
        with self.lock:
            age = time.monotonic() - self.loaded
            if self.data is not None and age < (10 if refresh else 30):
                return self.data
            result = subprocess.run(
                ['psql', '-X', '--no-password', '--set=ON_ERROR_STOP=1', '-qAt', '-d', 'dashboard'],
                input=(serve.ROOT / 'read_snapshot.sql').read_text(),
                capture_output=True, text=True, timeout=25,
            )
            if result.returncode:
                raise RuntimeError('Snapshot unavailable')
            data = json.loads(result.stdout)
            for field in ('accounts', 'deals', 'positions', 'backtests', 'backtest_days', 'hosts', 'terminal_status'):
                if not isinstance(data.get(field), list):
                    raise ValueError('Unexpected snapshot schema')
            self.data, self.loaded = data, time.monotonic()
            return data


snapshot = LiveSnapshot()


class LiveHandler(serve.Handler):
    def write_allowed(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}' and self.headers.get('Origin') == self.server.public_origin

    def do_GET(self):
        # Caddy rewrites Host to this loopback listener after authenticating.
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            return self.respond(403, {'error': 'Proxy access required'})
        origin = self.headers.get('Origin')
        if origin and origin != self.server.public_origin:
            return self.respond(403, {'error': 'Origin denied'})
        path = urlsplit(self.path)
        if serve.backtest_api.route(self, 'GET'): return
        if path.path == '/logs':
            self.send_response(302)
            self.send_header('Location', '/'+('?' + path.query if path.query else '')+'#logs')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        if path.path == '/api/logs':
            try:
                filters = logs_api.parse_filters(parse_qs(path.query, keep_blank_values=True))
            except (ValueError, TypeError):
                return self.respond(400, {'error': 'Проверьте фильтры и даты.'})
            try:
                return self.respond(200, logs_api.read_logs(filters, ['psql', '-X', '--no-password', '--set=ON_ERROR_STOP=1', '-qAt', '-d', 'dashboard']))
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                return self.respond(503, {'error': 'Не удалось прочитать логи. Повторите обновление.'})
        if path.path == '/healthz':
            return self.respond(200, {'status': 'ok', 'read_only': True})
        if path.path == '/api/data':
            try:
                return self.respond(200, snapshot.get(parse_qs(path.query).get('refresh') == ['1']))
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                return self.respond(503, {'error': 'Не удалось обновить данные. Повторите обновление позже.'})
        if path.path in LIVE_FILES:
            name, mime = LIVE_FILES[path.path]
            return self.respond(200, (serve.ROOT / name).read_bytes(), mime)
        return self.respond(404, {'error': 'Not found'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18841)
    parser.add_argument('--public-origin', default='https://46.225.132.195')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), LiveHandler)
    server.public_origin = args.public_origin
    print(f'ATLAS live upstream on loopback:{args.port}', flush=True)
    server.serve_forever()
