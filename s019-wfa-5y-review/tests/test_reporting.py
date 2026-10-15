from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from backtester.models import BacktestConfig, BacktestResult, EquityPoint
from backtester.reporting import _chart_series, _write_json_report, write_report
from backtester import Tick, run_tick_backtest
from backtester.order_engine import BrokerSpec, Order


def result_with_equity(values):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = tuple(EquityPoint(start + timedelta(seconds=i), 100, 0, value, i)
                   for i, value in enumerate(values))
    return BacktestResult(BacktestConfig(initial_cash=100), points, (), False, "ticks")


class ReportingTests(unittest.TestCase):
    def test_order_reports_keep_utc_and_export_correct_broker_days_and_offsets(self):
        class Strategy:
            execution_mode = "orders"
            broker_spec = BrokerSpec(symbol="TEST", account_currency="USD", server_timezone="Europe/Kyiv",
                                     point=1, tick_size=1, tick_value_per_lot=1, digits=0,
                                     volume_min=1, volume_max=100, volume_step=1)

            def on_tick(self, history, broker):
                if broker.position is None:
                    broker.submit(Order("buy", 1, 80, 150))
                else:
                    broker.close_position("manual")

        for start, server_open, server_close, day in (
            ("2026-01-05T21:59:59+00:00", "2026-01-05T23:59:59+02:00", "2026-01-06T00:00:00+02:00", "2026-01-06"),
            ("2026-07-06T20:59:59+00:00", "2026-07-06T23:59:59+03:00", "2026-07-07T00:00:00+03:00", "2026-07-07"),
        ):
            with self.subTest(start=start), tempfile.TemporaryDirectory() as temporary:
                stamp = datetime.fromisoformat(start)
                result = run_tick_backtest([Tick(stamp, 100, 101, sequence=0),
                                           Tick(stamp + timedelta(seconds=1), 110, 111, sequence=1)],
                                          Strategy(), BacktestConfig(initial_cash=100))
                output = Path(temporary) / "report"
                write_report(result, output)
                fills = list(csv.DictReader((output / "fills.csv").open()))
                self.assertEqual(fills[0]["timestamp"], start)
                self.assertEqual(fills[0]["server_time"], server_open)
                self.assertEqual(fills[1]["server_time"], server_close)
                self.assertEqual(fills[0]["signal_server_time"], server_open)
                events = list(csv.DictReader((output / "events.csv").open()))
                self.assertEqual(events[-1]["server_day"], day)
                document = json.loads((output / "result.json").read_text())
                self.assertEqual(document["events"][0]["server_time"], server_open)
                server_pnl = list(csv.DictReader((output / "daily_pnl_server.csv").open()))
                self.assertEqual([row["date"] for row in server_pnl], [server_open[:10], day])
                self.assertEqual([float(row["net_pnl"]) for row in server_pnl], [-1, 10])
                self.assertAlmostEqual(sum(float(row["net_pnl"]) for row in server_pnl), result.equity[-1].equity - 100)
                html = (output / "report.html").read_text()
                self.assertTrue("Broker server time (Europe\\u002fKyiv)" in html)
                self.assertIn("canonical_timestamps", (output / "result.json").read_text())

    def test_chart_keeps_worst_drawdown_even_when_not_equity_minimum(self):
        result = result_with_equity([100, 50, 1000, 60, 500, 100])
        chart = _chart_series(result, max_points=5)
        by_time = {point[0]: point for point in chart}
        self.assertEqual(len(chart), 5)
        for index in (0, 1, 2, 3, 5):
            self.assertIn(result.equity[index].timestamp, by_time)
        self.assertAlmostEqual(by_time[result.equity[1].timestamp][2], -50)
        self.assertAlmostEqual(by_time[result.equity[3].timestamp][2], -94)
        self.assertAlmostEqual(result.summary()["max_drawdown"], .94)

    def test_small_chart_retains_every_tick_and_initial_capital_peak(self):
        result = result_with_equity([95, 90, 110, 99])
        chart = _chart_series(result)
        self.assertEqual([point[0] for point in chart], [point.timestamp for point in result.equity])
        for actual, expected in zip((point[2] for point in chart), (-5, -10, 0, -10)):
            self.assertAlmostEqual(actual, expected)

    def test_report_exports_every_row_while_chart_is_bounded(self):
        result = result_with_equity(100 + (i % 157) for i in range(10_001))
        metadata = {"original": True}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report"
            write_report(result, output, metadata)
            document = json.loads((output / "result.json").read_text())
            self.assertEqual(len(document["equity"]), 10_001)
            self.assertEqual(document["equity"][-1]["tick_sequence"], 10_000)
            self.assertEqual(document["summary"], result.summary())
            self.assertEqual(len((output / "equity.csv").read_text().splitlines()), 10_002)
            sampling = document["metadata"]["chart_sampling"]
            self.assertEqual(sampling["source_points"], 10_001)
            self.assertLessEqual(sampling["rendered_points"], 5_000)
            self.assertIn("exports contain every point", (output / "report.html").read_text())
        self.assertEqual(metadata, {"original": True})
        chart = _chart_series(result)
        self.assertEqual(chart[0][0], result.equity[0].timestamp)
        self.assertEqual(chart[-1][0], result.equity[-1].timestamp)
        self.assertEqual([point[0] for point in chart], sorted(point[0] for point in chart))

    def test_json_output_does_not_materialize_an_equity_dictionary_per_tick(self):
        result = result_with_equity(100 + i for i in range(20_000))
        summary = result.summary()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            tracemalloc.start()
            try:
                _write_json_report(result, path, {}, summary)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 2_000_000)
            document = json.loads(path.read_text())
            self.assertEqual(len(document["equity"]), 20_000)
            self.assertEqual(document["equity"][0]["timestamp"], result.equity[0].timestamp.isoformat())
            self.assertEqual(document["equity"][-1]["equity"], result.equity[-1].equity)


if __name__ == "__main__":
    unittest.main()
