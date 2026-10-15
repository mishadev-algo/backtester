# S-019: walk-forward optimization

Run from the backtester repository on Linux:

```bash
uv sync --frozen
uv run --frozen python -u scripts/wfa_s019.py \
  --data data/deuidxeur-qdm-20211014-20261014 \
  --output results/new-s019-wfa --trials 256 --workers 4
```

The output directory must not exist. The runner uses Linux `fork` to share immutable quote arrays between workers. Budget approximately 12–16 GB RAM for this history, with additional memory for operating-system cache. There is no automatic resume. Window trials, parameter locks and completed window results are written incrementally.

## Selection and chronology

Each window has 18 calendar months for fitting, the next six for validation and the following six for out-of-sample (OOS) testing. Advance six months, retaining a rolling 24-month past sample. A final fragment shorter than 30 days extends the previous frozen test window. Warmup uses up to 10,000 observed M20 bars preceding the evaluated period; trading is disabled during warmup.

The search includes the original parameters, single-parameter neighbors and a deterministic random sample, then unseen neighbors of the six best fit candidates. Fit requires at least 40 closed trades and positive equity PnL. Validate the best 24 fit candidates and the original baseline using execution-delay seeds 190300, 190301 and 190302. Eligibility requires a median of at least 15 validation trades and positive validation PnL in at least two seeds. Maximize median validation recovery: equity PnL divided by `max(1000 USD, maximum equity drawdown USD)`. If no eligible candidate exists, hold cash in the next OOS window.

`selection-lock.json` is saved before evaluating that window's OOS data. Later windows may use earlier OOS data because those observations are then in the past. The grid was used in earlier research on 2026 data; this historical exercise is not an entirely untouched sample. No OOS results from this run are used to revise the selection protocol.

## Execution and continuous account

Use the canonical Python S-019 v0.4.9 strategy and order broker. The accelerated runner visits strategy action points and finds the first executable source quote with vector operations. It evaluates protective levels and equity extrema on the source Bid/Ask ticks. M20 bars form indicators; they do not replace tick execution. The accelerator is specific to bundled S019 M20 and must not be used for arbitrary Python strategies.

Capital starts at 100,000 USD, risk stays 1,000 USD per trade, and the limit is three openings per server day. The broker supports one simultaneous position. News is disabled. Broker time follows Europe/Kyiv, UTC+2 in winter and UTC+3 in summer.

OOS uses one continuous account: cash, daily opening counters and the equity high-water mark survive window changes. At the first quote of a new window, apply the old broker's pending fills and protective levels first, close any remaining position at that quote, cancel pending/delayed entry and initialize the new strategy using past quotes only. This explicit parameter-roll liquidation is part of the experiment. Close any final position at the last available quote. All fills and roll events are retained.

Compute overall drawdown from the continuous all-tick equity state; never add window drawdowns. Saved equity points retain segment boundaries and extrema rather than all 107 million quotes. Daily equity PnL telescopes to final account profit. The reporting check reconstructs drawdown from retained extrema and reconciles all realized trades to final cash.

The fixed original baseline runs continuously on exactly the same OOS dates, without parameter rolls. Consequently the comparison includes the operational effect of parameter-roll liquidations.

## Data and contract assumptions

Five-year input: 107,685,090 headerless QDM records (`DateTime,Bid,Ask`), 14 October 2021 through 14 October 2026, Europe/Kyiv. Volume is absent and stored as zero; S019 does not use it. The uncompressed source SHA256 is checked against the repository checksum. Bid/Ask and timestamp overlap matches the preceding data export on sampled winter and summer fragments.

This leaves approximately three years of OOS testing, October 2023–October 2026. The first two years supply the initial fit and validation periods.

The configured DAX40 contract is the previously supplied FivePercent snapshot: point/tick size 0.01, tick value 0.011203 USD per lot, lot range 0.01–500 in 0.01 steps. It is not a verified ICMarkets contract. The calculation uses a constant EURUSD equivalent of 1.1203, source spreads and no commissions, swaps, extra slippage or margin stop-out. Broker historical DST dates have not been independently verified. These assumptions apply to both WFA and the fixed baseline. MT5 news/include and delay-RNG differences documented in `S019_PORT.md` remain relevant.

## Outputs

`protocol.json` records the rules, fixed parameters, dataset and engine identity. Each `window-NN` contains every trial, validation ranking, frozen selection, executable Python parameters and continuous OOS result. The root contains all fills, WFA events, compact equity points, daily PnL and `wfa-result.json`.

The reviewed five-year run additionally includes a self-contained HTML chart, summary, window schedule, trades and monthly PnL CSVs, parameter SET files, import checks and reference comparisons. SET files describe the historical window they belong to; the final window's file was selected using data ending in April 2026. It is not a fresh calibration through October 2026. The active parameter profile is preserved.
