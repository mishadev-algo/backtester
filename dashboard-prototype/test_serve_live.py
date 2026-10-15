import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from serve_live import LiveSnapshot, LiveHandler, LIVE_FILES


class LiveTests(unittest.TestCase):
    def test_database_snapshot_uses_fixed_peer_readonly_query(self):
        data = {k: [] for k in ('accounts', 'deals', 'positions', 'backtests', 'backtest_days', 'hosts', 'terminal_status')}
        state = LiveSnapshot()
        with patch('serve_live.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(data))) as run:
            self.assertEqual(state.get(), data)
            args, kwargs = run.call_args
            self.assertEqual(args[0][0], 'psql')
            self.assertIn('--no-password', args[0])
            self.assertIn('REPEATABLE READ READ ONLY', kwargs['input'])
            self.assertTrue(kwargs['input'].rstrip().endswith('ROLLBACK;'))
            state.get()
            self.assertEqual(run.call_count, 1)
            state.loaded = -100
            run.return_value = SimpleNamespace(returncode=1)
            with self.assertRaises(RuntimeError): state.get()
            self.assertEqual(state.data, data)

    def test_routes_are_private_and_reject_writes(self):
        h = object.__new__(LiveHandler)
        calls = []
        h.respond = lambda status, body, *args: calls.append(status)
        h.server = SimpleNamespace(server_port=18841, public_origin='https://46.225.132.195')
        h.path = '/healthz'
        h.headers = {'Host': '46.225.132.195'}
        h.do_GET(); self.assertEqual(calls[-1], 403)
        h.headers = {'Host': '127.0.0.1:18841', 'Origin': 'https://evil.example'}
        h.do_GET(); self.assertEqual(calls[-1], 403)
        h.headers = {'Host': '127.0.0.1:18841', 'Origin': 'https://46.225.132.195'}
        h.do_GET(); self.assertEqual(calls[-1], 200)
        for path in ['/private/mapping.json', '/read_snapshot.sql', '/serve_live.py', '/manifest.json', '/../live-config.js']:
            h.path = path; h.do_GET(); self.assertEqual(calls[-1], 404)
        for method in (h.do_POST, h.do_PUT, h.do_PATCH, h.do_DELETE):
            method(); self.assertEqual(calls[-1], 405)
        self.assertIn('/live-config.js', LIVE_FILES)
