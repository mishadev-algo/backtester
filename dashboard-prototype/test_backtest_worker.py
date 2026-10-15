"""End-to-end batch/report parity with canonical execution on synthetic quotes."""
import csv
import json
import math
import random
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

try:
    import backtest_worker as worker
    from backtester.tick_data import TickCsvProfile, TickDatasetSpec, import_ticks
    from backtester.strategy_loader import load_strategy
    from backtester import Tick, BacktestConfig, run_tick_backtest
    AVAILABLE=True
except ImportError:AVAILABLE=False

@unittest.skipUnless(AVAILABLE,'Requires backtester environment')
class WorkerTests(unittest.TestCase):
    def test_past_only_warmup_batch_continuity_and_report_reconcile(self):
        from backtest_jobs import Jobs
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'ticks.csv';rng=random.Random(17);ticks=[];price=1000.
            with source.open('w') as f:
                writer=csv.writer(f);writer.writerow(['DateTime','Bid','Ask'])
                for i in range(14400):
                    price+=rng.choice([-8,-3,1,4,9]);stamp=datetime(2026,1,5,tzinfo=timezone.utc)+timedelta(seconds=i*20)
                    ticks.append(Tick(stamp,price,price+2,sequence=i));writer.writerow([stamp.isoformat(),price,price+2])
            data=root/'data';import_ticks(source,data,TickDatasetSpec(symbol='TEST',source='synthetic test'),TickCsvProfile(timestamp_format=None,timezone='UTC'))
            params=dict(broker=dict(symbol='TEST',account_currency='USD',server_timezone='Europe/Kyiv',point=1,tick_size=1,tick_value_per_lot=1,digits=0,volume_min=1,volume_max=100,volume_step=1),news_mode='disabled',InpWorkingTimeframe=20,InpADRPeriods=3,InpFastEMAPeriod=2,InpSlowEMAPeriod=5,InpEntryDelaySeed=99,InpDailyPositionLimit=30,InpLimitEntryFromStop=1.0)
            request=dict(strategy='s019',start='2026-01-06',end='2026-01-08',parameters=params,initial_cash=100000,commission_bps=0,spread_bps=0,slippage_bps=0,warmup_days=1)
            jobs=Jobs(root/'runtime');job=jobs.submit(request)
            with patch.object(worker,'DATA',data),patch.object(worker,'BATCH_SIZE',137):summary=worker.execute(job,jobs)
            with load_strategy(str(worker.ENGINE/'strategies/s019_v0_4_9.py'),'ticks') as loaded:
                start=datetime(2026,1,6,tzinfo=timezone.utc);end=datetime(2026,1,8,tzinfo=timezone.utc)
                ref=run_tick_backtest([t for t in ticks if start<=t.timestamp<end],loaded.factory(**params),BacktestConfig(initial_cash=100000),warmup_ticks=[t for t in ticks if t.timestamp<start])
            self.assertTrue(ref.fills)
            self.assertEqual(summary['ticks_processed'],len(ref.equity))
            self.assertAlmostEqual(summary['final_equity'],ref.summary()['final_equity'],places=9)
            self.assertAlmostEqual(summary['max_drawdown'],ref.summary()['max_drawdown'],places=13)
            doc=json.loads((jobs.root/'runs'/job['id']/'result.json').read_text())
            self.assertEqual(len(doc['fills']),len(ref.fills))
            actual={p.tick_sequence:p for p in ref.equity}
            for sample in doc['equity']:
                expected=actual[sample['tick_sequence']]
                self.assertAlmostEqual(sample['equity'],expected.equity,places=9)
                self.assertEqual(sample['cash'],expected.cash)
                self.assertEqual(sample['quantity'],expected.quantity)
            with (jobs.root/'runs'/job['id']/'daily_pnl.csv').open() as f:
                pnl=math.fsum(float(r['net_pnl']) for r in csv.DictReader(f))
            self.assertAlmostEqual(pnl,summary['final_equity']-100000,places=9)
