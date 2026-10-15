"""Overlay research UI/worker only; preserve the current dashboard and proxy config."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','deploy']);parser.add_argument('--release',default='20261015-research-01');args=parser.parse_args()
base=Path('/opt/atlas-workspace');stage=Path('/opt/atlas-backtester/research-stage');release=base/'releases'/args.release
names=['backtest_jobs.py','backtest_api.py','backtest_worker.py','backtest_research.py','backtests.js','backtests.css']
def run(*args):subprocess.run(args,check=True,stdout=subprocess.DEVNULL)
if args.action=='prepare':
    previous=os.readlink(base/'current');shutil.copytree(base/'current',release)
    for name in names:shutil.copyfile(stage/name,release/name)
    (release/'research-previous.json').write_text(json.dumps({'previous':previous}))
    run('chown','-R','root:dashboard',str(release))
    for p in release.rglob('*'):p.chmod(0o750 if p.is_dir() else 0o640)
    release.chmod(0o750);print(json.dumps({'prepared':str(release),'previous':previous}));raise SystemExit()
previous=json.loads((release/'research-previous.json').read_text())['previous']
assert os.readlink(base/'current')==previous,'Live release changed; prepare a fresh release'
backup=Path('/var/backups/atlas-workspace')/args.release;backup.mkdir(mode=0o700)
rollback=backup/'rollback.sh';rollback.write_text(f'#!/bin/sh\nset -eu\nsystemctl stop atlas-workspace atlas-backtest-worker\nln -sfn {previous} /opt/atlas-workspace/current\nsystemctl start atlas-workspace atlas-backtest-worker\n');rollback.chmod(0o700)
# Stop accepting new jobs, then refuse a cutover if any existing job needs the worker.
run('systemctl','stop','atlas-workspace')
try:
    with sqlite3.connect('/var/lib/atlas-backtests/jobs.sqlite3') as conn:
        assert conn.execute("select count(*) from jobs where status in ('queued','running')").fetchone()[0]==0,'Jobs active; wait before deployment'
except BaseException:
    run('systemctl','start','atlas-workspace');raise
try:
    run('systemctl','stop','atlas-backtest-worker')
    link=base/'current.research-next';link.symlink_to('releases/'+args.release);os.replace(link,base/'current')
    run('systemctl','start','atlas-workspace','atlas-backtest-worker')
    for attempt in range(40):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18841/api/backtest/catalog',timeout=3) as response:
                assert 'walk_forward' in json.load(response)['research']['modes']
            break
        except Exception:
            if attempt==39:raise
            time.sleep(.25)
    run('systemctl','is-active','atlas-workspace','atlas-backtest-worker','mt5-dashboard','caddy')
    for path in ['/','/backtests.js','/api/backtest/jobs','/api/backtest/catalog','/api/logs']:
        code=subprocess.check_output(['curl','--silent','--output','/dev/null','--write-out','%{http_code}','https://46.225.132.195'+path],text=True)
        assert code=='401',(path,code)
    print(json.dumps({'release':args.release,'rollback':str(rollback),'services':'active','auth':'401'}))
except BaseException:
    subprocess.run([str(rollback)],check=True);raise
