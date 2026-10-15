# Live ATLAS deployment — 2026-10-15

- Live: https://46.225.132.195/ (existing Caddy Basic Auth unchanged).
- Previous dashboard: https://46.225.132.195/legacy; existing `/accounts`, `/eas`, `/portfolio`, `/strategy`, `/backtests` and backtest import routes remain on the original service.
- Release: `/opt/atlas-workspace/releases/20261015-backtests-01`; current symlink: `/opt/atlas-workspace/current`.
- Service: `atlas-workspace.service`, enabled, OS user `dashboard`, loopback `127.0.0.1:18841`.
- Data: fixed `read_snapshot.sql` via local PostgreSQL peer authentication, `REPEATABLE READ READ ONLY`, rollback; no SSH or stored DB password. Initial GET cache max 30 seconds; explicit refresh throttled to 10 seconds. No autonomous browser polling.
- Imported 1,443 previously accepted per-record mappings from the browser audit, verified against source evidence. They seed browsers without existing local overrides. No new candidates automatically accepted.
- Local attribution overrides remain per browser; source database values are unchanged. Exclusions apply independent of local attribution settings.
- UI retains explicitly labelled demo workflows. This release does not introduce strategy execution, automated live trading or backtest computation.

## Verification

Unit checks passed for mapping/exclusion conflicts, source immutability, comparison filters, read-only SQL, origin/host restrictions and static-file allowlists.
Server-rendered assets and real API verified in browser through a temporary SSH tunnel: 13 labelled groups, 1,443 approved mappings, 202 excluded records, 1,651 included records and net PnL -4,839.37 USD at the verification snapshot. Two-account multi-select matched -218.52 USD. Comparison page displayed actual backtest/account matches.

After cutover: valid public TLS; unauthenticated `/`, `/api/data`, `/live-config.js`, `/legacy`, `/backtests` return 401; `/health` returns 200; unauthenticated collector POSTs return 401. Original ingest/alerts service and Caddy remain active. Public-IP navigation is blocked by the embedded browser, so no authenticated public-browser visual verification was possible there.

## Rollback

Run on the VPS as root:

```sh
/var/backups/atlas-workspace/20261015-01/rollback.sh
```

This restores the preserved Caddy configuration, validates/reloads Caddy, and disables/stops only the new ATLAS UI service. The original dashboard source/service and database were not replaced. Backup directory is root-only. The release and seeded mapping file stay behind existing authentication; they must not be published as public static assets.

## Future releases

Use a new release directory and backup ID. `package_live.js` includes only allowlisted source/assets and validates already-approved decisions from an explicit browser audit. Never archive `private/`, raw database snapshots or credentials. `prepare_proxy.py` and `cutover.py` describe the initial cutover and deliberately refuse unexpected proxy state; do not rerun unchanged after deployment.

## Update 20261015-02 — account toggle in trades

Account numbers in the Strategy/Account column now toggle the account filter. A second click restores all accounts; other dimensions stay selected. Verified live upstream round trip: 1,651 rows / -4,839.37 USD → account 414738707: 94 rows / -1,413.05 USD → full selection restored. Existing data API and 1,443 approved mappings retained. Only the UI service restarted; Caddy/ingest/alerts were not changed.

Rollback this update to release 01: `/var/backups/atlas-workspace/20261015-02/rollback.sh` on the VPS.

## Update 20261015-03 — restore Logs navigation

Added a «Логи» link under Data in the sidebar, pointing to the existing protected `/logs` page. The original logs backend and routes are unchanged. Verified backend HTTP 200 and public unauthenticated HTTP 401. Rollback: `/var/backups/atlas-workspace/20261015-03/rollback.sh`.


## Update 20261015-04 — integrated logs

Logs now render inside the ATLAS workspace at `/#logs`. The protected `/logs` URL redirects there and retains legacy host/terminal/stream/text filters. Caddy forwards `/logs`, `/logs.js` and `/api/logs` to the new UI service behind the unchanged Basic Auth.

The read-only logs API searches all saved `log_events`, with host, composite host+terminal, Journal/Experts multiselects, literal case-insensitive text search, UTC received-date range and 50-row pages. A server timestamp keeps the page cutoff stable until refresh. Raw log messages are HTML-escaped and open in a drawer. Portfolio filters and EA PnL exclusions do not suppress logs. DB reads use a repeatable-read, read-only transaction, 12-second statement timeout and peer authentication.

