"""Bounded chronological S019 search; every selection is locked before OOS execution."""
import csv
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import random
import statistics

GRID = {'InpADRPeriods':[8,12,16,24], 'InpLongADRMultiplier':[.75,1,1.25],
        'InpShortADRMultiplier':[1,1.5,2], 'InpLimitEntryFromStop':[.35,.5,.65,.8],
        'InpTakeProfitRR':[1.5,2,2.5,3], 'InpSlowEMAPeriod':[100,150,200],
        'InpLimitLifetimeBars':[1,3,6]}
INTEGER_KEYS={'InpADRPeriods','InpSlowEMAPeriod','InpLimitLifetimeBars'}

def stamp(value):return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
def day(value):return value.date().isoformat()
def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))

def candidates(parameters,grid,budget,seed):
    """Exact bounded budget, baseline first, deterministic sampling without replacement."""
    keys=sorted(grid);total=math.prod(len(grid[k]) for k in keys)
    result=[dict(parameters)];seen={json.dumps(parameters,sort_keys=True)}
    for index in random.Random(seed).sample(range(total),min(total,budget)):
        candidate=dict(parameters)
        for key in keys:
            index,remainder=divmod(index,len(grid[key]));candidate[key]=grid[key][remainder]
        identity=json.dumps(candidate,sort_keys=True)
        if identity not in seen:result.append(candidate);seen.add(identity)
        if len(result)>=budget:break
    return result

def windows(request):
    from backtester.walk_forward import walk_forward_windows
    r=request['research']
    if request['mode']=='optimization':
        return [{'index':0,'fit_start':request['start'],'validation_start':r['fit_end'],'test_start':r['validation_end'],'test_end':request['end']}]
    return [{k:day(v) if isinstance(v,datetime) else v for k,v in vars(w).items()}
            for w in walk_forward_windows(stamp(request['start']),stamp(request['end']),
                fit_months=r['fit_months'],validation_months=r['validation_months'],test_months=r['test_months'])]

def validate_research(request,raw):
    from backtest_jobs import ENGINE
    from backtester.strategy_loader import load_strategy
    mode=request['mode']
    if mode not in ('optimization','walk_forward'):raise ValueError('Неизвестный режим исследования')
    if request['strategy']!='s019':raise ValueError('Оптимизация и walk-forward сейчас доступны для S-019 M20')
    if not isinstance(raw,dict):raise ValueError('Задайте настройки исследования')
    common={'grid','trials','shortlist','seed','min_fit_trades','min_validation_trades'}
    specific={'fit_end','validation_end'} if mode=='optimization' else {'fit_months','validation_months','test_months'}
    if set(raw)-(common|specific):raise ValueError('Неизвестные настройки исследования')
    resolved={}
    limits={'trials':(2,32,12),'shortlist':(1,5,3),'seed':(1,2147483645,20261015),
            'min_fit_trades':(1,1000,30),'min_validation_trades':(1,1000,10)}
    if mode=='walk_forward':limits.update(fit_months=(1,36,18),validation_months=(1,12,6),test_months=(1,12,6))
    for key,(low,high,default) in limits.items():
        value=raw.get(key,default)
        if type(value) is not int or not low<=value<=high:raise ValueError(f'{key}: целое число от {low} до {high}')
        resolved[key]=value
    if resolved['shortlist']>resolved['trials']:raise ValueError('Финалистов не может быть больше кандидатов')
    grid=raw.get('grid',GRID)
    if not isinstance(grid,dict) or not grid or set(grid)-set(GRID):raise ValueError('Сетка может содержать только поддерживаемые параметры ADR, входа, RR, EMA и срока заявки')
    for key,values in grid.items():
        if not isinstance(values,list) or not 1<=len(values)<=8:raise ValueError(f'{key}: задайте от 1 до 8 значений')
        for value in values:
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<value<=10000:raise ValueError(f'{key}: нужны положительные конечные числа ≤ 10000')
            if key in INTEGER_KEYS and type(value) is not int:raise ValueError(f'{key}: нужны целые числа')
        if len(set(values))!=len(values):raise ValueError(f'{key}: значения повторяются')
    resolved['grid']=grid
    if mode=='optimization':
        import re
        for key in specific:
            value=raw.get(key,'')
            if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('Укажите границы обучения и валидации YYYY-MM-DD')
            stamp(value);resolved[key]=value
        if not request['start']<resolved['fit_end']<resolved['validation_end']<request['end']:raise ValueError('Нужно: начало < конец обучения < конец валидации < конец теста')
    request['research']=resolved
    plan=windows(request)
    if not 1<=len(plan)<=12:raise ValueError('Период должен содержать от 1 до 12 тестовых окон; измените даты или длины окон')
    with load_strategy(str(ENGINE/'strategies/s019_v0_4_9.py'),'ticks') as loaded:
        for key,values in grid.items():
            for value in values:loaded.factory(**{**request['parameters'],key:value})
        for params in candidates(request['parameters'],grid,resolved['trials'],resolved['seed']):loaded.factory(**params)
    return request

