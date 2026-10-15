"""Opt-in integration checks: synthetic CTEs, no tables or stored records changed.
Run ATLAS_TEST_LOG_DB=1 python3 -m unittest discover -s dashboard-prototype -p test_logs_classification.py
"""
import base64
import json
import os
import unittest
import logs_api
import serve

@unittest.skipUnless(os.environ.get('ATLAS_TEST_LOG_DB')=='1','Requires authorized VPS read-only SSH')
class LogClassificationTests(unittest.TestCase):
    def test_actual_sql_classification_and_filters(self):
        def mt(message, source='S-019_v4 (DAX40,M5)', level=0):
            return f'AB\t{level}\t10:00:00.000\t{source}\t{message}'
        samples=[
            (mt('EA_EVENT_V1 '+json.dumps({'strategy_id':'S-015','level':'ERROR','message':'unavailable'})), 'S-015','error','payload'),
            (mt('EA_EVENT_V1 '+json.dumps({'strategy_id':'S-015','level':'INFO','context':{'error':123},'message':'filter failed'})), 'S-015','info','payload'),
            (mt('EA_EVENT_V1 '+json.dumps({'strategy_id':'S-019','level':'WARN'})), 'S-019','warning','payload'),
            (mt('ORB range invalid. Range points: 100'), 'S-019','info','default'),
            (mt('S-036 LONG setup reset: EMA filter failed while armed; prior state=ARMED',source='S-036_v2 (XAUUSD,H1)'), 'S-036','info','default'),
            (mt('No errors; errors=0; без сбоев'), 'S-019','info','default'),
            (mt('Cannot select symbol DE40; error=4305',source='1 (DAX40,M5)'), '__unassigned__','error','text'),
            (mt('connection lost',source='Network',level=1), '__unassigned__','warning','mt5'),
            (mt('failed cancel order',source='Trades',level=2), '__unassigned__','error','mt5'),
            (mt('bind error',source='MCP',level=3), '__unassigned__','error','mt5'),
            (mt('S-014: order failed',source='Expert'), 'S-014','error','text'),
            (mt('mentions S-015 and S-019',source='Trades'), '__unassigned__','info','default'),
            (mt('healthy',source='S-0199_v4'), '__unassigned__','info','default'),
            (mt('EA_EVENT_V1 {malformed',source='S-009_v1'), 'S-009','info','default'),
            (mt('warning: retry scheduled'), 'S-019','warning','text'),
            (mt('<img src=x onerror=alert(1)>'), 'S-019','info','default'),
        ]
        rows=[dict(event_id=str(i),received_utc='2026-10-01T10:00:00+00:00',host_id='test-host',terminal_id='test-terminal',stream='experts',raw_line=s[0]) for i,s in enumerate(samples)]
        encoded=base64.b64encode(json.dumps(rows).encode()).decode()
        original=logs_api.query
        def fixture_query(filters):
            prefix=f"WITH log_events AS (SELECT * FROM jsonb_to_recordset(convert_from(decode('{encoded}','base64'),'UTF8')::jsonb) AS x(event_id text, received_utc text, host_id text, terminal_id text, stream text, raw_line text)), accounts AS (SELECT ''::text host_id,''::text terminal_id,0 login,''::text server WHERE false), config AS"
            return original(filters).replace('WITH config AS',prefix,1)
        from unittest.mock import patch
        with patch('logs_api.query',side_effect=fixture_query):
            result=logs_api.read_logs(logs_api.parse_filters({}),serve.COMMAND)
            by_id={int(r['event_id']):r for r in result['rows']}
            for i,(_,strategy,level,reason) in enumerate(samples):
                with self.subTest(i=i):
                    self.assertEqual((by_id[i]['strategy'],by_id[i]['severity'],by_id[i]['severity_source']),(strategy,level,reason))
            scoped=logs_api.read_logs(logs_api.parse_filters({'strategy':['["S-015","S-019"]'],'severity':['["error","warning"]']}),serve.COMMAND)
            self.assertEqual({int(r['event_id']) for r in scoped['rows']},{0,2,14})
            self.assertGreater(scoped['severity_counts']['info'],0)
            empty=logs_api.read_logs(logs_api.parse_filters({'strategy':['[]']}),serve.COMMAND)
            self.assertEqual(empty['total'],0)
            self.assertIn('S-015',empty['strategies'])
