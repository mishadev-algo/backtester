# ATLAS backtester

Python historical backtesting engine and ATLAS dashboard integration. The engine source currently retains the review bundle directory name to preserve existing references.

- `s019-wfa-5y-review/`: engine, strategies, tests, parameter profiles and research scripts.
- `dashboard-prototype/`: live dashboard UI, authenticated upstream, persistent job queue and worker.
- `DEUIDXEUR.csv.gz`: original QDM Bid/Ask history managed with Git LFS.

## Local setup

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required for the engine.

```sh
cd s019-wfa-5y-review
uv sync --frozen
uv run pytest
```

Import the verified five-year source (requires several GB of disk; bounded import memory):

```sh
uv run python scripts/import_qdm_5y.py --source ../DEUIDXEUR.csv.gz --output ../runtime/data/deuidxeur
```

Start the dashboard and worker in separate terminals from the repository root:

```sh
s019-wfa-5y-review/.venv/bin/python dashboard-prototype/serve.py --port 18840
s019-wfa-5y-review/.venv/bin/python dashboard-prototype/backtest_worker.py
```

Open `http://127.0.0.1:18840/#backtests`. Historical calculations work independently of the optional SSH-backed live-trading snapshot. `ATLAS_ENGINE_ROOT`, `ATLAS_TICK_DATA`, and `ATLAS_BACKTEST_STATE` override the default engine, imported dataset and runtime paths. The dashboard only accepts bundled strategy IDs; arbitrary code or data paths cannot be submitted through HTTP.

## Verification

```sh
s019-wfa-5y-review/.venv/bin/python -m unittest discover -s dashboard-prototype -p 'test_*.py'
node dashboard-prototype/test_real.js
node dashboard-prototype/test_mapping.js
node dashboard-prototype/test_logs_ui.js
```

The review archive omitted its `examples/` directory. The checked-in small synthetic fixtures reconstruct the original import/CLI test scenarios; they are not production market history.

## Deployment

See `dashboard-prototype/deploy/DEPLOYMENT.md`. Both API and worker run as `dashboard`, behind the existing authenticated proxy. The worker has a memory/CPU limit, processes one job at a time, and accepts at most five pending/running jobs. Queue, parameters and reports persist in `/var/lib/atlas-backtests`. Data is read-only to the worker. Broker trading and database mutation are not part of this integration.

Generated reports, runtime databases, private account snapshots, credentials and screenshots are excluded from Git. Strategy execution assumptions and model limits are documented in `s019-wfa-5y-review/docs/S019_PORT.md`.
