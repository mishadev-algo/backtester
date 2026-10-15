import base64
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import logs_api
from serve_live import LiveHandler


class LogsTests(unittest.TestCase):
    def test_filters_preserve_all_empty_and_multiple_choices(self):
        self.assertIsNone(logs_api.parse_filters({})['host'])
        self.assertEqual(logs_api.parse_filters({'host':['[]']})['host'], [])
        self.assertEqual(logs_api.parse_filters({'host':['["a","b"]']})['host'], ['a','b'])
        for params in [{'host':['"a"']}, {'stream':['["other"]']}, {'severity':['["fatal"]']}, {'strategy':['42']}, {'page':['-1']}, {'q':['x'*201]}, {'start':['2026-10-15'],'end':['2026-10-14']}, {'before':['2026-10-15T10:00:00']}]:
            with self.subTest(params=params), self.assertRaises(ValueError): logs_api.parse_filters(params)

    def test_untrusted_search_is_data_and_transaction_readonly(self):
        filters=logs_api.parse_filters({'q':["'; SELECT pg_sleep(10); -- <script>"], 'page':['2']})
        sql=logs_api.query(filters)
        self.assertNotIn(filters['q'], sql)
        token=re.search(r"decode\('([^']+)'", sql).group(1)
        self.assertEqual(json.loads(base64.b64decode(token)),filters)
        self.assertIn('REPEATABLE READ READ ONLY',sql)
        self.assertTrue(sql.rstrip().endswith('ROLLBACK;'))
        self.assertIn('statement_timeout',sql)

    def test_http_validation_and_unavailable_database(self):
        h=object.__new__(LiveHandler)
        h.server=SimpleNamespace(server_port=18841,public_origin='https://46.225.132.195')
        h.headers={'Host':'127.0.0.1:18841'}
        calls=[];h.respond=lambda status,body,*a:calls.append(status)
        h.path='/api/logs?page=-1'
        with patch('logs_api.read_logs') as query:
            h.do_GET();self.assertEqual(calls[-1],400);query.assert_not_called()
        h.path='/api/logs'
        with patch('logs_api.read_logs',side_effect=RuntimeError('private DB detail')):
            h.do_GET();self.assertEqual(calls[-1],503)
        for path in ['/logs_api.py','/read_logs.sql','/classify_logs.sql']:
            h.path=path;h.do_GET();self.assertEqual(calls[-1],404)
