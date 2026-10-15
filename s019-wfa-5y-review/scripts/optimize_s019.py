"""Reproducible bounded S-019 search; use python scripts/optimize_s019.py --help."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
import multiprocessing
from pathlib import Path
import random
from statistics import median
import time

from backtester import BacktestConfig
from backtester.experiments import code_identity
from backtester.s019_search import QuoteTape, run_search_trial
from backtester.strategy_loader import load_strategy

GRID = {
    "InpADRPeriods": [8, 12, 16, 24],
    "InpLongADRMultiplier": [.75, 1., 1.25],
    "InpShortADRMultiplier": [1., 1.5, 2.],
    "InpLimitEntryFromStop": [.35, .5, .65, .8],
    "InpTakeProfitRR": [1.5, 2., 2.5, 3.],
    "InpSlowEMAPeriod": [100, 150, 200],
    "InpLimitLifetimeBars": [1, 3, 6],
}
REPLAY_SEEDS = [190300, 190301, 190302]
TAPE = FACTORY = PARAMS = SPLITS = None


def candidate_key(values):
    return json.dumps(values, sort_keys=True)


def run_job(job):
    candidate, overrides, segment, seed = job
    params = {**PARAMS, **overrides, "InpEntryDelaySeed": seed}
    start, end = SPLITS[segment]
    metric, _ = run_search_trial(TAPE, FACTORY(**params), BacktestConfig(initial_cash=100000), start, end)
    return {"candidate": candidate, "overrides": overrides, "segment": segment, "seed": seed, **metric}


def main():
    global TAPE, FACTORY, PARAMS, SPLITS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--strategy", default="strategies/s019_v0_4_9.py")
    parser.add_argument("--params", default="parameters/s019-mt5-report-no-news.json", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trials", default=256, type=int)
    parser.add_argument("--workers", default=4, type=int)
    args = parser.parse_args()
    if args.trials < 20 or args.workers < 1:
        parser.error("at least 20 candidates and one worker required")
    if args.trials > math.prod(len(values) for values in GRID.values()):
        parser.error("candidate count exceeds the finite grid")
    args.output.mkdir(parents=True, exist_ok=False)
    PARAMS = json.loads(args.params.read_text())
    if PARAMS["InpWorkingTimeframe"] != 20 or PARAMS["InpRiskMoney"] != 1000 or PARAMS["InpDailyPositionLimit"] != 3:
        raise ValueError("this protocol fixes M20, risk 1000 USD and three daily openings")
    PARAMS["InpEntryDelaySeed"] = REPLAY_SEEDS[0]
    TAPE, manifest = QuoteTape.from_dataset(args.data)
    for array in [TAPE.timestamps, TAPE.bid, TAPE.ask, TAPE.sequence, TAPE.bar_starts]:
        array.setflags(write=False)
    begin = TAPE.boundary(datetime.fromisoformat("2026-01-01T00:00:00+02:00"))
    train_end = TAPE.boundary(datetime.fromisoformat("2026-07-01T00:00:00+03:00"))
    validation_end = TAPE.boundary(datetime.fromisoformat("2026-09-01T00:00:00+03:00"))
    SPLITS = {"train": (begin, train_end), "validation": (train_end, validation_end),
              "test": (validation_end, len(TAPE.bid)), "full": (begin, len(TAPE.bid))}
    protocol = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "grid": GRID,
                "coarse_candidates": args.trials, "refine_train_top": 6, "validate_train_top": 24,
                "search_seed": 20261014, "execution_seeds": REPLAY_SEEDS, "fixed_parameters": PARAMS,
                "selection": "highest median validation recovery factor over three execution seeds; train profit >0 and >=30 closes; validation >=10 closes and 2/3 profitable seeds",
                "score": "net equity change / max(1000 USD, maximum all-tick equity drawdown in USD)",
                "segments": {name: {"start_tick_index": a, "end_tick_index_exclusive": b,
                                      "first_utc": TAPE.tick(a).timestamp.isoformat(),
                                      "last_utc": TAPE.tick(b-1).timestamp.isoformat(), "ticks": b-a}
                             for name, (a, b) in SPLITS.items()},
                "accounts": "independent 100000 USD accounts; all earlier quotes warm indicators only; no carried trades",
                "holdout": "September onward is scored only after winner lock; this historical dataset was previously viewed",
                "costs": "fixed snapshot USD tick value; source bid/ask spread; no swaps/commission/additional slippage",
                "data": manifest.model_dump(mode="json"), "code": code_identity()}
    started = time.monotonic()
    with load_strategy(args.strategy, "ticks") as loaded:
        FACTORY = loaded.factory
        protocol["strategy_source_sha256"] = loaded.source_sha256
        (args.output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
        baseline = {key: PARAMS[key] for key in GRID}
        candidates = [baseline]
        seen = {candidate_key(baseline)}
        for key, values in GRID.items():
            for value in values:
                neighbor = {**baseline, key: value}
                if candidate_key(neighbor) not in seen:
                    seen.add(candidate_key(neighbor)); candidates.append(neighbor)
        rng = random.Random(20261014)
        while len(candidates) < args.trials:
            values = {key: rng.choice(choices) for key, choices in GRID.items()}
            if candidate_key(values) not in seen:
                seen.add(candidate_key(values)); candidates.append(values)
        all_rows = []

        def batch(pool, jobs, stage):
            tasks = [pool.submit(run_job, job) for job in jobs]
            done = []
            with (args.output / "trials.jsonl").open("a") as file:
                for future in as_completed(tasks):
                    result = future.result()
                    done.append(result); all_rows.append(result)
                    file.write(json.dumps(result) + "\n"); file.flush()
                    if len(done) % 32 == 0 or len(done) == len(tasks):
                        print(json.dumps({"stage": stage, "completed": len(done), "total": len(tasks),
                                          "elapsed_seconds": time.monotonic() - started}), flush=True)
            return done

        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("fork")) as pool:
            train = batch(pool, [(i, c, "train", REPLAY_SEEDS[0]) for i, c in enumerate(candidates)], "coarse_train")
            eligible = lambda row: row["closed_trades"] >= 30 and row["net_profit_usd"] > 0
            ranked = sorted([row for row in train if eligible(row)],
                            key=lambda row: (row["recovery_factor"], candidate_key(row["overrides"])), reverse=True)
            if not ranked:
                raise RuntimeError("No profitable training candidate with 30 closed trades")
            refinements = []
            for row in ranked[:6]:
                for key, choices in GRID.items():
                    for value in choices:
                        neighbor = {**row["overrides"], key: value}
                        if candidate_key(neighbor) not in seen:
                            seen.add(candidate_key(neighbor)); refinements.append(neighbor)
            new = len(candidates)
            candidates += refinements
            train += batch(pool, [(new+i, c, "train", REPLAY_SEEDS[0]) for i, c in enumerate(refinements)], "refine_train")
            ranked = sorted([row for row in train if eligible(row)],
                            key=lambda row: (row["recovery_factor"], candidate_key(row["overrides"])), reverse=True)
            shortlist = ranked[:24]
            if not any(row["candidate"] == 0 for row in shortlist):
                shortlist.append(next(row for row in train if row["candidate"] == 0))
            validated = batch(pool, [(row["candidate"], row["overrides"], "validation", seed)
                                     for row in shortlist for seed in REPLAY_SEEDS], "validation")
            choices = []
            for row in shortlist:
                values = [v for v in validated if v["candidate"] == row["candidate"]]
                choices.append({"candidate": row["candidate"], "overrides": row["overrides"],
                                "train": row, "validation": values,
                                "validation_score": median(v["recovery_factor"] for v in values),
                                "validation_median_profit_usd": median(v["net_profit_usd"] for v in values),
                                "validation_median_trades": median(v["closed_trades"] for v in values),
                                "validation_profitable_seeds": sum(v["net_profit_usd"] > 0 for v in values)})
            choices.sort(key=lambda row: (row["validation_score"], row["validation_median_profit_usd"],
                                           candidate_key(row["overrides"])), reverse=True)
            qualifying = [row for row in choices if row["validation_median_trades"] >= 10 and row["validation_profitable_seeds"] >= 2]
            if not qualifying:
                raise RuntimeError("No candidate passed the predefined validation checks; trials preserved")
            winner = qualifying[0]
            selected = {**PARAMS, **winner["overrides"]}
            lock = {"locked_at_utc": datetime.now(timezone.utc).isoformat(), "winner": winner,
                    "selected_parameters": selected, "holdout_used_for_selection": False,
                    "training_candidates": len(train), "validation_candidates": len(shortlist)}
            (args.output / "winner-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
            (args.output / "best-params.json").write_text(json.dumps(selected, indent=2) + "\n")
            (args.output / "validation-ranking.json").write_text(json.dumps(choices, indent=2) + "\n")
            tests = batch(pool, [(label, overrides, segment, seed)
                                 for label, overrides in [("baseline", baseline), ("selected", winner["overrides"])]
                                 for segment in ["test", "full"] for seed in REPLAY_SEEDS], "locked_test_and_full")
        result = {"winner": lock, "comparison": tests, "elapsed_seconds": time.monotonic() - started,
                  "status": "search_completed_reference_verification_pending"}
        (args.output / "optimization.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"stage": "search_completed", "winner": winner["overrides"],
                          "training_candidates": len(train), "elapsed_seconds": time.monotonic()-started}), flush=True)


if __name__ == "__main__":
    main()
