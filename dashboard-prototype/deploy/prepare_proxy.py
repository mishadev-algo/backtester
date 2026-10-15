"""Run on VPS as root. Stage proxy config + rollback; do not reload traffic."""
from pathlib import Path
import hashlib
import shutil
import os

release = '20261015-01'
backup = Path('/var/backups/atlas-workspace') / release
backup.mkdir(parents=True, mode=0o700)
os.chmod(backup.parent, 0o700)
os.chmod(backup, 0o700)
config = Path('/etc/caddy/Caddyfile')
old = config.read_text()
assert '127.0.0.1:18841' not in old, 'Already switched; inspect before changing'
original = backup / 'Caddyfile.before'
assert not original.exists(), 'Backup already exists; inspect before changing'
shutil.copy2(config, original)
backend = '''        reverse_proxy 127.0.0.1:8765 {
            header_up -Authorization
            header_up -Proxy-Authorization
        }'''
assert old.count(backend) == 1, 'Unexpected live proxy shape'
new = '''        # ATLAS workspace; inherits the existing basic_auth above.
        @atlas path / /index.html /real.js /real.css /journey.js /journey.css /mapping-model.js /mapping-ui.js /live-config.js /api/data
        handle @atlas {
            reverse_proxy 127.0.0.1:18841 {
                header_up Host 127.0.0.1:18841
                header_up -Authorization
                header_up -Proxy-Authorization
            }
        }
        handle /legacy {
            rewrite * /
            reverse_proxy 127.0.0.1:8765 {
                header_up -Authorization
                header_up -Proxy-Authorization
            }
        }
        handle {
            reverse_proxy 127.0.0.1:8765 {
                header_up -Authorization
                header_up -Proxy-Authorization
            }
        }'''
(backup / 'Caddyfile.next').write_text(old.replace(backend, new))
(backup / 'original.sha256').write_text(hashlib.sha256(config.read_bytes()).hexdigest())
rollback = backup / 'rollback.sh'
rollback.write_text('''#!/bin/sh
set -eu
cp -p /var/backups/atlas-workspace/20261015-01/Caddyfile.before /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
systemctl disable --now atlas-workspace
''')
rollback.chmod(0o700)
print('Proxy candidate and rollback prepared:', backup)
