import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from backtester import run_tick_backtest
from backtester.experiments import ExperimentStore, RunRequest, evaluate, execute_experiment
from backtester.datasets import parse_boundary
from backtester.strategy_loader import load_strategy
from backtester.tick_data import TickCsvProfile, TickDatasetSpec, import_ticks, load_ticks

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '''from dataclasses import dataclass
import sys

created = 0

@dataclass
class Strategy:
    fraction: float = 1.0

    def on_tick(self, history):
        assert __name__ in sys.modules
        return self.fraction if len(history) == 1 else None

def create_strategy(fraction=1.0):
    global created
    created += 1
    assert created == 1, "module state leaked across experiments"
    return Strategy(fraction)

def build(fraction=1.0):
    return Strategy(fraction)
'''


class StrategyFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="strategy files ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "my strategy.py"
        self.file.write_text(SOURCE)
        self.data = self.root / "ticks"
        import_ticks(ROOT / "examples/quote-ticks.csv", self.data,
                     TickDatasetSpec(symbol="UNSPECIFIED", source="fixture"), TickCsvProfile(timezone="Europe/Kyiv"))

    def test_cli_loads_absolute_path_with_spaces_and_records_exact_source(self):
        output = self.root / "absolute-run"
        result = subprocess.run([sys.executable, "-m", "backtester", "run", "--data", str(self.data),
                                 "--strategy", str(self.file), "--params", '{"fraction":0.5}',
                                 "--output", str(output)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["fill_count"], 1)
        document = json.loads((output / "result.json").read_text())
        self.assertEqual(document["summary"]["ticks_processed"], 11)
        self.assertAlmostEqual(document["fills"][0]["quantity"], 5000 / 24306.1)
        metadata = document["metadata"]
        self.assertEqual(metadata["strategy_file"], str(self.file.resolve()))
        self.assertEqual(metadata["strategy"], str(self.file.resolve()) + ":create_strategy")
        self.assertEqual(metadata["strategy_source_sha256"], hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_cli_loads_relative_file_without_module_installation(self):
        result = subprocess.run([sys.executable, "-m", "backtester", "run", "--data", str(self.data),
                                 "--strategy", self.file.name, "--output", str(self.root / "relative-run")],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["fill_count"], 1)

    def test_named_factory_and_class_entry_points_receive_parameters(self):
        for entry in ("build", "Strategy"):
            with self.subTest(entry=entry), load_strategy(f"{self.file}:{entry}", "ticks") as loaded:
                strategy = loaded.factory(fraction=0.25)
                result = run_tick_backtest(load_ticks(self.data), strategy)
                self.assertAlmostEqual(result.fills[0].quantity, 2500 / 24306.1)

    def test_reloaded_file_uses_new_source_even_with_same_size_and_mtime(self):
        template = ('TARGET = VALUE\n'
                    'class Strategy:\n'
                    '    def on_tick(self, history):\n'
                    '        return TARGET if len(history) == 1 else None\n'
                    'def create_strategy():\n'
                    '    return Strategy()\n')
        self.file.write_text(template.replace("VALUE", "1.0"))
        stat = self.file.stat()
        with load_strategy(str(self.file), "ticks") as loaded:
            first = run_tick_backtest(load_ticks(self.data), loaded.factory())
            first_hash = loaded.source_sha256
        self.file.write_text(template.replace("VALUE", "0.0"))
        os.utime(self.file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with load_strategy(str(self.file), "ticks") as loaded:
            second = run_tick_backtest(load_ticks(self.data), loaded.factory())
            self.assertNotEqual(first_hash, loaded.source_sha256)
        self.assertEqual(len(first.fills), 1)
        self.assertEqual(second.fills, ())

    def test_temporary_modules_are_cleaned_up_after_success_and_failure(self):
        def modules():
            return {name for name in sys.modules if name.startswith("_backtester_strategy_")}
        before = modules()
        paths = list(sys.path)
        with load_strategy(str(self.file), "ticks") as loaded:
            self.assertGreater(len(modules()), len(before))
            run_tick_backtest(load_ticks(self.data), loaded.factory())
        self.assertEqual(modules(), before)
        self.assertEqual(sys.path, paths)
        self.file.write_text('raise RuntimeError("broken strategy import")\n')
        with self.assertRaisesRegex(RuntimeError, "broken strategy import"):
            with load_strategy(str(self.file), "ticks"):
                self.fail("broken module must not be yielded")
        self.assertEqual(modules(), before)

    def test_missing_file_or_missing_factory_fails_clearly(self):
        with self.assertRaises(FileNotFoundError):
            with load_strategy(str(self.root / "missing.py")):
                self.fail("missing file must fail")
        self.file.write_text("create_strategy = 42\n")
        with self.assertRaisesRegex(ValueError, "must define callable 'create_strategy'"):
            with load_strategy(str(self.file)):
                self.fail("missing callable must fail")

    def test_syntax_error_is_logged_and_cli_returns_an_error_without_traceback(self):
        self.file.write_text("def create_strategy(:\n")
        result = subprocess.run([sys.executable, "-m", "backtester", "run", "--data", str(self.data),
                                 "--strategy", str(self.file), "--output", str(self.root / "broken-run")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid syntax", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        record = ExperimentStore(self.root / "experiments.sqlite3").list()[0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("SyntaxError", record["error"])

    def test_evaluation_reloads_the_file_and_resets_module_globals_per_segment(self):
        request = RunRequest(data=self.data, output=self.root / "evaluation", strategy=str(self.file))
        report = evaluate(request, parse_boundary("2025-10-13T00:00:01Z"), parse_boundary("2025-10-13T00:00:02.500Z"))
        self.assertEqual([report["segments"][label]["summary"]["fill_count"] for label in ("train", "validation", "test")], [1, 1, 1])
        for label in report["segments"]:
            metadata = json.loads((request.output / label / "result.json").read_text())["metadata"]
            self.assertEqual(metadata["strategy_file"], str(self.file))

    def test_file_loading_also_supports_the_bar_engine(self):
        self.file.write_text(SOURCE.replace("on_tick", "on_bar"))
        request = RunRequest(data=ROOT / "examples/bars.csv", output=self.root / "bars", strategy=str(self.file))
        result, _ = execute_experiment(request)
        self.assertEqual(result.summary()["bars_processed"], 10)
        self.assertEqual(len(result.fills), 1)
