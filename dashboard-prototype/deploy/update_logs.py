"""One-time release 04 cutover. Run as root on the VPS after uploading assets."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import time
import urllib.request

base=Path('/opt/atlas-workspace')
release=base/'releases/20261015-04'
backup=Path('/var/backups/atlas-workspace/20261015-04')
caddy=Path('/etc/caddy/Caddyfile')
expected='releases/20261015-03'
assert os.readlink(base/'current')==expected, 'Unexpected active release'
assert release.is_dir() and not backup.exists(), 'Missing release or existing backup'
old=caddy.read_text()
route='@atlas path / /index.html /real.js /real.css /journey.js /journey.css /mapping-model.js /mapping-ui.js /live-config.js /api/data'
assert old.count(route)==1
new=old.replace(route,route+' /logs /logs.js /api/logs')
backup.mkdir(mode=0o700)
shutil.copy2(caddy,backup/'Caddyfile.before')
rollback=backup/'rollback.sh'
rollback.write_text('#!/bin/sh\nset -eu\nln -sfn '+expected+' /opt/atlas-workspace/current\ncp '+str(backup/'Caddyfile.before')+' /etc/caddy/Caddyfile\ncaddy validate --config /etc/caddy/Caddyfile\nsystemctl restart atlas-workspace\nsystemctl reload caddy\n')
rollback.chmod(0o700)
def run(*args):
    r=subprocess.run(args,capture_output=True)
    if r.returncode: raise RuntimeError('Command failed: '+args[0])
def get(path):
    with urllib.request.urlopen('http://127.0.0.1:18841'+path,timeout=20) as r:return r.read()
try:
    subprocess.run(['chown','-R','root:dashboard',str(release)],check=True)
    for p in release.iterdir():p.chmod(0o640)
    release.chmod(0o750)
    caddy.write_text(new)
    run('caddy','validate','--config',str(caddy))
    tmp=base/'current.logs-next';tmp.symlink_to('releases/20261015-04');os.replace(tmp,base/'current')
    run('systemctl','restart','atlas-workspace')
    for attempt in range(20):
        try:
            assert json.loads(get('/healthz'))['status']=='ok';break
        except Exception:
            if attempt==19:raise
            time.sleep(.25)
    logs=json.loads(get('/api/logs'))
    assert len(logs['rows'])<=50 and logs['total']>=len(logs['rows'])
    assert b'logs.js' in get('/')
    assert b'function realLogs' in get('/logs.js')
    run('systemctl','reload','caddy')
    for path in ['/logs','/logs.js','/api/logs','/api/data','/legacy']:
        code=subprocess.check_output(['curl','--silent','--output','/dev/null','--write-out','%{http_code}','https://46.225.132.195'+path],text=True)
        assert code=='401',(path,code)
    run('systemctl','is-active','atlas-workspace','mt5-dashboard','caddy')
    print(json.dumps({'release':'20261015-04','logs':logs['total'],'rows':len(logs['rows']),'auth':'401 for protected routes','services':'active'}))
except BaseException:
    subprocess.run([str(rollback)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    raise
