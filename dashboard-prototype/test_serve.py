import unittest
from unittest.mock import patch
from types import SimpleNamespace
from serve import Snapshot, Handler, FILES, ROOT


class ReadOnlyTests(unittest.TestCase):
    def test_snapshot_refresh_failure_preserves_previous(self):
        state = Snapshot()
        state.data = {'fetched_at': 'previous'}
        state.loaded = -100
        with patch('serve.subprocess.run', return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(RuntimeError):
                state.get(refresh=True)
            self.assertEqual(state.get(), {'fetched_at': 'previous'})
            self.assertEqual(run.call_count, 1)

    def test_readonly_query_and_no_secret_static_files(self):
        sql = (ROOT / 'read_snapshot.sql').read_text()
        self.assertIn('REPEATABLE READ READ ONLY', sql)
        self.assertTrue(sql.rstrip().endswith('ROLLBACK;'))
        self.assertNotIn('/read_snapshot.sql', FILES)
        self.assertNotIn('/serve.py', FILES)

    def test_writes_and_foreign_origins_rejected(self):
        h = object.__new__(Handler)
        calls = []
        h.respond = lambda status, body: calls.append(status)
        for method in (h.do_POST, h.do_PUT, h.do_PATCH, h.do_DELETE):
            method()
        self.assertEqual(calls, [405] * 4)
        h.server = SimpleNamespace(server_port=18840)
        h.headers = {'Host': 'external.example:18840'}
        h.do_GET()
        self.assertEqual(calls[-1], 403)
        h.headers = {'Host': '127.0.0.1:18840', 'Origin': 'https://external.example'}
        h.do_GET()
        self.assertEqual(calls[-1], 403)


if __name__ == '__main__':
    unittest.main()
