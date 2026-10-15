import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backtest_worker as worker
import backtest_research as research
from backtest_jobs import Jobs
from backtester.models import BacktestConfig,BacktestResult,EquityPoint
try:
    from backtester.s019_search import EquityState
    AVAILABLE=True
except ImportError:AVAILABLE=False

@dataclass(frozen=True)
class FakeResult(BacktestResult):
    def summary(self):return {**super().summary(),'closed_trades':50,'max_drawdown_usd':100.}

@unittest.skipUnless(AVAILABLE,'Requires backtester environment')
class ResearchTests(unittest.TestCase):
    def request(self,mode='optimization'):
        params=json.loads((worker.ENGINE/'parameters/s019-mt5-report-no-news.json').read_text());params['InpEntryDelaySeed']=190300
        return dict(mode=mode,strategy='s019',start='2026-01-01',end='2026-05-01',parameters=params,initial_cash=100000,commission_bps=1,slippage_bps=.5,spread_bps=0,warmup_days=30,
                    research=dict(grid={'InpTakeProfitRR':[2.,3.]},trials=2,shortlist=1,seed=7,min_fit_trades=1,min_validation_trades=1,**({'fit_end':'2026-02-01','validation_end':'2026-03-01'} if mode=='optimization' else {'fit_months':1,'validation_months':1,'test_months':1})))
    def test_budget_determinism_and_validation(self):
        request=self.request();research.validate_research(request,request['research'])
        a=research.candidates(request['parameters'],research.GRID,12,7)
        self.assertEqual(a,research.candidates(request['parameters'],research.GRID,12,7));self.assertEqual(len(a),12);self.assertEqual(a[0],request['parameters'])
        for override in ({'trials':33},{'grid':{'broker':[1]}},{'grid':{'InpADRPeriods':[1.2]}},{'validation_end':'2026-02-01'},{'grid':{'InpTakeProfitRR':[float('nan')]}}):
            r=self.request()
            with self.assertRaises((ValueError,TypeError)):research.validate_research(r,{**r['research'],**override})
        r=self.request('walk_forward');r['end']='2026-02-01'
        with self.assertRaises(ValueError):research.validate_research(r,r['research'])
    def exercise(self,mode,eligible):
        with tempfile.TemporaryDirectory() as tmp:
            store=Jobs(tmp);request=self.request(mode);job=store.submit(request);calls=[];account_ids=[]
            output=Path(tmp)/'runs'/job['id']
            plan=research.windows(request)
            def simulate(req,status,*,manifest,collect=True,account=None,cash=False):
                self.assertEqual(req['commission_bps'],request['commission_bps']);self.assertEqual(req['initial_cash'],100000)
                status('fake',30);calls.append((req['start'],req['end'],collect,cash))
                is_oos=collect and len(calls)>1 and not (req['start']==plan[0]['test_start'] and req['end']==plan[-1]['test_end'] and len(calls)==expected_calls)
                if is_oos:
                    locks=json.loads((output/'selection-locks.json').read_text());self.assertEqual(locks[-1]['test_start'],req['start']);self.assertNotIn('oos_profit',locks[-1]);self.assertEqual(locks[-1]['decision'],'selected' if eligible else 'cash')
                if account:account_ids.append(id(account));balance=account['balance']
                else:balance=100000
                profit=100 if eligible else -100
                if cash:profit=0
                next_account=account or {'state':EquityState(100000),'balance':100000}
                next_account['balance']=balance+profit;next_account['state'].peak=max(next_account['state'].peak,balance+profit)
                points=(EquityPoint(research.stamp(req['start']),balance,0,balance,1),EquityPoint(research.stamp(req['end'])-timedelta(seconds=1),balance+profit,0,balance+profit,2))
                result=FakeResult(BacktestConfig(initial_cash=100000),points,(),False,'ticks',{'broker':{'server_timezone':'UTC'}})
                return result,{'dataset_id':'synthetic'},next_account
            expected_calls=len(plan)*(2+(3 if eligible else 0)+1)+1
            summary=research.execute_research(job,store,simulate,SimpleNamespace(model_dump=lambda **_: {'dataset_id':'synthetic'}),lambda *args:None)
            self.assertEqual(len(calls),expected_calls)
            self.assertEqual(calls[-1][:2],(plan[0]['test_start'],plan[-1]['test_end']))
            self.assertEqual(summary['research']['cash_windows'],0 if eligible else len(plan))
            self.assertEqual(summary['final_equity'],100000+len(plan)*(100 if eligible else 0))
            self.assertTrue((output/'report.html').is_file());self.assertTrue((output/'baseline-result.json').is_file())
            self.assertEqual(len(json.loads((output/'selection-locks.json').read_text())),len(plan))
            if mode=='walk_forward':self.assertEqual(len(account_ids),len(plan)-1)
            # Fit/validation calls always end before their window's OOS, and never carry an account.
            rows=[json.loads(line) for line in (output/'trials.jsonl').read_text().splitlines()]
            for row in rows:self.assertLessEqual(row['end'],plan[row['window']]['test_start'])
            self.assertTrue(all(row['phase'] in ('fit','validation') for row in rows))
    def test_optimization_lock_before_oos_and_same_period_baseline(self):self.exercise('optimization',True)
    def test_walk_forward_continuous_account_and_locks(self):self.exercise('walk_forward',True)
    def test_no_eligible_candidates_stay_in_cash(self):self.exercise('walk_forward',False)
    def test_cancel_preserves_audit_without_executing_oos(self):
        from backtest_worker import Cancelled
        with tempfile.TemporaryDirectory() as tmp:
            jobs=Jobs(tmp);job=jobs.submit(self.request())
            def stop(*a,**kw):raise Cancelled()
            with self.assertRaises(Cancelled):research.execute_research(job,jobs,stop,SimpleNamespace(model_dump=lambda **_:{}),lambda *a:None)
            root=Path(tmp)/'runs'/job['id'];self.assertTrue((root/'protocol.json').exists());self.assertFalse((root/'selection-locks.json').exists())

if __name__=='__main__':unittest.main()
