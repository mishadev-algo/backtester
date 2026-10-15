"""Run on the VPS: authenticated-upstream API acceptance, no trading mutations."""
import json
import time
import urllib.request
import urllib.error
base='http://127.0.0.1:18841'
def call(path,data=None,origin='https://46.225.132.195'):
    req=urllib.request.Request(base+path,data=None if data is None else json.dumps(data).encode(),headers={'Origin':origin,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
cat=call('/api/backtest/catalog');assert cat['ready']
request=dict(strategy='s019',start='2026-10-01',end='2026-10-14',parameters=cat['strategies'][0]['parameters'],initial_cash=100000,commission_bps=0,spread_bps=0,slippage_bps=0,warmup_days=180)
try:
    call('/api/backtest/jobs',request,'https://evil.invalid');raise AssertionError('Foreign Origin accepted')
except urllib.error.HTTPError as e:assert e.code==403
try:
    call('/api/backtest/jobs',{**request,'strategy':'/tmp/injected.py'});raise AssertionError('Arbitrary strategy accepted')
except urllib.error.HTTPError as e:assert e.code==400
job=call('/api/backtest/jobs',request);print(json.dumps({'submitted':job['id']}),flush=True)
for _ in range(60):
    row=call('/api/backtest/jobs/'+job['id'])
    if row['status'] in ('failed','succeeded','cancelled'):break
    time.sleep(1)
assert row['status']=='succeeded',row
assert row['summary']['fill_count']>0
report=call('/api/backtest/jobs/'+job['id']+'/files/result.json')
assert report['summary']==row['summary']
for route,field in [('/api/data','accounts'),('/api/logs','rows')]:assert isinstance(call(route)[field],list)
print(json.dumps({'job':job['id'],'status':row['status'],'summary':row['summary'],'foreign_origin':403,'arbitrary_code':400,'live_snapshots_and_logs':'ok'}),flush=True)
