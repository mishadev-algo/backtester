# ATLAS local prototype

Run from this directory with `python3 serve.py --port 18840`.
The server binds only to `127.0.0.1`. Do not publish this preview: it displays private account data.

## VPS data connection

The preview uses an existing authenticated SSH ControlMaster at
`/private/tmp/atlas-vps-readonly.sock`. To reconnect, run this in your own terminal
and enter the SSH password at the prompt (it is not saved by the prototype):

```sh
ssh -M -S /private/tmp/atlas-vps-readonly.sock -N \
  -o StrictHostKeyChecking=yes -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 root@46.225.132.195
```

If a previous master is still active, reuse it. `ATLAS_SSH_SOCKET` can select
another socket. The preview reads the fixed `read_snapshot.sql` through
`sudo -u postgres psql -X --set=ON_ERROR_STOP=1 -qAt -d dashboard`.
Every snapshot runs in a `REPEATABLE READ READ ONLY` transaction and is rolled
back. No code, configuration, services, or tables are changed on the VPS.

Open **Данные VPS** and click **Обновить с VPS** to refresh. Data lives in server
memory; there is no automatic polling or raw database snapshot on disk. A failed
refresh keeps the previous client snapshot with an error banner. Sleep or loss
of SSH may require reconnecting. The original synthetic demo remains available
through the source selector, with separate renderers and data objects.

## Accounting boundaries

- Trading PnL sums profit, commission, swap and fee for `kind=trade`, including
  entry costs. Cash and other operations are excluded. Accounts are grouped by
  currency; deal identity includes broker, login and ticket.
- MT5 deal rows include entries and partial exits; they are not round-trip
  positions. `unmapped` is preserved without inferring a strategy from magic.
- Backtests contain daily aggregates. Return uses imported starting capital;
  drawdown is calculated from successive daily balances and their running peak.
  Neither is historical intraday equity. Parameters and source code are absent.
- Comparison pairs are selected manually or from labelled suggestions based on
  family/instrument names. Suggestions are not confirmed equivalence. The shared period is restricted to
  observed history. Report calendar days and UTC live days may differ; capital,
  lots and version equivalence are not established. No slippage, matched-trade
  rate, or causal attribution is invented from daily aggregates.
- Current balances and positions may be stale and come from different snapshot
  times. Unknown or incomplete terminal state is shown explicitly.

## Checks

```sh
node test_real.js
python3 -m unittest test_serve.py
```

Screenshots of real data belong in ignored `private/`. No credentials are stored
in the source, static assets or browser storage.

## Local strategy attribution

`mapping-model.js` builds conservative family-level candidates from explicit
S-xxx comments, source-backed magic/symbol/comment combinations, and position
chains scoped to broker plus account. Magic alone is never sufficient.
Conflicting identities are withheld; existing named VPS rows are retained.
Local labels deliberately do not assert an EA version.
S-009 source evidence includes `ORB reverse long/short` from
`TryOpenStopReversal`, guarded by the known magic/symbol pair. Its exits can
inherit the family only through the same broker/account/position/symbol.
Unverified S-015 magic variants remain separate unresolved groups even when
comments or execution times resemble known S-015 trades.

The Attribution screen applies per-deal overlays stored only in browser
localStorage, with source fingerprints. Refresh recomputes evidence and withholds
stale or conflicting mappings. Original strategy, ticket and financial values
are retained. Toggle the original view or remove individual groups to undo.
The full JSON audit contains evidence for unresolved as well as proposed rows.

By user request, confirmed QQ, Gold Reaper and GolgGrid/GoldGrid records are
excluded locally from aggregate statistics, PnL, charts, the strategy library,
the trade ledger/CSV and live comparison. `_excluded_ea` is a separate view flag;
raw snapshot fields are immutable. QQ and Gold Reaper require the previously
validated magic/comment/symbol/account scopes. GoldGrid requires an explicit
name or an unambiguous nonzero magic scope anchored by that name. Position
inheritance includes magic-zero closes but never crosses broker/account/symbol
or conflicting strategy evidence. A shared account alone is not an exclusion.

Account 53015232 on vps-254 also contains S-009 and S-019; those stay included.
No separate GoldGrid name occurs in the 2026-10-15 snapshot. This is shown in
the exclusion audit instead of inventing another magic association. Unresolved
records outside identified external EAs remain included. Raw account balances
and positions remain explicitly labelled as whole-account broker data.

Overview → «Исключения» reconciles before/excluded/retained PnL using current
filters, lists excluded records and provides deal drilldown. Resetting filters,
refreshing, or toggling/removing attribution does not remove exclusions.

Run `node test_mapping.js` for collision, scope and immutability cases.

## Workspace layout

The local workspace groups navigation into Research (strategies/backtests),
Execution (trade ledger/comparison), and Data (accounts/attribution). The landing
page links these stages and shows actual selected-history metrics. Filters are
collapsible on overview/library screens and expanded for the trade ledger.
Strategy cards retain separate source labels, instruments and local attribution
status. On narrow screens, the backtest library uses a select control; larger
screens retain a side list. The strategy creation entry opens the explicitly
labelled demo journey. No new trading or server write actions are introduced.

## Comparison workspace

The selected run, strategy label, account, symbol and optional date range are
saved in browser localStorage. Older deal rows remain usable even when the
latest account snapshot's history window starts later; this is flagged and
never presented as proof of complete historical coverage. The window starts at
the later of the run start and first observed scoped deal, and ends at the
earlier of the run end and latest account snapshot. Custom dates intersect it.

