"""Bounded, read-only log queries. Filter values are encoded data, never SQL code."""
import base64
from datetime import date, datetime
import json
from pathlib import Path
import subprocess


def parse_filters(params):
    result = {}
    for name in ('host', 'terminal', 'stream', 'strategy', 'severity'):
        value = json.loads(params.get(name, ['null'])[0])
        if value is not None and (not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or len(v) > 200 for v in value)):
            raise ValueError('Invalid selection')
        if name == 'stream' and value is not None and any(v not in ('journal', 'experts') for v in value):
            raise ValueError('Invalid stream')
        if name == 'severity' and value is not None and any(v not in ('error', 'warning', 'info') for v in value):
            raise ValueError('Invalid severity')
        result[name] = value
    result['legacy_terminal'] = params.get('legacy_terminal', [''])[0]
    if len(result['legacy_terminal']) > 200: raise ValueError('Invalid terminal')
    result['q'] = params.get('q', [''])[0].strip()
    if len(result['q']) > 200: raise ValueError('Search too long')
    for name in ('start', 'end'):
        value = params.get(name, [''])[0]
        if value and date.fromisoformat(value).isoformat() != value: raise ValueError('Invalid date')
        result[name] = value
    if result['start'] and result['end'] and result['start'] > result['end']: raise ValueError('Invalid period')
    result['page'] = int(params.get('page', ['0'])[0])
    if not 0 <= result['page'] <= 10000: raise ValueError('Invalid page')
    result['before'] = params.get('before', [''])[0]
    if result['before']:
        dt = datetime.fromisoformat(result['before'].replace('Z', '+00:00'))
        if dt.utcoffset() is None: raise ValueError('Timezone required')
    return result


def query(filters):
    encoded = base64.b64encode(json.dumps(filters, ensure_ascii=False).encode()).decode('ascii')
    # The token alphabet has no SQL quote or backslash. Decoding happens in SQL as JSON data.
    return (Path(__file__).parent / 'read_logs.sql').read_text().replace('__FILTERS_BASE64__', encoded).replace('__LOG_CLASSIFICATION__', (Path(__file__).parent / 'classify_logs.sql').read_text())


def read_logs(filters, command):
    output = subprocess.run(command, input=query(filters), capture_output=True, text=True, timeout=20)
    if output.returncode: raise RuntimeError('Log query unavailable')
    data = json.loads(output.stdout)
    if not isinstance(data.get('rows'), list) or not isinstance(data.get('terminals'), list):
        raise RuntimeError('Invalid logs response')
    return data
