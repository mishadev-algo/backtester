# Live ATLAS deployment — 2026-10-15

- Live: https://46.225.132.195/ (existing Caddy Basic Auth unchanged).
- Previous dashboard: https://46.225.132.195/legacy; existing `/accounts`, `/eas`, `/portfolio`, `/strategy`, `/backtests` and backtest import routes remain on the original service.
- Release: `/opt/atlas-workspace/releases/20261015-04`; current symlink: `/opt/atlas-workspace/current`.
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