Validated eight Python tests and existing frontend regression checks; real DB checks covered disjoint pages, composite terminal scope, literal SQL-like search and empty selections. Browser checks covered search, source selection, full-message drawer and the deployed redirect. Deployment probes confirmed real rows, protected routes returning 401 without auth, and all three services active. No log/trade data or collection settings were modified.

Rollback this release: `/var/backups/atlas-workspace/20261015-04/rollback.sh`; restores release 03 and its Caddy configuration. `update_logs.py` is a one-time script requiring release 03 and a fresh backup directory.


## Update 20261015-05 — strategy filters and log diagnosis

Added server-side strategy and severity multiselects, error/warning row colours with text labels, scoped severity counts and a “Только проблемы” shortcut. Strategy identity comes only from valid EA_EVENT_V1 JSON strategy_id, the MT5 EA source name or an explicit message prefix. General terminal events remain “Без стратегии”; no account-based inference or trade attribution is performed.

Severity precedence: explicit structured level, nonzero MT5 severity (1 warning, 2/3 error), then conservative message heuristics. Known ORB range and EMA setup-filter rejections remain informational; benign zero error counters are stripped before heuristic matching. Inferred warnings/errors are labelled “по тексту” and the drawer explains evidence. Classification is for navigation and does not establish the actual execution outcome of a trade.

`classify_logs.sql` uses JSON validation supported by the deployed PostgreSQL 18.6; malformed payloads fall back safely. All classification/filtering happens in a read-only query before pagination; no database schema or records are changed. Classification query measured about 1.2 seconds over 12,735 saved rows through SSH.

Verification: eight Python unit checks, opt-in SQL integration test covering 16 synthetic cases and multiselect/empty filters, frontend serialization/escaping tests and existing statistics regression checks. Real-data two-strategy problem filter (S-015 + S-019) returned 175 records at verification. Production probes confirmed strategy/error scope and protected endpoints remain authenticated. Caddy and collection services were not changed.

Rollback: `/var/backups/atlas-workspace/20261015-05/rollback.sh` returns to release 04. `update_log_filters.py` is a one-time deployment script requiring release 04 and a fresh backup directory.


## Update 20261015-backtests-01 — real historical calculations

Live Backtests now includes a real launch form, durable queue/status/cancellation, job history, HTML charts and CSV/JSON downloads. Imported reports remain in their own tab. Bundled strategies: S-019 v0.4.9 M20 on Bid/Ask and SMA M20 on aggregated Bid bars. UTC start is inclusive and end exclusive. Common parameters have controls; advanced JSON includes the full contract.

The Python environment and engine are in `/opt/atlas-backtester/{venv,engine}`. Dataset and jobs are in `/var/lib/atlas-backtests`; 107,685,090 source quotes were imported with the original gzip and uncompressed CSV SHA256 verified. The source has not been reduced to bars for S-019. API and worker run as dashboard, with separate systemd services. Worker: one job at a time, 80% CPU quota, 1800 MiB memory cap, 2-hour per-job timeout checked between batches. Source data is mounted read-only for the worker. Generated data/reports are not in Git.

`atlas-workspace.service` uses the dedicated Python environment and accepts only fixed backtest endpoints. The existing Caddy authentication covers `/api/backtest/*`, `/backtests.js`, `/backtests.css` and reports. POST requires the permitted Host/Origin. No arbitrary filesystem path, script upload, shell command, or live trade action is accepted. Existing snapshots/logs remain read-only.

Checks: 102 engine tests and 55 subtests pass, including cross-batch fills/drawdown parity. Dashboard queue/origin/artifact tests and worker-to-canonical/report reconciliation pass; frontend regression checks pass. The database fixture test is environment-gated. Real S-019 and SMA September calculations completed and daily PnL reconciled. Browser form submission, persisted job history and Plotly rendering verified through the SSH preview. A post-deploy October S-019 API job completed with 939,349 ticks and 24 fills; foreign Origin returned 403, arbitrary strategy path returned 400, snapshot/log APIs remained healthy. Public unauthenticated routes returned 401. A September S-019 run peaked at approximately 338 MiB RSS/cgroup memory during acceptance.

Prepared from release 20261015-05, preserving its log filters and approved mapping seed. Rollback: `/var/backups/atlas-workspace/20261015-backtests-01/rollback.sh`. This restores the prior UI/service/Caddy config and stops the new worker; stored datasets and reports remain available. `backtests_release.py` checks that the live release has not changed between preparation and deployment.