Daily results preserve recorded zeros separately from absent records. Duplicate
backtest dates are summed. Cumulative charts carry forward recorded sums across
absent days with an explicit caveat. Daily differences require records on both
sides; aggregate differences compare observed sums and do not measure execution
quality. Costs are already included in execution Net PnL. Day drawers link to
individual MT5 records. CSV exports include presence flags, source identifiers,
period, currency, snapshot time and limitations.

### Множественный выбор в истории

Счета, стратегии и инструменты поддерживают поиск и выбор нескольких значений галочками. Кнопки «Выбрать все» / «Снять все» действуют на весь список, включая скрытые поиском строки. Выбор применяется кнопкой «Применить»: внутри одного фильтра используется OR, между фильтрами — AND. Пустой выбор даёт пустую выборку; общий сброс возвращает все значения и всю историю. Суммы, графики, журнал и его CSV используют одну выборку. Списки вариантов стабильны в пределах валюты; смена счетов не сбрасывает выбранные стратегии и инструменты. Сверка бэктеста остаётся отдельной явной парой.

## Real historical backtests

The Backtests section now opens the real experiment workspace. Imported reports remain under a separate tab. Bundled choices are S-019 v0.4.9 M20 (source Bid/Ask execution) and a basic SMA crossover on M20 Bid bars (next-open execution and configured spread). The form accepts a UTC inclusive start / exclusive end, starting cash, prior warmup days, costs and strategy parameters. Common parameters have individual controls; the full JSON and contract specification remain editable.

`backtest_jobs.py` stores a bounded SQLite queue. `backtest_worker.py` runs under a separate supervised service, preserving strategy/broker state across bounded quote batches. Progress follows the processed history, including warmup; reports persist after browser closure. Cancellation is cooperative between batches. Queued jobs survive restarts; an interrupted active job becomes failed and can be started again. Historical reports are immutable. There is no WFA launch UI yet.

The worker retains equity event boundaries and extrema, while drawdown/execution inspect source quotes. Equity CSV is compact, not a complete quote-by-quote export. Daily PnL reconciles to final marked equity; open positions are not forcibly liquidated. S-019 news is disabled and contract assumptions are visible in the form and report. SMA uses a different, explicitly labelled unit/bar model.

API routes: GET `/api/backtest/catalog`, GET/POST `/api/backtest/jobs`, GET `/api/backtest/jobs/<id>`, POST `/api/backtest/jobs/<id>/cancel`, GET `/api/backtest/jobs/<id>/files/<allowlisted-name>`. Writes require the exact allowed Host and Origin and JSON requests. Requests cannot choose filesystem paths, Python files or shell commands. Existing trading snapshots and logs remain read-only.

### Parameter optimization and walk-forward

The backtest workspace has three calculation modes. Research currently supports the bundled S-019 M20 strategy. `mode` defaults to `backtest`; use `optimization` or `walk_forward` with a `research` object for the other modes. All modes share the persistent single-worker queue, cancellation, two-hour timeout and existing authentication/Origin checks.

Common research fields: `grid` (supported keys from the catalog, 1–8 values per key), `trials` (2–32 including the submitted baseline), `shortlist` (1–5), deterministic search `seed`, `min_fit_trades`, `min_validation_trades`. Optimization requires `start < fit_end < validation_end < end`. Walk-forward uses `fit_months`, `validation_months`, `test_months` (also the step); at most 12 test windows. Calendar dates and end-exclusive boundaries are UTC. A trailing fragment shorter than 30 days extends the preceding frozen test window.

Candidates: submitted parameters first, then a reproducible sample of the grid within the budget. Fit requires positive PnL and enough closed trades. The shortlist is ranked by profit divided by `max(fixed risk money, monetary drawdown)`. Validation uses three execution seeds (submitted seed and the next two); median trade count must pass and at least two seeds must be profitable. Highest median validation score wins; ties use candidate order. Test performance is never used to choose parameters. This bounded protocol deliberately omits the CLI scripts’ additional refinement pass.

`protocol.json` is persisted before trials; `selection-locks.json` before each OOS execution. Attempts append to `trials.jsonl`, and outcomes go to `windows.json`. These audit files remain accessible after cancellation/failure. Fit and validation use fresh accounts with past-only indicator warmup. WFA keeps balance, opening counters and drawdown across OOS windows. On a roll, the old protections/pending orders execute on the first new-window quote before the remaining position/order is closed/cancelled; the next strategy warms on past quotes while flat. No eligible candidate means cash. Positions at the final test boundary remain marked to market, matching ordinary backtests.

Every research result includes a fresh ordinary backtest of the submitted parameters over the **same OOS period**, capital, costs, warmup and execution seed. The report overlays equity/drawdown and links both detailed reports, all attempts, locks and exports. WFA includes roll transaction costs; its baseline trades continuously. Prior research may already have inspected these dates; these reports do not claim untouched historical data. A window's parameters can be copied into an ordinary backtest form; that replay starts a fresh account and does not reproduce a WFA account's carried balance.

Research uses the same bounded Arrow batches as ordinary backtests, never the CLI scripts’ full in-memory tape or multiprocessing. Deployment: upload the six module/asset files listed in `deploy/research_release.py` to `/opt/atlas-backtester/research-stage`, run `prepare`, then `deploy` after the queue is idle. It overlays the current release without replacing unrelated live dashboard features, records rollback, and checks services and protected routes.