def score(summary,risk):
    profit=summary['final_equity']-summary['initial_cash']
    return {'net_profit':profit,'max_drawdown':summary['max_drawdown'],
            'max_drawdown_money':summary['max_drawdown_usd'],'closed_trades':summary['closed_trades'],
            'recovery':profit/max(risk,summary['max_drawdown_usd']), 'profit_factor':summary.get('profit_factor')}

def execute_research(job,store,simulate,manifest,status):
    from backtester.models import BacktestResult
    from backtester.reporting import write_report
    request=job['request'];r=request['research'];plan=windows(request)
    output=store.root/'runs'/job['id'];output.mkdir(parents=True,exist_ok=False)
    pool=candidates(request['parameters'],r['grid'],r['trials'],r['seed'])
    seeds=[request['parameters']['InpEntryDelaySeed']+i for i in range(3)]
    risk=float(request['parameters'].get('InpRiskMoney',100))
    total=len(plan)*(len(pool)+3*min(len(pool),r['shortlist'])+1)+1;done=0
    protocol={'version':'atlas-research-v1','job_id':job['id'],'request':request,'windows':plan,
              'dataset':manifest.model_dump(mode='json'),'candidate_parameters':pool,'validation_seeds':seeds,
              'research_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'simulation_source_sha256':hashlib.sha256((Path(__file__).parent/'backtest_worker.py').read_bytes()).hexdigest(),
              'selection':'Baseline + deterministic random grid; positive fit profit and minimum closed trades; shortlist by recovery. Validation: median minimum trades and at least 2 of 3 positive seeds; maximize median recovery. Ties resolved by candidate order.',
              'score':'net_profit / max(fixed risk money, maximum monetary drawdown)',
              'warmup':'Calendar days before each evaluation; indicators only, no trades. Fit and validation each start with a fresh account.',
              'oos':'Parameters locked before OOS. Continuous balance, daily opening counters and drawdown across WFA windows. Old protections execute on first new-window quote, then close/cancel before rewarming new strategy. No eligible candidate means cash. Final positions marked, not liquidated.',
              'baseline':'One ordinary continuous backtest of submitted parameters over the identical OOS dates, capital, costs, warmup and execution seed.',
              'limitations':'Historical research; these data may have been inspected previously. No guarantee of untouched out-of-sample data. News disabled, fixed tick value; no swaps or FX conversion.',
              'maximum_simulations':total}
    dump(output/'protocol.json',protocol)
    locks=[];all_rankings=[];account=None;points=[];last_result=None;first_metadata=None;tick_count=0
    initial_equity=request['initial_cash'];previous_equity=initial_equity
    def run(params,start,end,label,**kwargs):
        nonlocal done
        trial={**request,'mode':'backtest','parameters':params,'start':start,'end':end}
        trial.pop('research',None)
        def progress(stage,value):status(f'{label} · {stage}',2+95*(done+value/100)/total)
        status(label,2+95*done/total)
        result,metadata,next_account=simulate(trial,progress,manifest=manifest,**kwargs)
        done+=1
        return result,metadata,next_account
    def record(row):
        with (output/'trials.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
    for w in plan:
        prefix=f"Окно {w['index']+1}/{len(plan)}";fitted=[];ranking=[]
        for i,params in enumerate(pool):
            result,_,_=run(params,w['fit_start'],w['validation_start'],f'{prefix} · обучение {i+1}/{len(pool)}',collect=False)
            m=score(result.summary(),risk);record({'window':w['index'],'phase':'fit','candidate':i,'parameters':params,'start':w['fit_start'],'end':w['validation_start'],**m})
            if m['closed_trades']>=r['min_fit_trades'] and m['net_profit']>0:fitted.append((i,m))
        finalists=sorted(fitted,key=lambda item:(-item[1]['recovery'],item[0]))[:r['shortlist']]
        for i,fit in finalists:
            validations=[]
            for seed in seeds:
                params={**pool[i],'InpEntryDelaySeed':seed}
                result,_,_=run(params,w['validation_start'],w['test_start'],f'{prefix} · валидация кандидата {i+1} · seed {seed}',collect=False)
                m=score(result.summary(),risk);validations.append(m)
                record({'window':w['index'],'phase':'validation','candidate':i,'seed':seed,'start':w['validation_start'],'end':w['test_start'],**m})
            eligible=statistics.median(m['closed_trades'] for m in validations)>=r['min_validation_trades'] and sum(m['net_profit']>0 for m in validations)>=2
            ranking.append({'candidate':i,'parameters':pool[i],'fit':fit,'validation':validations,'eligible':eligible,
                            'validation_score':statistics.median(m['recovery'] for m in validations)})
        ranking.sort(key=lambda row:(not row['eligible'],-row['validation_score'],row['candidate']))
        winner=next((row for row in ranking if row['eligible']),None)
        lock={**w,'selected_candidate':winner['candidate'] if winner else None,
              'parameters':winner['parameters'] if winner else None,'decision':'selected' if winner else 'cash',
              'locked_at':datetime.now(timezone.utc).isoformat(),'ranking':ranking}
        locks.append(lock);dump(output/'selection-locks.json',locks) # persisted BEFORE OOS
        all_rankings.append({'window':w['index'],'ranking':ranking,'fit_eligible':len(fitted)})
        params=winner['parameters'] if winner else request['parameters']
        result,metadata,account=run(params,w['test_start'],w['test_end'],f'{prefix} · тест OOS',account=account,cash=winner is None)
        if first_metadata is None:first_metadata=metadata
        points.extend(result.equity);last_result=result;tick_count+=result.summary()['ticks_processed']
        equity=result.summary()['final_equity']
        # Keep the pre-OOS locks immutable; measured outcomes are in a separate file.
        all_rankings[-1].update({**w,'decision':lock['decision'],'selected_candidate':lock['selected_candidate'],
                                'parameters':lock['parameters'],'oos_profit':equity-previous_equity,'ending_equity':equity})
        previous_equity=equity;dump(output/'windows.json',all_rankings)
    oos_start=plan[0]['test_start'];oos_end=plan[-1]['test_end']
    baseline,baseline_metadata,_=run(request['parameters'],oos_start,oos_end,'Обычный бэктест · тот же OOS период')
    status('Сохранение сравнения',97)
    @dataclass(frozen=True)
    class ResearchResult(BacktestResult):
        statistics:dict=field(default_factory=dict)
        def summary(self):return {**BacktestResult.summary(self),**self.statistics}
    chosen=ResearchResult(last_result.config,tuple(points),last_result.fills,last_result.final_signal_unfilled,
                          'ticks',last_result.execution_metadata,last_result.events,
                          {'ticks_processed':tick_count,'max_drawdown':account['state'].max_drawdown,
                           'max_drawdown_usd':account['state'].max_drawdown_usd,'closed_trades':len(last_result.fills)//2})
    comparison={'mode':request['mode'],'oos_start':oos_start,'oos_end':oos_end,
                'selected':chosen.summary(),'baseline':baseline.summary(),'windows':all_rankings,
                'cash_windows':sum(w['decision']=='cash' for w in locks),'simulations':done}
    metadata={**first_metadata,'request':request,'job_id':job['id'],'period':{'start_inclusive':oos_start,'end_exclusive':oos_end},
              'research':comparison,'protocol_file':'protocol.json','selection_locks_file':'selection-locks.json'}
    write_report(chosen,output/'selected',metadata)
    write_report(baseline,output/'baseline',baseline_metadata)
    for path in (output/'selected').iterdir():path.rename(output/('selected-report.html' if path.name=='report.html' else path.name))
    (output/'selected').rmdir()
    for path in (output/'baseline').iterdir():path.rename(output/('baseline-'+path.name))
    (output/'baseline').rmdir()
    dump(output/'comparison.json',comparison)
    with (output/'trials.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=['window','phase','candidate','seed','start','end','net_profit','max_drawdown','max_drawdown_money','closed_trades','recovery','profit_factor'],extrasaction='ignore');writer.writeheader()
        for line in (output/'trials.jsonl').read_text().splitlines():writer.writerow(json.loads(line))
    write_comparison_report(output,chosen,baseline,comparison,protocol)
    status('Сравнение готово',99)
    return {**chosen.summary(),'mode':request['mode'],'research':comparison}

def write_comparison_report(output,chosen,baseline,comparison,protocol):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from backtester.reporting import _chart_series
    fig=make_subplots(rows=2,cols=1,shared_xaxes=True,subplot_titles=('Equity · валюта счёта','Просадка · %'),vertical_spacing=.12)
    label='Оптимизация OOS' if comparison['mode']=='optimization' else 'Walk-forward OOS'
    for result,name,color in ((chosen,label,'#55d6b4'),(baseline,'Обычный бэктест · исходные параметры','#81a7ff')):
        series=_chart_series(result)
        for row,column in ((1,1),(2,2)):
            fig.add_trace(go.Scatter(x=[p[0] for p in series],y=[p[column] for p in series],name=name,legendgroup=name,showlegend=row==1,line=dict(color=color)),row=row,col=1)
    fig.update_layout(template='plotly_dark',height=760,margin=dict(l=60,r=30,t=60,b=40))
    rows=''.join(f"<tr><td>{html.escape(name)}</td><td>{s['final_equity']:,.2f}</td><td>{s['total_return']*100:.2f}%</td><td>{s['max_drawdown']*100:.2f}%</td><td>{s['fill_count']}</td></tr>" for name,s in ((label,chosen.summary()),('Обычный бэктест',baseline.summary())))
    windows_rows=''.join(f"<tr><td>{w['window']+1}</td><td>{w['test_start']} → {w['test_end']}</td><td>{'Кэш: нет прошедших отбор' if w['decision']=='cash' else 'Кандидат '+str(w['selected_candidate']+1)}</td><td>{w['oos_profit']:,.2f}</td></tr>" for w in comparison['windows'])
    text=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{label} · сравнение</title><style>body{{background:#101620;color:#dce8f2;font:16px system-ui;margin:32px auto;padding:0 24px;max-width:1250px}}a{{color:#81a7ff}}table{{border-collapse:collapse;width:100%;margin:24px 0}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #34404d}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}p{{line-height:1.6}}</style><h1>{label} / обычный бэктест</h1><p>Единый тестовый период: {comparison['oos_start']} → {comparison['oos_end']} UTC, конец не включительно. Одинаковые начальный капитал, расходы, seed и котировки. Обучение и валидация исключены из итоговой доходности.</p><table><tr><th>Вариант</th><th>Equity</th><th>Доходность</th><th>Макс. просадка</th><th>Исполнения</th></tr>{rows}</table>{fig.to_html(full_html=False,include_plotlyjs=True)}<h2>Тестовые окна</h2><table><tr><th>Окно</th><th>OOS · UTC</th><th>Решение до теста</th><th>PnL</th></tr>{windows_rows}</table><p>При смене окна старая позиция закрывается после обработки защитных приказов на первой котировке. Расходы учитываются; капитал и максимум equity сохраняются. В конце теста открытая позиция оценена по рынку. Без прошедшего отбор кандидата окно остаётся в кэше. История уже могла использоваться в других исследованиях.</p><p><a href="selected-report.html">Подробный отчёт исследования</a> · <a href="baseline-report.html">Подробный обычный бэктест</a> · <a href="trials.csv">Все попытки CSV</a> · <a href="selection-locks.json">Зафиксированные параметры</a> · <a href="comparison.json">Сравнение JSON</a></p><details><summary>Протокол и настройки</summary><pre>{html.escape(json.dumps(protocol,ensure_ascii=False,indent=2))}</pre></details></html>'''
    (output/'report.html').write_text(text)
