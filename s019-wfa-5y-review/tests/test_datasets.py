from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

import pyarrow.parquet as pq

from backtester.datasets import CsvProfile, DatasetSpec, import_csv, inspect_dataset, load_dataset, parse_boundary

ROOT = Path(__file__).resolve().parents[1]


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = DatasetSpec(symbol="SYNTHETIC", timeframe="D1", source="fixture", price_type="mid")

    def test_headerless_export_round_trips_in_multiple_parquet_batches(self):
        profile = CsvProfile.model_validate_json((ROOT / "examples/qdm-csv-profile.json").read_text())
        manifest = import_csv(ROOT / "examples/export-bars.csv", self.root / "dataset", self.spec, profile, batch_size=2)
        verified = inspect_dataset(self.root / "dataset")
        self.assertEqual(manifest, verified)
        self.assertEqual(manifest.rows, 10)
        self.assertEqual(manifest.gap_intervals, 0)
        bars = load_dataset(self.root / "dataset")
        self.assertEqual(bars[0].close, 101)
        self.assertEqual(bars[-1].close, 109)
        self.assertEqual(bars[0].open_timestamp.day, 1)
        self.assertEqual(bars[0].close_timestamp.day, 2)
        self.assertEqual(pq.read_metadata(self.root / "dataset/bars.parquet").num_rows, 10)

    def test_duckdb_period_is_start_inclusive_end_exclusive(self):
        import_csv(ROOT / "examples/bars.csv", self.root / "dataset", self.spec)
        bars = load_dataset(self.root / "dataset", parse_boundary("2026-01-02"), parse_boundary("2026-01-04"))
        self.assertEqual([bar.timestamp.day for bar in bars], [2, 3])
        warmup = load_dataset(self.root / "dataset", end=parse_boundary("2026-01-06"), tail=2)
        self.assertEqual([bar.timestamp.day for bar in warmup], [4, 5])

    def test_close_label_convention_derives_actual_open_and_close(self):
        spec = self.spec.model_copy(update={"timestamp_convention": "bar_close"})
        import_csv(ROOT / "examples/bars.csv", self.root / "dataset", spec)
        bar = load_dataset(self.root / "dataset")[1]
        self.assertEqual(bar.timestamp.day, 2)
        self.assertEqual(bar.open_timestamp.day, 1)
        self.assertEqual(bar.close_timestamp.day, 2)

    def test_semicolon_decimal_comma_and_explicit_column_mapping(self):
        source = self.root / "source.csv"
        source.write_text("Date;Time;Open;High;Low;Close\n2026.01.01;12:00;1,10;1,12;1,09;1,11\n")
        profile = CsvProfile(delimiter=";", decimal_separator=",", timestamp_format="%Y.%m.%d %H:%M", timezone="Europe/Kyiv",
                             columns={"date": "Date", "time": "Time", "open": "Open", "high": "High", "low": "Low", "close": "Close"})
        import_csv(source, self.root / "dataset", self.spec, profile)
        bar = load_dataset(self.root / "dataset")[0]
        self.assertAlmostEqual(bar.close, 1.11)
        self.assertEqual(bar.timestamp.hour, 10)
        self.assertEqual(bar.volume, 0)

    def test_naive_intraday_timestamp_requires_timezone(self):
        source = self.root / "source.csv"
        source.write_text("timestamp,open,high,low,close\n2026-01-01T12:00:00,1,1,1,1\n")
        with self.assertRaisesRegex(ValueError, "explicit profile timezone"):
            import_csv(source, self.root / "dataset", self.spec)
        self.assertFalse((self.root / "dataset").exists())

    def test_ambiguous_and_nonexistent_dst_times_are_rejected(self):
        profile = CsvProfile(timezone="America/New_York")
        for timestamp, message in (("2026-03-08T02:30:00", "nonexistent"), ("2026-11-01T01:30:00", "ambiguous")):
            with self.subTest(timestamp=timestamp):
                source = self.root / "source.csv"
                source.write_text(f"timestamp,open,high,low,close\n{timestamp},1,1,1,1\n")
                with self.assertRaisesRegex(ValueError, message):
                    import_csv(source, self.root / "dataset", self.spec, profile)
                self.assertFalse((self.root / "dataset").exists())

    def test_gap_is_reported_without_inventing_weekend_bars(self):
        source = self.root / "source.csv"
        source.write_text("timestamp,open,high,low,close\n2026-01-02,1,1,1,1\n2026-01-05,1,1,1,1\n")
        manifest = import_csv(source, self.root / "dataset", self.spec)
        self.assertEqual(manifest.rows, 2)
        self.assertEqual(manifest.gap_intervals, 1)
        self.assertEqual(manifest.max_interval_seconds, 3 * 86400)
        self.assertEqual(len(load_dataset(self.root / "dataset")), 2)

    def test_duplicate_and_bad_prices_leave_no_dataset(self):
        for row in ("2026-01-01,1,1,1,1", "2026-01-02,1,0.5,1,1", "2026-01-02,nan,1,1,1"):
            with self.subTest(row=row):
                source = self.root / "source.csv"
                source.write_text("timestamp,open,high,low,close\n2026-01-01,1,1,1,1\n" + row + "\n")
                with self.assertRaisesRegex(ValueError, "CSV line 3"):
                    import_csv(source, self.root / "dataset", self.spec, batch_size=1)
                self.assertFalse((self.root / "dataset").exists())
                self.assertFalse(list(self.root.glob(".dataset-import-*")))

    def test_existing_dataset_is_preserved(self):
        import_csv(ROOT / "examples/bars.csv", self.root / "dataset", self.spec)
        original = (self.root / "dataset/manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            import_csv(ROOT / "examples/bars.csv", self.root / "dataset", self.spec)
        self.assertEqual((self.root / "dataset/manifest.json").read_bytes(), original)

    def test_modified_parquet_is_detected_before_execution(self):
        import_csv(ROOT / "examples/bars.csv", self.root / "dataset", self.spec)
        with (self.root / "dataset/bars.parquet").open("ab") as destination:
            destination.write(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            load_dataset(self.root / "dataset")

    def test_ticks_do_not_silently_become_price_bars(self):
        source = self.root / "ticks.csv"
        source.write_text("timestamp,bid,ask\n2026-01-01T00:00:00Z,1,1.1\n")
        with self.assertRaisesRegex(ValueError, "price/time columns"):
            import_csv(source, self.root / "dataset", self.spec)

    def test_boundaries_and_profiles_reject_ambiguous_configuration(self):
        with self.assertRaisesRegex(ValueError, "explicit timezone"):
            parse_boundary("2026-01-01T12:00:00")
        for kwargs in ({"delimiter": "::"}, {"fieldnames": ["open", "open"]},
                       {"columns": {"timestamp": "time", "date": "date", "open": "open", "high": "high", "low": "low", "close": "close"}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CsvProfile(**kwargs)
