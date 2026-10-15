"""Read/verify research artifacts; submit one independent ordinary baseline replay."""
import argparse
import csv
import io
import json
import math
import time
import urllib.request

parser=argparse.ArgumentParser();parser.add_argument('optimization');parser.add_argument('walk_forward');args=parser.parse_args()
base='http://127.0.0.1:18841/api/backtest/'
def fetch(path,payload=None,raw=False):
    req=urllib.request.Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Origin':'https://46.225.132.195','Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as response:data=response.read()
    return data if raw else json.loads(data)
def wait(id):
    for _ in range(240):
        job=fetch('jobs/'+id)
        if job['status'] not in ('queued','running'):break
        time.sleep(.5)
    assert job['status']=='succeeded',(id,job['status'],job['error'])
    return job
for id in (args.optimization,args.walk_forward):
    job=wait(id);prefix='jobs/'+id+'/files/';comparison=fetch(prefix+'comparison.json');result=fetch(prefix+'result.json');baseline=fetch(prefix+'baseline-result.json');locks=fetch(prefix+'selection-locks.json');protocol=fetch(prefix+'protocol.json')
    assert comparison['selected']==result['summary'];assert comparison['baseline']==baseline['summary']
    assert result['config']==baseline['config']
    assert baseline['metadata']['request']['start']==comparison['oos_start'];assert baseline['metadata']['request']['end']==comparison['oos_end']
    assert baseline['metadata']['parameters']==job['request']['parameters']
    assert result['summary']['first_timestamp']==baseline['summary']['first_timestamp'];assert result['summary']['last_timestamp']==baseline['summary']['last_timestamp']
    assert result['summary']['ticks_processed']==baseline['summary']['ticks_processed']
    for file,summary in [('daily_pnl.csv',result['summary']),('baseline-daily_pnl.csv',baseline['summary'])]:
        rows=csv.DictReader(io.StringIO(fetch(prefix+file,raw=True).decode()));pnl=math.fsum(float(r['net_pnl']) for r in rows)
        assert math.isclose(pnl,summary['final_equity']-summary['initial_cash'],abs_tol=1e-7)
    assert math.isclose(sum(w['oos_profit'] for w in comparison['windows']),result['summary']['final_equity']-result['summary']['initial_cash'],abs_tol=1e-7)
    assert len(locks)==len(comparison['windows'])
    for lock,w in zip(locks,comparison['windows']):
        assert lock['parameters']==w['parameters'];assert 'oos_profit' not in lock
        assert lock['fit_start']<lock['validation_start']<lock['test_start']<lock['test_end']
    for line in fetch(prefix+'trials.jsonl',raw=True).decode().splitlines():
        trial=json.loads(line);assert trial['end']<=locks[trial['window']]['test_start']
    html=fetch(prefix+'report.html',raw=True);assert b'Plotly.newPlot' in html
    for name in ['selected-report.html','baseline-report.html','trials.csv','baseline-equity.csv','baseline-fills.csv']:assert fetch(prefix+name,raw=True)
    print(json.dumps({'job':id,'mode':job['request']['mode'],'status':'verified','windows':len(locks),'selected_equity':result['summary']['final_equity'],'baseline_equity':baseline['summary']['final_equity'],'simulations':comparison['simulations']}),flush=True)
job=wait(args.optimization);r=job['summary']['research'];request={**job['request'],'mode':'backtest','start':r['oos_start'],'end':r['oos_end']};request.pop('research')
ordinary=fetch('jobs',request);print(json.dumps({'ordinary_replay':ordinary['id']}),flush=True);ordinary=wait(ordinary['id'])
for key in ['initial_cash','final_cash','final_equity','total_return','max_drawdown','max_drawdown_usd','fill_count','ticks_processed','closed_trades','profit_factor','first_timestamp','last_timestamp']:
    assert ordinary['summary'][key]==r['baseline'][key],(key,ordinary['summary'][key],r['baseline'][key])
for name in ['fills.csv','equity.csv','daily_pnl.csv']:
    assert fetch('jobs/'+ordinary['id']+'/files/'+name,raw=True)==fetch('jobs/'+args.optimization+'/files/baseline-'+name,raw=True),name
print(json.dumps({'ordinary_replay':ordinary['id'],'baseline_identical_metrics_fills_equity_daily':True}),flush=True)
