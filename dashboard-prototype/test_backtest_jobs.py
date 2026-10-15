import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backtest_jobs import Jobs
from serve import Handler
from serve_live import LiveHandler

class QueueTests(unittest.TestCase):
    def test_durable_queue_limit_cancel_and_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs=Jobs(tmp)
            first=jobs.submit({'strategy':'s019'})
            for _ in range(4):jobs.submit({})
            with self.assertRaisesRegex(ValueError,'Очередь'):jobs.submit({})
            claimed=jobs.claim();self.assertEqual(claimed['id'],first['id'])
            self.assertEqual(Jobs(tmp).get(first['id'])['status'],'running')
            jobs.cancel(first['id']);self.assertEqual(jobs.get(first['id'])['cancel'],1)
            jobs.update(first['id'],status='cancelled')
            row=jobs.submit({});jobs.cancel(row['id'])
            self.assertEqual(jobs.get(row['id'])['status'],'cancelled')
            with self.assertRaises(KeyError):jobs.get('../private')

    def test_post_requires_exact_origin_and_host(self):
        for cls,origin in ((Handler,'http://localhost:18841'),(LiveHandler,'https://46.225.132.195')):
            h=object.__new__(cls);h.server=SimpleNamespace(server_port=18841,public_origin='https://46.225.132.195')
            h.path='/api/backtest/jobs';calls=[];h.respond=lambda s,*a:calls.append(s)
            for headers in ({'Host':'127.0.0.1:18841'},{'Host':'127.0.0.1:18841','Origin':'https://evil.invalid'},{'Host':'evil.invalid','Origin':origin}):
                h.headers=headers;h.do_POST();self.assertEqual(calls[-1],403)
            h.headers={'Host':'127.0.0.1:18841','Origin':origin}
            with patch('backtest_api.route',return_value=True) as route:
                h.do_POST();route.assert_called_once_with(h,'POST')

    def test_report_allowlist_and_nonexistent_job(self):
        import backtest_api
        with tempfile.TemporaryDirectory() as tmp:
            jobs=Jobs(tmp);job=jobs.submit({});jobs.update(job['id'],status='succeeded')
            root=Path(tmp)/'runs'/job['id'];root.mkdir(parents=True);(root/'report.html').write_text('<html>Actual report</html>')
            h=SimpleNamespace();calls=[];h.respond=lambda s,*a:calls.append((s,a))
            with patch('backtest_api.Jobs',return_value=jobs):
                for path in ('/api/backtest/jobs/../files/report.html','/api/backtest/jobs/'+job['id']+'/files/../../jobs.sqlite3','/api/backtest/jobs/'+job['id']+'/files/private.json'):
                    h.path=path;backtest_api.route(h,'GET');self.assertEqual(calls[-1][0],404)
                h.path='/api/backtest/jobs/'+job['id']+'/files/report.html';backtest_api.route(h,'GET');self.assertEqual(calls[-1][0],200)
                jobs.update(job['id'],status='failed');backtest_api.route(h,'GET');self.assertEqual(calls[-1][0],404)
