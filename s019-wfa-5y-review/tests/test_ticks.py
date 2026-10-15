from datetime import datetime, timedelta, timezone
import csv
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from backtester import BacktestConfig, Tick, run_tick_backtest
from backtester.datasets import parse_boundary
from backtester.experiments import ExperimentStore, RunRequest, evaluate, execute_experiment
from backtester.tick_data import TickCsvProfile, TickDatasetSpec, import_ticks, inspect_ticks, load_ticks

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2025, 10, 13, tzinfo=timezone.utc)


def tick(index, bid=100, ask=101, *, millis=None):
    return Tick(STAMP + timedelta(milliseconds=index if millis is None else millis), bid, ask, 10, index)


class Signals:
    def __init__(self, *values):
        self.values = iter(values)

    def on_tick(self, history):
        return next(self.values, None)


class TickEngineTests(unittest.TestCase):
    def test_next_quote_execution_uses_ask_for_buy_bid_for_sell(self):
        result = run_tick_backtest([tick(0, 90, 200), tick(1, 104, 105), tick(2, 110, 140)],
                                   Signals(1, 0), BacktestConfig(initial_cash=1000))
        buy, sell = result.fills
        self.assertEqual((buy.price, sell.price), (105, 110))
        self.assertEqual((buy.tick_sequence, buy.signal_tick_sequence), (1, 0))
        self.assertEqual((sell.tick_sequence, sell.signal_tick_sequence), (2, 1))
        self.assertAlmostEqual(result.equity[1].equity, 1000 / 105 * 104)
        self.assertAlmostEqual(result.equity[-1].equity, 1000 / 105 * 110)
        self.assertEqual(result.equity[-1].quantity, 0)
        self.assertEqual(result.summary()["ticks_processed"], 3)
        self.assertNotIn("bars_processed", result.summary())

    def test_spread_is_already_in_quotes_and_cannot_be_charged_again(self):
        with self.assertRaisesRegex(ValueError, "already contains"):
            run_tick_backtest([tick(0)], Signals(), BacktestConfig(spread_bps=5))
        result = run_tick_backtest([tick(0), tick(1)], Signals(1), BacktestConfig(initial_cash=1000))
        self.assertAlmostEqual(result.equity[-1].equity, 1000 / 101 * 100)
        self.assertEqual(result.config.price_type, "bid_ask")

    def test_fees_and_slippage_reserve_cash_and_reconcile(self):
        result = run_tick_backtest([tick(0), tick(1), tick(2)], Signals(1, 0),
                                   BacktestConfig(initial_cash=1000, commission_bps=100, slippage_bps=100))
        buy, sell = result.fills
        units = 1000 / (101 * 1.01 * 1.01)
        self.assertAlmostEqual(buy.quantity, units)
        self.assertAlmostEqual(buy.price, 101 * 1.01)
        self.assertAlmostEqual(sell.price, 99)
        self.assertAlmostEqual(result.equity[-1].equity, units * 99 * 0.99)
        self.assertTrue(all(point.cash >= 0 for point in result.equity))

    def test_same_timestamp_ticks_execute_in_source_order(self):
        result = run_tick_backtest([tick(0, millis=0), tick(1, millis=0), tick(2, millis=0)], Signals(1, 0))
        self.assertEqual(len(result.equity), 3)
        self.assertEqual(len(result.fills), 2)
        for fill in result.fills:
            self.assertEqual(fill.timestamp, fill.signal_timestamp)
            self.assertGreater(fill.tick_sequence, fill.signal_tick_sequence)

    def test_final_signal_stays_unfilled(self):
        result = run_tick_backtest([tick(0)], Signals(1))
        self.assertEqual(result.fills, ())
        self.assertTrue(result.final_signal_unfilled)

    def test_history_has_no_future_quotes_and_warmup_only_establishes_target(self):
        views = []
        self_case = self
        class Strategy:
            def on_tick(self, history):
                views.append(history)
                with self_case.assertRaises(IndexError):
                    history[len(history)]
                return 1.0 if len(history) == 1 else None
        result = run_tick_backtest([tick(2), tick(3)], Strategy(), warmup_ticks=[tick(0), tick(1)])
        self.assertEqual([len(view) for view in views], [1, 2, 3, 4])
        self.assertEqual(result.fills[0].tick_sequence, 2)
        self.assertEqual(len(result.equity), 2)
        self.assertEqual(result.equity[0].timestamp, tick(2).timestamp)

    def test_drawdown_is_observed_on_each_tick_including_intraminute_changes(self):
        result = run_tick_backtest([tick(0, 100, 100), tick(1, 100, 100), tick(2, 120, 120), tick(3, 90, 90)], Signals(1))
        self.assertAlmostEqual(result.summary()["max_drawdown"], 0.25)
        self.assertEqual(result.summary()["ticks_processed"], 4)

    def test_bad_quotes_targets_and_order_are_rejected(self):
        for bid, ask, volume in ((0, 1, 0), (101, 100, 0), (100, float("nan"), 0), (100, 101, -1)):
            with self.subTest(bid=bid, ask=ask), self.assertRaises(ValueError):
                Tick(STAMP, bid, ask, volume)
        for target in (-1, 1.1, True, float("nan")):
            with self.subTest(target=target), self.assertRaises(ValueError):
                run_tick_backtest([tick(0)], Signals(target))
        for ticks in ([], [tick(1), tick(0)], [tick(0), tick(0)]):
            with self.subTest(ticks=ticks), self.assertRaises(ValueError):
                run_tick_backtest(ticks, Signals())


class TickDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = self.root / "dataset"
        self.profile = TickCsvProfile(timezone="Europe/Kyiv")
        self.spec = TickDatasetSpec(symbol="UNSPECIFIED", source="provided-format-sample")

    def import_sample(self):
        return import_ticks(ROOT / "examples/quote-ticks.csv", self.dataset, self.spec, self.profile, batch_size=2)

    def test_all_sample_quotes_and_milliseconds_survive_import(self):
        manifest = self.import_sample()
        ticks = load_ticks(self.dataset)
        self.assertEqual(manifest.rows, 11)
        self.assertEqual(len(ticks), 11)
        self.assertEqual(ticks[0].timestamp, STAMP + timedelta(milliseconds=395))
        self.assertEqual(ticks[-1].timestamp, STAMP + timedelta(seconds=3, milliseconds=354))
        self.assertEqual(ticks[0].bid, 24305.8)
        self.assertEqual(ticks[-1].ask, 24313.5)
        self.assertEqual([quote.volume for quote in ticks], [10] * 11)
        self.assertEqual([quote.sequence for quote in ticks], list(range(11)))
        self.assertEqual(inspect_ticks(self.dataset), manifest)
        self.assertFalse((self.dataset / "bars.parquet").exists())

    def test_equal_timestamps_are_preserved_even_for_identical_quotes(self):
        source = self.root / "source.csv"
        source.write_text("DateTime,Bid,Ask,Volume\n20251013 03:00:00.395,100,101,10\n20251013 03:00:00.395,100,101,10\n")
        manifest = import_ticks(source, self.dataset, self.spec, self.profile)
        self.assertEqual(manifest.rows, 2)
        self.assertEqual(manifest.equal_timestamp_records, 1)
        self.assertEqual([quote.sequence for quote in load_ticks(self.dataset)], [0, 1])

    def test_period_and_warmup_preserve_original_sequences(self):
        self.import_sample()
        ticks = load_ticks(self.dataset, STAMP + timedelta(seconds=1), STAMP + timedelta(seconds=2))
        self.assertEqual([quote.sequence for quote in ticks], [3, 4, 5])
        warmup = load_ticks(self.dataset, end=STAMP + timedelta(seconds=2), tail=2)
        self.assertEqual([quote.sequence for quote in warmup], [4, 5])

    def test_timezone_is_required_and_fixed_offset_differs_from_kyiv_in_winter(self):
        with self.assertRaisesRegex(ValueError, "explicit profile timezone"):
            import_ticks(ROOT / "examples/quote-ticks.csv", self.dataset, self.spec, TickCsvProfile())
        self.assertFalse(self.dataset.exists())
        source = self.root / "winter.csv"
        source.write_text("DateTime,Bid,Ask,Volume\n20250113 03:00:00.395,100,101,10\n")
        import_ticks(source, self.dataset, self.spec, self.profile)
        import_ticks(source, self.root / "fixed", self.spec, TickCsvProfile(timezone="UTC+03:00"))
        self.assertEqual(load_ticks(self.dataset)[0].timestamp.hour, 1)
        self.assertEqual(load_ticks(self.root / "fixed")[0].timestamp.hour, 0)

    def test_checksum_existing_dataset_and_unsorted_records(self):
        self.import_sample()
        with self.assertRaises(FileExistsError):
            self.import_sample()
        with (self.dataset / "ticks.parquet").open("ab") as destination:
            destination.write(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_ticks(self.dataset)
        source = self.root / "unsorted.csv"
        source.write_text("DateTime,Bid,Ask,Volume\n20251013 03:00:01.000,100,101,10\n20251013 03:00:00.000,100,101,10\n")
        with self.assertRaisesRegex(ValueError, "CSV line 3"):
            import_ticks(source, self.root / "bad", self.spec, self.profile)
        self.assertFalse((self.root / "bad").exists())

    def test_tick_experiment_and_report_have_correct_counts_and_reconcile(self):
        self.import_sample()
        request = RunRequest(data=self.dataset, output=self.root / "run", parameters={"fast": 2, "slow": 3})
        result, metadata = execute_experiment(request)
        self.assertEqual(result.summary()["ticks_processed"], 11)
        self.assertGreater(len(result.fills), 0)
        self.assertEqual(metadata["data_kind"], "ticks")
        self.assertEqual(metadata["resolved_config"]["price_type"], "bid_ask")
        with (request.output / "daily_pnl.csv").open() as source:
            daily = list(csv.DictReader(source))
        self.assertEqual(len(daily), 1)
        self.assertAlmostEqual(math.fsum(float(row["net_pnl"]) for row in daily), result.equity[-1].equity - 10000)
        with (request.output / "fills.csv").open() as source:
            reader = csv.DictReader(source)
            self.assertIn("signal_tick_sequence", reader.fieldnames)
        self.assertTrue((request.output / "report.html").is_file())

    def test_tick_evaluation_splits_time_without_aggregation(self):
        self.import_sample()
        request = RunRequest(data=self.dataset, output=self.root / "evaluation", parameters={"fast": 2, "slow": 3}, warmup_bars=3)
        report = evaluate(request, STAMP + timedelta(seconds=1), STAMP + timedelta(seconds=2, milliseconds=500))
        self.assertEqual([report["segments"][label]["summary"]["ticks_processed"] for label in ("train", "validation", "test")], [3, 5, 3])
        test_doc = json.loads((request.output / "test/result.json").read_text())
        self.assertEqual(test_doc["metadata"]["warmup_unit"], "ticks")
        self.assertEqual(len(test_doc["equity"]), 3)

    def test_bar_strategy_is_rejected_for_tick_data_and_failure_is_logged(self):
        self.import_sample()
        request = RunRequest(data=self.dataset, output=self.root / "run", strategy="backtester.strategies:create_strategy")
        with self.assertRaisesRegex(ValueError, "on_tick"):
            execute_experiment(request)
        records = ExperimentStore(self.root / "experiments.sqlite3").list()
        self.assertEqual(records[0]["status"], "failed")

    def test_cli_import_inspect_and_run_use_the_tick_engine(self):
        commands = [
            ["import-ticks", "--input", str(ROOT / "examples/quote-ticks.csv"), "--timezone", "Europe/Kyiv",
             "--symbol", "UNSPECIFIED", "--output", str(self.dataset)],
            ["inspect-data", str(self.dataset)],
            ["run", "--data", str(self.dataset), "--params", '{"fast":2,"slow":3}', "--output", str(self.root / "cli-run")],
        ]
        for command in commands:
            result = subprocess.run([sys.executable, "-m", "backtester", *command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
        self.assertEqual(document["data_kind"], "ticks")
        self.assertEqual(document["ticks_processed"], 11)
