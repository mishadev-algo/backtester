from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from backtester import BacktestConfig, Bar, run_backtest
from backtester.datasets import DatasetSpec, import_csv, parse_boundary
from backtester.experiments import ExperimentStore, RunRequest, evaluate, execute_experiment

ROOT = Path(__file__).resolve().parents[1]


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "experiments.sqlite3"
        self.request = RunRequest(data=ROOT / "examples/bars.csv", output=self.root / "run", parameters={"fast": 2, "slow": 3})

    def test_success_records_identity_and_generates_standalone_chart(self):
        result, metadata = execute_experiment(self.request, self.registry)
        rows = ExperimentStore(self.registry).list()
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertEqual(rows[0]["run_id"], metadata["run_id"])
        self.assertEqual(json.loads(rows[0]["summary_json"]), result.summary())
        self.assertEqual(len(metadata["engine_source_sha256"]), 64)
        self.assertEqual(len(metadata["strategy_source_sha256"]), 64)
        self.assertIn("pyarrow", metadata["dependencies"])
        html = (self.request.output / "report.html").read_text()
        self.assertIn("Plotly.newPlot", html)
        self.assertNotIn('<script src="https://cdn.plot.ly', html)

    def test_bad_strategy_is_recorded_as_failed_without_report(self):
        request = self.request.model_copy(update={"strategy": "missing_strategy_module:create_strategy"})
        with self.assertRaises(ModuleNotFoundError):
            execute_experiment(request, self.registry)
        rows = ExperimentStore(self.registry).list()
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("ModuleNotFoundError", rows[0]["error"])
        self.assertFalse(request.output.exists())

    def test_finished_record_cannot_be_overwritten(self):
        result, metadata = execute_experiment(self.request, self.registry)
        with self.assertRaisesRegex(ValueError, "already finished"):
            ExperimentStore(self.registry).finish(metadata["run_id"], error="overwrite")
        self.assertEqual(ExperimentStore(self.registry).list()[0]["status"], "succeeded")

    def test_imported_bid_dataset_controls_execution_reference(self):
        source = self.root / "prices.csv"
        source.write_text("timestamp,open,high,low,close\n2026-01-01,100,100,100,100\n2026-01-02,100,100,100,100\n2026-01-03,100,100,100,100\n")
        data = self.root / "dataset"
        import_csv(source, data, DatasetSpec(symbol="TEST", timeframe="D1", source="fixture", price_type="bid"))
        request = self.request.model_copy(update={"data": data, "parameters": {"fast": 1, "slow": 2},
                                                  "config": BacktestConfig(initial_cash=1000, spread_bps=100)})
        # Direct engine reference-price cases are calculated independently of the MA signal.
        class BuyThenSell:
            def on_bar(self, history):
                return {1: 1.0, 2: 0.0}.get(len(history))
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [Bar(stamp + timedelta(days=i), 100, 100, 100, 100) for i in range(3)]
        for price_type, prices in (("bid", (101, 100)), ("ask", (100, 99)), ("mid", (100.5, 99.5))):
            result = run_backtest(bars, BuyThenSell(), BacktestConfig(initial_cash=1000, spread_bps=100, price_type=price_type))
            self.assertEqual(len(result.fills), 2)
            for fill, expected_price in zip(result.fills, prices):
                self.assertAlmostEqual(fill.price, expected_price)
            self.assertAlmostEqual(result.equity[-1].equity, 1000 / prices[0] * prices[1])
        result, metadata = execute_experiment(request, self.registry)
        self.assertEqual(result.config.price_type, "bid")
        self.assertEqual(metadata["resolved_config"]["price_type"], "bid")

    def test_warmup_carries_target_and_never_trades_before_period(self):
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [Bar(stamp + timedelta(days=i), 100, 100, 100, 100) for i in range(4)]
        views = []
        class Strategy:
            def on_bar(self, history):
                views.append(tuple(history))
                return 1.0 if len(history) == 1 else None
        result = run_backtest(bars[2:], Strategy(), BacktestConfig(initial_cash=1000), warmup_bars=bars[:2])
        self.assertEqual([len(view) for view in views], [1, 2, 3, 4])
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].timestamp, bars[2].timestamp)
        self.assertEqual(len(result.equity), 2)
        self.assertEqual(result.equity[0].quantity, 10)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_backtest(bars[1:], Strategy(), warmup_bars=bars[:2])

    def test_evaluation_has_disjoint_periods_fresh_accounts_and_fixed_parameters(self):
        request = self.request.model_copy(update={"output": self.root / "evaluation", "warmup_bars": 3})
        report = evaluate(request, parse_boundary("2026-01-05"), parse_boundary("2026-01-08"), self.registry)
        self.assertEqual(set(report["segments"]), {"train", "validation", "test"})
        expected = {"train": ("2026-01-01", "2026-01-04", 4), "validation": ("2026-01-05", "2026-01-07", 3),
                    "test": ("2026-01-08", "2026-01-10", 3)}
        for label, (first, last, count) in expected.items():
            summary = report["segments"][label]["summary"]
            self.assertTrue(summary["first_timestamp"].startswith(first))
            self.assertTrue(summary["last_timestamp"].startswith(last))
            self.assertEqual(summary["bars_processed"], count)
            self.assertEqual(summary["initial_cash"], 10_000)
            document = json.loads((request.output / label / "result.json").read_text())
            self.assertEqual(document["metadata"]["parameters"], {"fast": 2, "slow": 3})
            self.assertEqual(document["metadata"]["warmup_bars_used"], 0 if label == "train" else 3)
        self.assertTrue((request.output / "evaluation.json").is_file())
        self.assertEqual(len(ExperimentStore(self.registry).list()), 3)

    def test_empty_test_period_fails_before_any_experiment_runs(self):
        request = self.request.model_copy(update={"output": self.root / "evaluation"})
        with self.assertRaisesRegex(ValueError, "no price bars"):
            evaluate(request, parse_boundary("2026-01-05"), parse_boundary("2026-02-01"), self.registry)
        self.assertFalse(request.output.exists())
        self.assertFalse(self.registry.exists())

    def test_negative_warmup_and_naive_boundary_are_rejected(self):
        for values in ({"warmup_bars": -1}, {"start": datetime(2026, 1, 1)}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RunRequest(data=self.request.data, output=self.request.output, **values)

    def test_bar_open_labels_do_not_backdate_signals_or_close_equity(self):
        from backtester.datasets import load_dataset
        spec = DatasetSpec(symbol="TEST", timeframe="D1", source="fixture", price_type="mid")
        import_csv(ROOT / "examples/bars.csv", self.root / "dataset", spec)
        bars = load_dataset(self.root / "dataset")
        class BuyOnce:
            def on_bar(self, history):
                return 1.0 if len(history) == 1 else None
        result = run_backtest(bars[:2], BuyOnce())
        self.assertEqual(result.fills[0].signal_timestamp, bars[0].close_timestamp)
        self.assertEqual(result.fills[0].timestamp, bars[1].open_timestamp)
        self.assertEqual(result.equity[0].timestamp, bars[0].close_timestamp)
        self.assertEqual(result.equity[1].timestamp, bars[1].close_timestamp)
        from dataclasses import replace
        overlapping = replace(bars[1], open_timestamp=bars[1].open_timestamp - timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, "overlap"):
            run_backtest([bars[0], overlapping], BuyOnce())
