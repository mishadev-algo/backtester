"""Prepare/cut over the backtest integration without overwriting other live features."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['prepare','deploy'])
parser.add_argument('--release',default='20261015-backtests-01')
args=parser.parse_args()
base=Path('/opt/atlas-workspace'); staging=Path('/opt/atlas-backtester/dashboard')
release=base/'releases'/args.release;backup=Path('/var/backups/atlas-workspace')/args.release

def run(*a):subprocess.run(a,check=True,stdout=subprocess.DEVNULL)

if args.action=='prepare':
    previous=os.readlink(base/'current')
    shutil.copytree(base/'current',release)
    (release/'previous-release.json').write_text(json.dumps({'previous':previous}))
    for name in ['backtest_api.py','backtest_jobs.py','backtest_worker.py','backtests.js','backtests.css','serve.py','serve_live.py']:
        shutil.copyfile(staging/name,release/name)
    p=release/'index.html';s=p.read_text().replace('</head>','<link rel="stylesheet" href="backtests.css">\n</head>').replace('<script src="mapping-ui.js"></script>','<script src="mapping-ui.js"></script>\n<script src="backtests.js"></script>');p.write_text(s)
    p=release/'real.js';s=p.read_text().replace("let page=state.page==='logs'", "let page=state.page==='backtests'&&typeof realBacktests==='function'?realBacktests():state.page==='logs'")
    s=s.replace('Запись в VPS отключена','Торговые данные · только чтение; бэктесты · отдельный расчёт');p.write_text(s)
    run('chown','-R','root:dashboard',str(release))
    for p in release.rglob('*'):p.chmod(0o750 if p.is_dir() else 0o640)
    release.chmod(0o750)
    print(json.dumps({'prepared':str(release),'previous':previous}));raise SystemExit()

previous=json.loads((release/'previous-release.json').read_text())['previous']
assert os.readlink(base/'current')==previous,'Live release changed after preparation; prepare again'
assert not backup.exists()
backup.mkdir(mode=0o700)
caddy=Path('/etc/caddy/Caddyfile');service=Path('/etc/systemd/system/atlas-workspace.service')
shutil.copy2(caddy,backup/'Caddyfile.before');shutil.copy2(service,backup/'atlas-workspace.service.before')
rollback=backup/'rollback.sh'
rollback.write_text(f'''#!/bin/sh
set -eu
systemctl disable --now atlas-backtest-worker || true
ln -sfn {previous} /opt/atlas-workspace/current
cp {backup}/Caddyfile.before /etc/caddy/Caddyfile
cp {backup}/atlas-workspace.service.before /etc/systemd/system/atlas-workspace.service
systemctl daemon-reload
caddy validate --config /etc/caddy/Caddyfile
systemctl restart atlas-workspace
systemctl reload caddy
''');rollback.chmod(0o700)
try:
    old=caddy.read_text();lines=old.splitlines()
    matching=[i for i,l in enumerate(lines) if l.strip().startswith('@atlas path ')]
    assert len(matching)==1
    lines[matching[0]]+=' /backtests.js /backtests.css /api/backtest/*'
    caddy.write_text('\n'.join(lines)+'\n')
    run('caddy','validate','--config',str(caddy))
    shutil.copy2(staging/'atlas-workspace.service',service)
    shutil.copy2(staging/'atlas-backtest-worker.service','/etc/systemd/system/atlas-backtest-worker.service')
    tmp=base/'current.backtests-next';tmp.symlink_to('releases/'+args.release);os.replace(tmp,base/'current')
    run('systemctl','daemon-reload');run('systemctl','restart','atlas-workspace')
    run('systemctl','enable','--now','atlas-backtest-worker')
    for attempt in range(40):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18841/api/backtest/catalog',timeout=5) as r:assert json.load(r)['ready']
            break
        except Exception:
            if attempt==39:raise
            time.sleep(.25)
    run('systemctl','reload','caddy')
    for path in ['/','/api/backtest/catalog','/api/backtest/jobs','/backtests.js','/api/data','/api/logs','/legacy']:
        code=subprocess.check_output(['curl','--silent','--output','/dev/null','--write-out','%{http_code}','https://46.225.132.195'+path],text=True)
        assert code=='401',(path,code)
    run('systemctl','is-active','atlas-workspace','atlas-backtest-worker','mt5-dashboard','caddy')
    print(json.dumps({'release':args.release,'previous':previous,'protected_routes':True,'rollback':str(rollback)}))
except BaseException:
    subprocess.run([str(rollback)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    raise
