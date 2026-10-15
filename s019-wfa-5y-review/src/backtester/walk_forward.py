"""Calendar walk-forward windows and continuous, sampled OOS accounting."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta

from .datasets import export_timezone
from .s019_search import EPOCH


def add_months(stamp: datetime, months: int):
    ordinal = stamp.year * 12 + stamp.month - 1 + months
    year, month = divmod(ordinal, 12)
    return stamp.replace(year=year, month=month+1, day=min(stamp.day, monthrange(year, month+1)[1]))


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    fit_start: datetime
    validation_start: datetime
    test_start: datetime
    test_end: datetime


def walk_forward_windows(start: datetime, end: datetime, *, fit_months=18, validation_months=6, test_months=6):
    if start.utcoffset() is None or end.utcoffset() is None or start >= end:
        raise ValueError("window dates must be timezone-aware and increasing")
    if min(fit_months, validation_months, test_months) < 1:
        raise ValueError("window month counts must be positive")
    windows = []
    index = 0
    while True:
        fit = add_months(start, index * test_months)
        validation = add_months(fit, fit_months)
        test = add_months(fit, fit_months + validation_months)
        if test >= end:
            break
        stop = min(add_months(test, test_months), end)
        # Do not create a new optimization for only a few trailing days.
        # Extend the preceding frozen set; this rule depends only on dates.
        if windows and stop - test < timedelta(days=30):
            previous = windows[-1]
            windows[-1] = WalkForwardWindow(previous.index, previous.fit_start,
                                            previous.validation_start, previous.test_start, stop)
            break
        windows.append(WalkForwardWindow(index, fit, validation, test, stop))
        index += 1
    return windows


def warmup_index(tape, start, bars=10000):
    import numpy as np
    bar = int(np.searchsorted(tape.bar_starts, start, side="right")) - 1
    return int(tape.bar_starts[max(0, bar-bars)])


class EquitySamples:
    """Keep first/last/extreme points supplied by the all-quote runner.

    Each skipped interval is contained in one M20 bar, so the samples retain
    server-day boundaries. Daily PnL therefore telescopes exactly. Max DD is
    computed separately on every source quote by EquityState.
    """
    def __init__(self, tape, server_timezone, initial_cash=100000.):
        self.tape = tape
        self.zone = export_timezone(server_timezone)
        self.points = []
        self.daily = {}
        self.previous = initial_cash

    def __call__(self, indices, values):
        for index, value in zip(indices, values):
            index, value = int(index), float(value)
            if self.points and index < self.points[-1][0]:
                raise ValueError("equity observations cannot move backwards")
            stamp = EPOCH + timedelta(microseconds=int(self.tape.timestamps[index]))
            day = stamp.astimezone(self.zone).date().isoformat()
            self.daily[day] = self.daily.get(day, 0.) + value-self.previous
            self.previous = value
            if self.points and index == self.points[-1][0]:
                self.points[-1] = (index, value)
            else:
                self.points.append((index, value))
