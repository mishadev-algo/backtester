from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from backtester.walk_forward import add_months, walk_forward_windows


class WalkForwardTests(unittest.TestCase):
    def test_calendar_windows_only_select_using_preceding_data(self):
        zone = ZoneInfo("Europe/Kyiv")
        start = datetime(2021, 10, 14, 3, tzinfo=zone)
        end = datetime(2026, 10, 14, 3, tzinfo=zone)
        windows = walk_forward_windows(start, end)
        self.assertEqual(len(windows), 6)
        self.assertEqual(windows[0].validation_start, datetime(2023, 4, 14, 3, tzinfo=zone))
        self.assertEqual(windows[0].test_start, datetime(2023, 10, 14, 3, tzinfo=zone))
        for previous, following in zip(windows, windows[1:]):
            self.assertEqual(previous.test_end, following.test_start)
            self.assertEqual(add_months(previous.fit_start, 6), following.fit_start)
        for window in windows:
            self.assertLess(window.fit_start, window.validation_start)
            self.assertLess(window.validation_start, window.test_start)
            self.assertLess(window.test_start, window.test_end)
        self.assertEqual(windows[-1].test_end, end)

    def test_short_trailing_period_keeps_preceding_frozen_set(self):
        zone = ZoneInfo("Europe/Kyiv")
        windows = walk_forward_windows(datetime(2021, 10, 14, tzinfo=zone),
                                       datetime(2026, 10, 15, tzinfo=zone))
        self.assertEqual(len(windows), 6)
        self.assertEqual(windows[-1].test_end, datetime(2026, 10, 15, tzinfo=zone))

    def test_month_arithmetic_and_dst_are_calendar_based(self):
        zone = ZoneInfo("Europe/Kyiv")
        self.assertEqual(add_months(datetime(2024, 1, 31, tzinfo=zone), 1).day, 29)
        winter = datetime(2026, 1, 14, tzinfo=zone)
        summer = add_months(winter, 6)
        self.assertEqual(winter.utcoffset().total_seconds(), 7200)
        self.assertEqual(summer.utcoffset().total_seconds(), 10800)
