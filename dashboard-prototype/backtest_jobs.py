"""Durable bounded job queue shared by the HTTP API and a single worker."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
ENGINE = Path(os.environ.get('ATLAS_ENGINE_ROOT', ROOT.parent / 's019-wfa-5y-review'))
RUNTIME = Path(os.environ.get('ATLAS_BACKTEST_STATE', ROOT.parent / 'runtime'))
DATA = Path(os.environ.get('ATLAS_TICK_DATA', RUNTIME / 'data/deuidxeur'))
ARTIFACTS = {'report.html': 'text/html', 'result.json': 'application/json', 'fills.csv': 'text/csv',
             'equity.csv': 'text/csv', 'daily_pnl.csv': 'text/csv', 'daily_pnl_server.csv': 'text/csv', 'events.csv': 'text/csv'}

ARTIFACTS.update({name:'application/json' for name in ['protocol.json','selection-locks.json','windows.json','comparison.json','baseline-result.json']})
ARTIFACTS.update({name:'text/csv' for name in ['trials.csv','baseline-fills.csv','baseline-equity.csv','baseline-daily_pnl.csv','baseline-daily_pnl_server.csv','baseline-events.csv']})
ARTIFACTS.update({'trials.jsonl':'application/x-ndjson','selected-report.html':'text/html','baseline-report.html':'text/html'})
AUDIT_ARTIFACTS={'protocol.json','selection-locks.json','windows.json','trials.jsonl'}

def now():
    return datetime.now(timezone.utc).isoformat()

def catalog():
    defaults=json.loads((ENGINE / 'parameters/s019-mt5-report-no-news.json').read_text())
    defaults['InpEntryDelaySeed']=190300
    manifest=json.loads((DATA / 'manifest.json').read_text()) if (DATA / 'manifest.json').is_file() else None
    return {'strategies': [
        {'id':'s019', 'name':'S-019 v0.4.9 · M20 · Bid/Ask', 'parameters':defaults,
         'description':'Тиковое исполнение. Один контракт, одна позиция. Новости выключены; стоимость тика постоянна. Спецификация FivePercent DAX40, не подтверждена для других брокеров.'},
        {'id':'sma', 'name':'SMA crossover · M20 · бары Bid', 'parameters':{'fast':10,'slow':30},
         'description':'Базовая long/flat стратегия. Сигнал на закрытии M20, исполнение на следующем открытии. Спред задаётся в bps; модель в единицах инструмента, не CFD-лотах.'}],
        'dataset':None if manifest is None else {k:manifest[k] for k in ('dataset_id','spec','rows','first_timestamp','last_timestamp')},
        'ready':manifest is not None, 'max_queued':5,
        'research':{'modes':['optimization','walk_forward'],'strategy':'s019','max_trials':32,'max_windows':12,
                    'grid':__import__('backtest_research').GRID}}

def validate(payload):
    import sys
    sys.path.insert(0,str(ENGINE / 'src')) if str(ENGINE / 'src') not in sys.path else None
    from backtester.models import BacktestConfig
    from backtester.strategy_loader import load_strategy
    if not isinstance(payload,dict) or set(payload)-{'strategy','start','end','parameters','initial_cash','commission_bps','slippage_bps','spread_bps','warmup_days','mode','research'}:
        raise ValueError('Неизвестные поля запроса')
    kind=payload.get('strategy')
    if kind not in ('s019','sma'): raise ValueError('Выберите доступную стратегию')
    def boundary(key):
        raw=payload.get(key,'')
        if not isinstance(raw,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw): raise ValueError('Даты должны быть YYYY-MM-DD (UTC)')
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    start,end=boundary('start'),boundary('end')
    if start>=end: raise ValueError('Начало должно быть раньше конца периода')
    cat=catalog()
    if not cat['ready']: raise ValueError('Тиковая история ещё не подготовлена')
    ds=cat['dataset']; first=datetime.fromisoformat(ds['first_timestamp']); last=datetime.fromisoformat(ds['last_timestamp'])
    from datetime import timedelta
    if start.date()<first.date() or end>datetime.combine(last.date()+timedelta(days=1),datetime.min.time(),timezone.utc) or start>last:
        raise ValueError('Период выходит за доступную историю')
    warmup=payload.get('warmup_days',180)
    if type(warmup) is not int or not 0<=warmup<=730: raise ValueError('Прогрев: целое число от 0 до 730 дней')
    params=payload.get('parameters')
    if not isinstance(params,dict): raise ValueError('Параметры должны быть JSON-объектом')
    config=BacktestConfig(initial_cash=payload.get('initial_cash',100000),commission_bps=payload.get('commission_bps',0),
                          slippage_bps=payload.get('slippage_bps',0),spread_bps=payload.get('spread_bps',0),price_type='bid_ask' if kind=='s019' else 'bid')
    spec=str(ENGINE/'strategies/s019_v0_4_9.py') if kind=='s019' else 'sma'
    with load_strategy(spec,'ticks' if kind=='s019' else 'bars') as loaded:
        strategy=loaded.factory(**params)
        if kind=='s019':
            if strategy.seconds!=1200: raise ValueError('В панели сейчас поддерживается S-019 на M20')
            if strategy.broker_spec.symbol!=ds['spec']['symbol']: raise ValueError('Контракт не совпадает с набором данных')
            if config.spread_bps: raise ValueError('В тиках уже есть Bid/Ask: дополнительный спред должен быть 0')
            if not params.get('InpEntryDelaySeed'): raise ValueError('Укажите положительный seed для повторяемого расчёта')
        elif strategy.slow>10000: raise ValueError('SMA slow должен быть не больше 10000')
    from dataclasses import asdict
    request={'strategy':kind,'start':payload['start'],'end':payload['end'],'parameters':params,
            'warmup_days':warmup,**{k:v for k,v in asdict(config).items() if k!='price_type'}}
    request['mode']=payload.get('mode','backtest')
    if request['mode']!='backtest':
        from backtest_research import validate_research
        return validate_research(request,payload.get('research'))
    if 'research' in payload:raise ValueError('Настройки исследования не относятся к обычному бэктесту')
    return request

class Jobs:
    def __init__(self,root=RUNTIME):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created TEXT, updated TEXT, status TEXT, request TEXT, progress REAL DEFAULT 0, stage TEXT, error TEXT, summary TEXT, cancel INTEGER DEFAULT 0)')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.root/'jobs.sqlite3',timeout=15); c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def rows(self):
        with self.connect() as c: return [self.decode(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]
    @staticmethod
    def decode(row):
        d=dict(row)
        for key in ('request','summary'): d[key]=json.loads(d[key]) if d[key] else None
        return d
    def get(self,id):
        if not re.fullmatch('[0-9a-f]{32}',id): raise KeyError(id)
        with self.connect() as c: row=c.execute('SELECT * FROM jobs WHERE id=?',(id,)).fetchone()
        if row is None: raise KeyError(id)
        return self.decode(row)
    def submit(self,request):
        id=uuid4().hex
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=5: raise ValueError('Очередь заполнена: дождитесь завершения расчёта')
            c.execute('INSERT INTO jobs(id,created,updated,status,request,stage) VALUES(?,?,?,?,?,?)',(id,now(),now(),'queued',json.dumps(request,allow_nan=False),'В очереди'))
        return self.get(id)
    def update(self,id,**values):
        if set(values)-{'status','progress','stage','error','summary','cancel'}: raise ValueError('Invalid update')
        if 'summary' in values: values['summary']=json.dumps(values['summary'],allow_nan=False)
        values['updated']=now()
        with self.connect() as c: c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),id))
    def claim(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:return None
            c.execute("UPDATE jobs SET status='running',stage='Подготовка',updated=? WHERE id=?",(now(),row['id']))
        return self.get(row['id'])
    def cancel(self,id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT status FROM jobs WHERE id=?',(id,)).fetchone()
            if row is None:raise KeyError(id)
            if row['status']=='queued':c.execute("UPDATE jobs SET status='cancelled',stage='Отменён',cancel=1,updated=? WHERE id=?",(now(),id))
            elif row['status']=='running':c.execute('UPDATE jobs SET cancel=1,updated=? WHERE id=?',(now(),id))
        return self.get(id)
