"""Single supervised worker. Quote arrays are bounded; account state survives batches."""
import fcntl
import hashlib
import json
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest_jobs import Jobs, ENGINE, DATA, RUNTIME
sys.path.insert(0,str(ENGINE/'src'))

BATCH_SIZE = 131072

class Cancelled(Exception): pass

def execute(job,store):
    import numpy as np
    import pyarrow.dataset as ds
    from backtester.models import BacktestConfig, BacktestResult, EquityPoint, Bar
    from backtester.s019_search import QuoteTape, EquityState, run_search_trial, EPOCH
    from backtester.order_engine import OrderBroker
    from backtester.strategy_loader import load_strategy
    from backtester.experiments import code_identity
    from backtester.tick_data import inspect_ticks
    from backtester.reporting import write_report
    from backtester.engine import run_backtest
    request=job['request']; id=job['id']; begun=time.monotonic()
    def status(stage,progress):
        if store.get(id)['cancel']:raise Cancelled()
        if time.monotonic()-begun>7200:raise TimeoutError('Расчёт превысил лимит 2 часа')
        store.update(id,stage=stage,progress=min(99,progress))
    status('Проверка целостности данных',1)
    manifest=inspect_ticks(DATA)
    start=datetime.fromisoformat(request['start']).replace(tzinfo=timezone.utc)
    end=datetime.fromisoformat(request['end']).replace(tzinfo=timezone.utc)
    warm=start-timedelta(days=request['warmup_days'])
    source=ds.dataset(DATA/'ticks.parquet',format='parquet')
    selection=(ds.field('timestamp')>=warm)&(ds.field('timestamp')<end)
    scanner=source.scanner(columns=['timestamp','bid','ask','sequence'],filter=selection,batch_size=BATCH_SIZE,use_threads=False,batch_readahead=1,fragment_readahead=1)
    config=BacktestConfig(**{k:request[k] for k in ('initial_cash','commission_bps','spread_bps','slippage_bps')},price_type='bid_ask' if request['strategy']=='s019' else 'bid')
    spec=str(ENGINE/'strategies/s019_v0_4_9.py') if request['strategy']=='s019' else 'sma'
    count=warm_count=0; points=[]; first=None; last=None; bars=[]; current=None
    state=EquityState(config.initial_cash)
    with load_strategy(spec,'ticks' if request['strategy']=='s019' else 'bars') as loaded:
        strategy=loaded.factory(**request['parameters'])
        broker=OrderBroker(config,strategy.broker_spec) if request['strategy']=='s019' else None
        # Samples include every callback, interval boundaries and equity extremes.
        # Broker balance and position are constant within each skipped interval.
        for batch in scanner.to_batches():
            if not batch.num_rows:continue
            arrays={n:batch.column(n).to_numpy() for n in batch.schema.names}
            tape=QuoteTape(arrays['timestamp'].astype('datetime64[us]').astype(np.int64),arrays['bid'],arrays['ask'],arrays['sequence'])
            boundary=tape.boundary(start)
            def observe(indices,values):
                p=broker.position; quantity=0 if p is None else p.volume*(1 if p.side=='buy' else -1)
                for ix,value in zip(indices,values):
                    ix=int(ix);point=EquityPoint(EPOCH+timedelta(microseconds=int(tape.timestamps[ix])),broker.balance,quantity,float(value),int(tape.sequence[ix]))
                    if points and points[-1].tick_sequence==point.tick_sequence:points[-1]=point
                    else:points.append(point)
            if broker:
                # A batch entirely before start warms indicators without trading.
                if boundary==len(tape.bid):
                    for a,b in zip(tape.bar_starts,np.r_[tape.bar_starts[1:],len(tape.bid)]):
                        tick=tape.tick(int(a));broker.advance(tick,int(tape.sequence[a]),warmup=True);strategy.on_tick([tick],broker)
                        strategy.current.high=max(strategy.current.high,float(tape.bid[a:b].max()))
                        strategy.current.low=min(strategy.current.low,float(tape.bid[a:b].min()))
                        strategy.current.close=float(tape.bid[b-1])
                else:
                    run_search_trial(tape,strategy,config,boundary,len(tape.bid),broker=broker,equity_state=state,
                                     equity_observer=observe,continue_account=count>0)
            else:
                # UTC M20 boundaries equal broker M20 boundaries for this dataset.
                for a,b in zip(tape.bar_starts,np.r_[tape.bar_starts[1:],len(tape.bid)]):
                    stamp=EPOCH+timedelta(microseconds=int(tape.timestamps[a]//1_200_000_000*1_200_000_000))
                    q=tape.bid[a:b]
                    if current and current[0]!=stamp:
                        bars.append(Bar(current[0],*current[1:],open_timestamp=current[0],close_timestamp=current[0]+timedelta(minutes=20)))
                        current=None
                    if current is None:current=[stamp,float(q[0]),float(q.max()),float(q.min()),float(q[-1])]
                    else:current[2]=max(current[2],float(q.max()));current[3]=min(current[3],float(q.min()));current[4]=float(q[-1])
            warm_count+=boundary;count+=len(tape.bid)-boundary
            if boundary<len(tape.bid):first=first or tape.tick(boundary).timestamp
            last=tape.tick(len(tape.bid)-1).timestamp
            fraction=(last-warm).total_seconds()/max(1,(end-warm).total_seconds())
            status(('Прогрев' if not count else 'Расчёт')+' · '+last.strftime('%Y-%m-%d %H:%M UTC'),3+fraction*90)
        if not count:raise ValueError('В выбранном периоде нет котировок')
        if broker:
            # Preserve open position valuation, matching the canonical engine.
            p=broker.position
            points.append(EquityPoint(last,broker.balance,0 if p is None else p.volume*(1 if p.side=='buy' else -1),broker.equity(),broker.sequence)) if not points or points[-1].timestamp!=last else None
            metadata={'model':'S019 M20; source Bid/Ask ticks, bounded batches, server protections before OnTick',
                      'broker':strategy.broker_spec.model_dump(mode='json'),'strategy':strategy.execution_metadata(),
                      'margin_checked':strategy.broker_spec.margin_per_lot is not None,
                      'equity_storage':'Source tick execution; retained callback/interval extremes, not every tick. Open positions are marked, not forcibly closed.',
                      'profit_model':'Linear constant account-currency tick value; no swaps or currency conversion'}
            @dataclass(frozen=True,slots=True)
            class SampledResult(BacktestResult):
                def summary(self):
                    summary=BacktestResult.summary(self)
                    summary.update(ticks_processed=count,max_drawdown=state.max_drawdown,max_drawdown_usd=state.max_drawdown_usd,
                                   first_timestamp=first.isoformat(),last_timestamp=last.isoformat())
                    return summary
            result=SampledResult(config,tuple(points),tuple(broker.fills),broker.pending is not None or strategy.has_deferred_entry,
                                 'ticks',metadata,tuple(broker.events))
        else:
            # Discard the final incomplete bar unless its full interval was observed.
            if current and min(end,manifest.last_timestamp)>=current[0]+timedelta(minutes=20):
                bars.append(Bar(current[0],*current[1:],open_timestamp=current[0],close_timestamp=current[0]+timedelta(minutes=20)))
            result=run_backtest([b for b in bars if start<=b.timestamp<end],strategy,config,warmup_bars=[b for b in bars if b.timestamp<start])
        status('Сохранение отчёта',96)
        metadata={'dataset':manifest.model_dump(mode='json'),'dataset_id':manifest.dataset_id,**code_identity(),
                  'strategy':loaded.spec,'strategy_source_sha256':loaded.source_sha256,'parameters':request['parameters'],
                  'period':{'start_inclusive':start.isoformat(),'end_exclusive':end.isoformat()},'warmup_ticks_used':warm_count,
                  'worker_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'request':request,'order_execution':result.execution_metadata,'job_id':id}
        write_report(result,store.root/'runs'/id,metadata)
        status('Отчёт готов',99)
        return result.summary()

def main():
    store=Jobs()
    lock=(RUNTIME/'worker.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with store.connect() as c:
        c.execute("UPDATE jobs SET status='failed',error='Расчёт прерван перезапуском worker. Запустите повторно.',stage='Прерван' WHERE status='running'")
    def stop(*args):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    while True:
        job=store.claim()
        if not job:time.sleep(1);continue
        try:
            summary=execute(job,store)
            store.update(job['id'],status='succeeded',progress=100,stage='Готово',summary=summary)
        except Cancelled:store.update(job['id'],status='cancelled',stage='Отменён')
        except KeyboardInterrupt:
            store.update(job['id'],status='failed',stage='Прерван',error='Worker остановлен. Запустите повторно.')
            raise
        except Exception as exc:
            store.update(job['id'],status='failed',stage='Ошибка',error=str(exc)[:1500])

if __name__=='__main__':main()
