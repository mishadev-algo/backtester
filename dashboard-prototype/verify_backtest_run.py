"""Manual server acceptance: real data, durable job, exported reconciliation."""
import csv
import json
import math
from backtest_jobs import Jobs, catalog, validate
from backtest_worker import execute

store=Jobs()
cat=catalog()
for kind in ('s019','sma'):
    request=validate(dict(strategy=kind,start='2026-09-01',end='2026-10-01',parameters=next(x['parameters'] for x in cat['strategies'] if x['id']==kind),initial_cash=100000,commission_bps=0,slippage_bps=0,spread_bps=0 if kind=='s019' else 2,warmup_days=180))
    job=store.submit(request);store.update(job['id'],status='running')
    try:
        summary=execute(job,store)
        output=store.root/'runs'/job['id']
        with (output/'daily_pnl.csv').open() as f:pnl=math.fsum(float(r['net_pnl']) for r in csv.DictReader(f))
        assert math.isclose(pnl,summary['final_equity']-summary['initial_cash'],abs_tol=1e-7)
        assert summary['fill_count']>0
        assert (output/'report.html').is_file()
        store.update(job['id'],status='succeeded',stage='Готово',progress=100,summary=summary)
        print(json.dumps({'id':job['id'],'strategy':kind,'summary':summary,'daily_reconciled':True}),flush=True)
    except BaseException as exc:
        store.update(job['id'],status='failed',stage='Ошибка',error=str(exc));raise
