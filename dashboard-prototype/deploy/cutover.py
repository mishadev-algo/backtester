"""Activate validated ATLAS route, preserving authentication; rollback on failed probes."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import urllib.error

backup = Path('/var/backups/atlas-workspace/20261015-01')
config = Path('/etc/caddy/Caddyfile')
assert hashlib.sha256(config.read_bytes()).hexdigest() == (backup / 'original.sha256').read_text(), 'Live config changed after preparation'
with urllib.request.urlopen('http://127.0.0.1:18841/api/data', timeout=30) as r:
    snapshot = json.load(r)
assert snapshot['accounts'] and snapshot['deals'], 'New UI has no data'
subprocess.run(['caddy','validate','--config',str(backup/'Caddyfile.next'),'--adapter','caddyfile'],check=True,capture_output=True)


def status(path, method='GET'):
    req=urllib.request.Request('https://46.225.132.195'+path, method=method)
    try:
        with urllib.request.urlopen(req,timeout=10) as response:return response.status
    except urllib.error.HTTPError as error:return error.code

try:
    stage=config.with_name('Caddyfile.atlas-next')
    shutil.copy2(backup/'Caddyfile.next',stage)
    stage.chmod(config.stat().st_mode & 0o777)
    os.replace(stage,config)
    subprocess.run(['systemctl','reload','caddy'],check=True,capture_output=True)
    checks={path:status(path) for path in ['/', '/api/data', '/live-config.js', '/legacy', '/backtests', '/health']}
    assert all(v==(200 if k=='/health' else 401) for k,v in checks.items()), checks
    for path in ['/v1/ingest','/v1/snapshot']:
        checks[path]=status(path,'POST');assert checks[path]==401, checks
    subprocess.run(['systemctl','enable','atlas-workspace'],check=True,capture_output=True)
    print(json.dumps({'switched':True,'public_checks':checks,'rollback':str(backup/'rollback.sh')}))
except BaseException:
    shutil.copy2(backup/'Caddyfile.before',config)
    subprocess.run(['systemctl','reload','caddy'],check=True,capture_output=True)
    print('Live proxy restored to previous version')
    raise
