"""Rolling S-019 WFA: 18-month fit, 6-month validation, 6-month next test."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import math
import multiprocessing
from pathlib import Path
import random
from statistics import median
import time

from backtester import BacktestConfig
from backtester.experiments import code_identity
from backtester.order_engine import OrderBroker
from backtester.s019_search import EquityState, QuoteTape, run_search_trial
from backtester.strategy_loader import load_strategy
from backtester.walk_forward import EquitySamples, walk_forward_windows, warmup_index
from backtester.datasets import export_timezone
from optimize_s019 import GRID, REPLAY_SEEDS, candidate_key

TAPE = FACTORY = PARAMS = None
WARMUP_BARS = 10000


def job_run(job):
    params = {**PARAMS, **job["overrides"], "InpEntryDelaySeed": job["seed"]}
    metrics, _ = run_search_trial(TAPE, FACTORY(**params), BacktestConfig(initial_cash=100000),
                                 job["start"], job["end"], warmup_start=job["warmup"])
    return {**job, **metrics}


def coarse_candidates(count):
    baseline = {key: PARAMS[key] for key in GRID}
    values, seen = [baseline], {candidate_key(baseline)}
    for key, choices in GRID.items():
        for value in choices:
            neighbor = {**baseline, key: value}
            if candidate_key(neighbor) not in seen:
                seen.add(candidate_key(neighbor)); values.append(neighbor)
    rng = random.Random(20261014)
    while len(values) < count:
        item = {key: rng.choice(choices) for key, choices in GRID.items()}
        if candidate_key(item) not in seen:
            seen.add(candidate_key(item)); values.append(item)
    return values


def window_indices(window):
    return {name: TAPE.boundary(value) for name, value in [
        ("fit_start", window.fit_start), ("validation_start", window.validation_start),
        ("test_start", window.test_start), ("test_end", window.test_end)]}


def protocol_window(window):
    return {key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in window.__dict__.items()}


def save_stream(path, rows):
    with path.open("w") as file:
        for row in rows:
            file.write(json.dumps(row, default=lambda value: value.isoformat()) + "\n")


def main():
    global TAPE, FACTORY, PARAMS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--params", default="parameters/s019-mt5-report-no-news.json", type=Path)
    parser.add_argument("--strategy", default="strategies/s019_v0_4_9.py")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trials", default=256, type=int)
    parser.add_argument("--workers", default=4, type=int)
    args = parser.parse_args()
    if not 20 <= args.trials <= math.prod(len(v) for v in GRID.values()) or args.workers < 1:
        parser.error("invalid search size or workers")
    args.output.mkdir(parents=True, exist_ok=False)
    PARAMS = json.loads(args.params.read_text())
    if (PARAMS["InpWorkingTimeframe"], PARAMS["InpRiskMoney"], PARAMS["InpDailyPositionLimit"], PARAMS["news_mode"]) != (20, 1000, 3, "disabled"):
        raise ValueError("fixed WFA assumptions require M20, risk 1000, three openings/day, news disabled")
    PARAMS["InpEntryDelaySeed"] = REPLAY_SEEDS[0]
    started = time.monotonic()
    TAPE, manifest = QuoteTape.from_dataset(args.data)
    zone = export_timezone(PARAMS["broker"]["server_timezone"])
    anchor = TAPE.tick(0).timestamp.astimezone(zone).replace(minute=0, second=0, microsecond=0)
    stop = (TAPE.tick(len(TAPE.bid)-1).timestamp+timedelta(microseconds=1)).astimezone(zone)
    windows = walk_forward_windows(anchor, stop)
    if not windows:
        raise ValueError("not enough data for 24 months of past data plus a test period")
    for array in [TAPE.timestamps, TAPE.bid, TAPE.ask, TAPE.sequence, TAPE.bar_starts]:
        array.setflags(write=False)
    protocol = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "method": "rolling nested walk-forward",
                "fit_months": 18, "validation_months": 6, "oos_months": 6, "warmup_observed_m20_bars": WARMUP_BARS,
                "grid": GRID, "coarse_candidates_per_window": args.trials, "refine_fit_top": 6,
                "validate_fit_top": 24, "search_seed": 20261014, "execution_seeds": REPLAY_SEEDS,
                "selection": "profitable fit with >=40 closes; median validation >=15 closes, at least 2/3 profitable seeds; maximize median validation recovery",
                "score": "net equity change / max(1000 USD, max all-tick equity drawdown USD)",
                "no_qualifying_model": "hold cash for the next OOS window",
                "roll": "at first quote of each new window, old server protection/pending fills first, then close old positions and cancel old pending/delay; warm new parameters with past quotes only",
                "oos_account": "continuous 100000 USD account, fixed 1000 USD risk, daily entry counters and high-water mark retained across windows",
                "end_of_data": "liquidate on last available bid/ask quote; report this convention",
                "baseline": "fixed original profile continuously over the same OOS dates, no parameter rolls",
                "research_history": "grid reused from prior 2026 research; this is not a wholly untouched market sample",
                "costs": "source bid/ask, constant snapshot USD tick value, no swaps, commissions, additional slippage or stop-out",
                "data": manifest.model_dump(mode="json"), "fixed_parameters": PARAMS, "code": code_identity(),
                "windows": [protocol_window(w) for w in windows]}
    selections = []
    independent_oos = []
    main_broker = None
    state = EquityState(100000.)
    samples = EquitySamples(TAPE, PARAMS["broker"]["server_timezone"])
    previous_equity = 100000.
    position_models = {}

    with load_strategy(args.strategy, "ticks") as loaded:
        FACTORY = loaded.factory
        protocol["strategy_source_sha256"] = loaded.source_sha256
        (args.output/"protocol.json").write_text(json.dumps(protocol, indent=2)+"\n")
        original_candidates = coarse_candidates(args.trials)
        print(json.dumps({"stage":"loaded", "rows":len(TAPE.bid), "windows":len(windows),
                          "oos_start":windows[0].test_start.isoformat(), "oos_end":stop.isoformat()}), flush=True)

        def batch(pool, jobs, path, stage, fold):
            futures = [pool.submit(job_run, item) for item in jobs]
            complete = []
            with path.open("a") as file:
                for future in as_completed(futures):
                    row = future.result(); complete.append(row)
                    file.write(json.dumps(row)+"\n"); file.flush()
                    if len(complete) % 64 == 0 or len(complete) == len(futures):
                        print(json.dumps({"stage":stage,"fold":fold+1,"completed":len(complete),
                                          "total":len(futures),"elapsed_seconds":time.monotonic()-started}),flush=True)
            return complete

        def make_job(fold, candidate, overrides, phase, start, end, seed=190300):
            return {"fold":fold, "candidate":candidate, "overrides":overrides, "phase":phase,
                    "start":start, "end":end, "warmup":warmup_index(TAPE,start,WARMUP_BARS), "seed":seed}

        with ProcessPoolExecutor(max_workers=args.workers,mp_context=multiprocessing.get_context("fork")) as pool:
            for window in windows:
                fold=window.index
                directory=args.output/f"window-{fold+1:02d}"
                directory.mkdir()
                bounds=window_indices(window)
                candidates=list(original_candidates)
                seen={candidate_key(v) for v in candidates}
                fit=batch(pool,[make_job(fold,i,c,"fit",bounds["fit_start"],bounds["validation_start"])
                                for i,c in enumerate(candidates)],directory/"trials.jsonl","fit",fold)
                eligible=lambda row:row["closed_trades"]>=40 and row["net_profit_usd"]>0
                rank=lambda rows:sorted((r for r in rows if eligible(r)),
                                       key=lambda r:(r["recovery_factor"],candidate_key(r["overrides"])),reverse=True)
                ranked=rank(fit)
                extra=[]
                for row in ranked[:6]:
                    for key,values in GRID.items():
                        for value in values:
                            item={**row["overrides"],key:value}
                            if candidate_key(item) not in seen:
                                seen.add(candidate_key(item));extra.append(item)
                first=len(candidates);candidates+=extra
                fit+=batch(pool,[make_job(fold,first+i,c,"fit",bounds["fit_start"],bounds["validation_start"])
                                 for i,c in enumerate(extra)],directory/"trials.jsonl","refine",fold)
                ranked=rank(fit)
                shortlist=ranked[:24]
                if not any(r["candidate"]==0 for r in shortlist):
                    shortlist.append(next(r for r in fit if r["candidate"]==0))
                validated=batch(pool,[make_job(fold,r["candidate"],r["overrides"],"validation",
                                               bounds["validation_start"],bounds["test_start"],seed)
                                       for r in shortlist for seed in REPLAY_SEEDS],directory/"trials.jsonl","validation",fold)
                choices=[]
                for row in shortlist:
                    vals=[v for v in validated if v["candidate"]==row["candidate"]]
                    choices.append({"candidate":row["candidate"],"overrides":row["overrides"],"fit":row,
                                    "validation":vals,"median_score":median(v["recovery_factor"] for v in vals),
                                    "median_profit":median(v["net_profit_usd"] for v in vals),
                                    "median_trades":median(v["closed_trades"] for v in vals),
                                    "profitable_seeds":sum(v["net_profit_usd"]>0 for v in vals)})
                choices.sort(key=lambda r:(r["median_score"],r["median_profit"],candidate_key(r["overrides"])),reverse=True)
                qualifying=[r for r in choices if r["median_trades"]>=15 and r["profitable_seeds"]>=2 and eligible(r["fit"])]
                chosen=qualifying[0] if qualifying else None
                selection={"window":protocol_window(window),"indices":bounds,
                           "locked_at_utc":datetime.now(timezone.utc).isoformat(),"oos_used_for_selection":False,
                           "fit_candidates":len(fit),"validation_candidates":len(shortlist),"selected":chosen,
                           "action":"trade" if chosen else "cash"}
                (directory/"selection-lock.json").write_text(json.dumps(selection,indent=2)+"\n")
                (directory/"validation-ranking.json").write_text(json.dumps(choices,indent=2)+"\n")
                selected_params={**PARAMS,**chosen["overrides"]} if chosen else {**PARAMS,"InpAllowLong":False,"InpAllowShort":False}
                (directory/"params.json").write_text(json.dumps(selected_params,indent=2)+"\n")
                # Every OOS computation happens after the model lock is persisted.
                diagnostic_jobs=[make_job(fold,"selected",chosen["overrides"] if chosen else {"InpAllowLong":False,"InpAllowShort":False},
                                                     "oos",bounds["test_start"],bounds["test_end"],seed)
                                 for seed in REPLAY_SEEDS]
                diagnostic_jobs.append(make_job(fold,"selected",chosen["overrides"] if chosen else {"InpAllowLong":False,"InpAllowShort":False},
                                                "in_sample_full",bounds["fit_start"],bounds["test_start"]))
                diagnostics=batch(pool,diagnostic_jobs,directory/"trials.jsonl","oos_check",fold)
                independent_oos += [row for row in diagnostics if row["phase"]=="oos"]
                selection["in_sample_replay"]=next(row for row in diagnostics if row["phase"]=="in_sample_full")
                first_quote=TAPE.tick(bounds["test_start"])
                if main_broker is None:
                    main_broker=OrderBroker(BacktestConfig(initial_cash=100000),FACTORY(**selected_params).broker_spec)
                # The old server processes the boundary quote before the EA changes parameters.
                events_before=len(main_broker.events)
                main_broker.advance(first_quote,int(TAPE.sequence[bounds["test_start"]]))
                for event in main_broker.events[events_before:]:
                    if event["event"]=="position_opened":position_models[event["position_id"]]=fold-1
                main_broker.close_position("wfa_parameter_roll")
                main_broker.cancel_pending("wfa_parameter_roll")
                main_broker.event("wfa_parameters_changed",fold=fold,action=selection["action"],parameters=selected_params)
                events_before=len(main_broker.events)
                metric,main_broker=run_search_trial(TAPE,FACTORY(**selected_params),BacktestConfig(initial_cash=100000),
                                                   bounds["test_start"],bounds["test_end"],
                                                   warmup_start=warmup_index(TAPE,bounds["test_start"],WARMUP_BARS),
                                                   broker=main_broker,equity_state=state,equity_observer=samples)
                for event in main_broker.events[events_before:]:
                    if event["event"]=="position_opened":position_models[event["position_id"]]=fold
                selection["continuous_oos"]={"window_equity_pnl_usd":metric["final_equity_usd"]-previous_equity,
                                              "cumulative_profit_usd":metric["net_profit_usd"],
                                              "equity_end_usd":metric["final_equity_usd"],"global_drawdown_usd":state.max_drawdown_usd,
                                              "global_drawdown_fraction":state.max_drawdown}
                previous_equity=metric["final_equity_usd"]
                selections.append(selection)
                (directory/"window-result.json").write_text(json.dumps(selection,indent=2)+"\n")
                print(json.dumps({"stage":"window_completed","fold":fold+1,"action":selection["action"],
                                  "parameters":chosen["overrides"] if chosen else None,
                                  **selection["continuous_oos"],"elapsed_seconds":time.monotonic()-started}),flush=True)

        last=TAPE.tick(len(TAPE.bid)-1)
        main_broker.tick=last
        main_broker.close_position("wfa_end_of_data")
        main_broker.cancel_pending("wfa_end_of_data")
        samples([len(TAPE.bid)-1],[main_broker.balance])
        assert max(main_broker.opened_by_day.values(),default=0)<=3
        assert math.isclose(math.fsum(samples.daily.values()),main_broker.balance-100000,abs_tol=1e-6)
        baseline_samples=EquitySamples(TAPE,PARAMS["broker"]["server_timezone"])
        first_start=TAPE.boundary(windows[0].test_start)
        baseline_metric,baseline=run_search_trial(TAPE,FACTORY(**PARAMS),BacktestConfig(initial_cash=100000),first_start,len(TAPE.bid),
                                                warmup_start=warmup_index(TAPE,first_start,WARMUP_BARS),equity_observer=baseline_samples)
        baseline.close_position("wfa_end_of_data");baseline.cancel_pending("wfa_end_of_data")
        baseline_samples([len(TAPE.bid)-1],[baseline.balance])
        assert math.isclose(math.fsum(baseline_samples.daily.values()),baseline.balance-100000,abs_tol=1e-6)
        save_stream(args.output/"wfa-fills.jsonl",[fill.__dict__ if hasattr(fill,"__dict__") else
                    {name:getattr(fill,name) for name in fill.__dataclass_fields__} for fill in main_broker.fills])
        save_stream(args.output/"baseline-fills.jsonl",[{name:getattr(fill,name) for name in fill.__dataclass_fields__} for fill in baseline.fills])
        save_stream(args.output/"wfa-events.jsonl",main_broker.events)
        save_stream(args.output/"wfa-equity-samples.jsonl",[{"index":i,"equity":v} for i,v in samples.points])
        save_stream(args.output/"baseline-equity-samples.jsonl",[{"index":i,"equity":v} for i,v in baseline_samples.points])
        (args.output/"daily-equity-pnl.json").write_text(json.dumps({"wfa":samples.daily,"baseline":baseline_samples.daily},indent=2)+"\n")
        final={"status":"completed_pending_reference_checks","windows":selections,"independent_oos":independent_oos,
               "wfa":{"initial_cash":100000,"final_equity_usd":main_broker.balance,"net_profit_usd":main_broker.balance-100000,
                        "max_drawdown_usd":state.max_drawdown_usd,"max_drawdown":state.max_drawdown,
                        "max_daily_openings":max(main_broker.opened_by_day.values(),default=0),
                        "fill_count":len(main_broker.fills),"sampled_equity_points":len(samples.points),"position_models":position_models},
               "baseline":baseline_metric,"elapsed_seconds":time.monotonic()-started}
        (args.output/"wfa-result.json").write_text(json.dumps(final,indent=2)+"\n")
        print(json.dumps({"stage":"wfa_completed","wfa":final["wfa"],"baseline":baseline_metric,
                          "elapsed_seconds":time.monotonic()-started}),flush=True)


if __name__=="__main__":
    main()
