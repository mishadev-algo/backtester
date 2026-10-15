import csv
from datetime import timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from backtester.data import load_csv
from backtester import run_backtest
from backtester.reporting import write_report
from backtester.strategies import MovingAverageCross

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_cli_generates_reconciled_reports_and_records_input_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'run'
            result = subprocess.run([
                sys.executable, '-m', 'backtester', '--data', str(ROOT / 'examples/bars.csv'),
                '--params', '{"fast":2,"slow":3}', '--commission-bps', '5',
                '--spread-bps', '4', '--slippage-bps', '2', '--output', str(output),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            printed = json.loads(result.stdout)
            document = json.loads((output / 'result.json').read_text())
            self.assertEqual(printed, document['summary'])
            self.assertEqual(printed['bars_processed'], 10)
            self.assertGreater(printed['fill_count'], 0)
            self.assertGreater(printed['commissions_paid'], 0)
            self.assertEqual(len(document['metadata']['data_sha256']), 64)
            with (output / 'daily_pnl.csv').open() as source:
                reader = csv.DictReader(source)
                self.assertEqual(reader.fieldnames, ['date', 'net_pnl'])
                total = math.fsum(float(row['net_pnl']) for row in reader)
            self.assertAlmostEqual(total, printed['final_equity'] - printed['initial_cash'])
            self.assertTrue((output / 'fills.csv').is_file())
            self.assertTrue((output / 'equity.csv').is_file())

    def test_custom_strategy_factory_is_callable_from_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'custom_strategy.py').write_text(
                'class Buy:\n'
                '    def on_bar(self, history):\n'
                '        return 1.0 if len(history) == 1 else None\n'
                'def create_strategy():\n'
                '    return Buy()\n')
            result = subprocess.run([
                sys.executable, '-m', 'backtester', '--data', str(ROOT / 'examples/bars.csv'),
                '--strategy', 'custom_strategy:create_strategy', '--output', str(root / 'output'),
            ], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['fill_count'], 1)

    def test_existing_reports_are_preserved(self):
        bars = load_csv(ROOT / 'examples/bars.csv')
        result = run_backtest(bars, MovingAverageCross(2, 3))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'result'
            write_report(result, output)
            original = (output / 'result.json').read_bytes()
            with self.assertRaises(FileExistsError):
                write_report(result, output)
            self.assertEqual((output / 'result.json').read_bytes(), original)

    def test_csv_normalizes_offset_timestamps_and_accepts_missing_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'prices.csv'
            source.write_text('timestamp,open,high,low,close\n2026-01-01T01:00:00+01:00,100,100,100,100\n')
            bars = load_csv(source)
            self.assertEqual(bars[0].timestamp.hour, 0)
            self.assertEqual(bars[0].timestamp.tzinfo, timezone.utc)
            self.assertEqual(bars[0].volume, 0)

    def test_bad_csv_rows_fail_with_line_number(self):
        for row in ('2026-01-01T12:00:00,100,100,100,100',
                    '2026-01-01,nan,100,100,100',
                    '2026-01-01,100,90,100,100',
                    '2026-01-01,100,100,100,100,EXTRA'):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / 'prices.csv'
                source.write_text('timestamp,open,high,low,close\n' + row + '\n')
                with self.assertRaisesRegex(ValueError, 'CSV line 2'):
                    load_csv(source)

    def test_equivalent_offset_timestamps_are_rejected_as_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'prices.csv'
            source.write_text('timestamp,open,high,low,close\n'
                              '2026-01-01T00:00:00Z,100,100,100,100\n'
                              '2026-01-01T01:00:00+01:00,100,100,100,100\n')
            with self.assertRaisesRegex(ValueError, 'unique'):
                load_csv(source)

    def test_bad_cli_parameters_exit_nonzero_without_creating_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'result'
            result = subprocess.run([
                sys.executable, '-m', 'backtester', '--data', str(ROOT / 'examples/bars.csv'),
                '--params', '{"fast":3,"slow":2}', '--output', str(output),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('0 < fast < slow', result.stderr)
            self.assertFalse(output.exists())
