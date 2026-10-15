"""Event-indexed search runner for the bundled S-019 M20 implementation.

Uses the ordinary strategy and OrderBroker. Every quote remains available for
first-crossing execution and full-resolution drawdown. Only callbacks between
state changes are skipped; this is not an OHLC execution model.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .models import BacktestConfig, Tick
from .order_engine import OrderBroker
from .tick_data import inspect_ticks

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def fills_fingerprint(fills):
    rows = [[fill.timestamp.isoformat(), fill.signal_timestamp.isoformat(), fill.side,
             fill.quantity, fill.reference_price, fill.price, fill.commission,
             fill.tick_sequence, fill.signal_tick_sequence, fill.reason,
             fill.order_id, fill.position_id, fill.quantity_unit] for fill in fills]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def microseconds(stamp):
    delta = stamp.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass
class QuoteTape:
    timestamps: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    sequence: np.ndarray

    def __post_init__(self):
        if not len(self.bid) or not (len(self.timestamps) == len(self.bid) == len(self.ask) == len(self.sequence)):
            raise ValueError("quote arrays must be nonempty and have equal length")
        if np.any(np.diff(self.timestamps) < 0) or np.any(np.diff(self.sequence) <= 0):
            raise ValueError("quotes must preserve time and source sequence")
        if not (np.isfinite(self.bid).all() and np.isfinite(self.ask).all()) or np.any(self.bid <= 0) or np.any(self.ask < self.bid):
            raise ValueError("invalid bid/ask quotes")
        buckets = self.timestamps // 1_200_000_000
        self.bar_starts = np.r_[0, np.flatnonzero(np.diff(buckets)) + 1]

    @classmethod
    def from_dataset(cls, path: Path | str):
        path = Path(path)
        manifest = inspect_ticks(path)
        table = pq.read_table(path / "ticks.parquet", columns=["timestamp", "bid", "ask", "sequence"])
        arrays = {name: table[name].combine_chunks().to_numpy() for name in table.column_names}
        return cls(arrays["timestamp"].astype("datetime64[us]").astype(np.int64),
                   arrays["bid"], arrays["ask"], arrays["sequence"]), manifest

    @classmethod
    def from_ticks(cls, ticks):
        return cls(np.array([microseconds(t.timestamp) for t in ticks], dtype=np.int64),
                   np.array([t.bid for t in ticks]), np.array([t.ask for t in ticks]),
                   np.array([t.sequence if t.sequence is not None else i for i, t in enumerate(ticks)], dtype=np.int64))

    def tick(self, index):
        return Tick(EPOCH + timedelta(microseconds=int(self.timestamps[index])),
                    float(self.bid[index]), float(self.ask[index]), sequence=int(self.sequence[index]))

    def boundary(self, stamp):
        return int(np.searchsorted(self.timestamps, microseconds(stamp)))


@dataclass
class EquityState:
    peak: float
    max_drawdown: float = 0.
    max_drawdown_usd: float = 0.


def run_search_trial(tape: QuoteTape, strategy, config: BacktestConfig, start: int, end: int, *, warmup_start=0,
                     broker: OrderBroker | None = None, equity_state: EquityState | None = None,
                     equity_observer=None, continue_account=False):
    """Fresh account, past-only warmup, all-quote execution/marking for S-019.

    Restricted to M20 with minute-aligned broker offsets. The strategy must
    use only its current quote and its own Candle state, as bundled S-019 does.
    Source volume is not used by that strategy/broker.
    """
    if strategy.__class__.__name__ != "S019" or strategy.seconds != 1200:
        raise ValueError("event-indexed search supports only the bundled S019 M20")
    if not 0 <= warmup_start <= start < end <= len(tape.bid):
        raise ValueError("invalid warmup or evaluation boundaries")
    for index in [warmup_start, start, end - 1]:
        offset = tape.tick(index).timestamp.astimezone(strategy.zone).utcoffset().total_seconds()
        if offset % 1200:
            raise ValueError("broker offset must align M20 boundaries with UTC")
    if broker is None:
        broker = OrderBroker(config, strategy.broker_spec)
    elif broker.spec != strategy.broker_spec or (not continue_account and (broker.position is not None or broker.pending is not None)):
        raise ValueError("reused broker must be flat, have no pending order, and use the same contract")
    if continue_account and (start != 0 or equity_state is None):
        raise ValueError("continuation requires start=0 and the prior equity state")
    state = equity_state if equity_state is not None else EquityState(config.initial_cash)
    initial_balance = broker.balance
    peak, max_dd, max_dd_money = state.peak, state.max_drawdown, state.max_drawdown_usd
    visited = 0

    def mark(begin, stop):
        nonlocal peak, max_dd, max_dd_money
        if begin >= stop:
            return
        if broker.position is None:
            equity = np.array([broker.balance])
        else:
            p = broker.position
            quotes = tape.bid[begin:stop] if p.side == "buy" else tape.ask[begin:stop]
            # Match OrderBroker.profit's arithmetic order, including rounding.
            pnl = (1 if p.side == "buy" else -1) * (quotes - p.entry_price) * p.volume * broker.spec.value_per_price_unit
            equity = broker.balance + pnl
        peaks = np.maximum.accumulate(np.maximum(equity, peak))
        drawdown = peaks - equity
        max_dd_money = max(max_dd_money, float(drawdown.max()))
        max_dd = max(max_dd, float((drawdown / peaks).max()))
        peak = float(peaks[-1])
        if equity_observer is not None:
            if len(equity) == 1:
                indices = np.unique([begin, stop - 1])
                equity_observer(indices, np.full(len(indices), equity[0]))
            else:
                selected = np.unique([0, len(equity)-1, int(equity.argmin()), int(equity.argmax()),
                                      int(drawdown.argmax()), int((drawdown / peaks).argmax())])
                equity_observer(begin + selected, equity[selected])

    index = warmup_start
    while index < end:
        tick = tape.tick(index)
        broker.advance(tick, int(tape.sequence[index]), warmup=index < start)
        strategy.on_tick([tick], broker)
        visited += 1
        if index >= start:
            # An execution/strategy callback can change the account on this tick.
            equity = broker.equity()
            peak = max(peak, equity)
            max_dd_money = max(max_dd_money, peak - equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            if equity_observer is not None:
                equity_observer(np.array([index]), np.array([equity]))

        bar = int(np.searchsorted(tape.bar_starts, index, side="right"))
        following = int(tape.bar_starts[bar]) if bar < len(tape.bar_starts) else end
        following = min(following, end)
        if index < start:
            following = min(following, start)
        else:
            deferred = strategy.deferred
            if deferred is not None:
                release = int(np.searchsorted(tape.timestamps, microseconds(deferred.release), side="left"))
                following = min(following, max(index + 1, release))
            # Check the first quote of the next bar as well: server protection
            # and GTC fills happen before that bar's strategy callback.
            stop = min(following + 1, end)
            if index + 1 < stop:
                p = broker.position
                if p is not None:
                    quotes = tape.bid[index + 1:stop] if p.side == "buy" else tape.ask[index + 1:stop]
                    crossed = quotes <= p.stop_loss if p.side == "buy" else quotes >= p.stop_loss
                    if p.take_profit:
                        crossed |= quotes >= p.take_profit if p.side == "buy" else quotes <= p.take_profit
                elif broker.pending is not None:
                    order = broker.pending.request
                    crossed = (tape.ask[index + 1:stop] <= order.limit_price if order.side == "buy"
                               else tape.bid[index + 1:stop] >= order.limit_price)
                else:
                    crossed = None
                if crossed is not None:
                    first = int(np.argmax(crossed))
                    if crossed[first]:
                        following = min(following, index + 1 + first)

        if index >= start:
            mark(index + 1, following)
        # These skipped quotes are now in the past. Update only the currently
        # open Candle, before the next callback can close it or read a signal.
        if index + 1 < following:
            quotes = tape.bid[index + 1:following]
            strategy.current.high = max(strategy.current.high, float(quotes.max()))
            strategy.current.low = min(strategy.current.low, float(quotes.min()))
            strategy.current.close = float(quotes[-1])
        index = following

    # Mark the final quote even if it did not change strategy/broker state.
    broker.tick = tape.tick(end - 1)
    final_equity = broker.equity()
    state.peak, state.max_drawdown, state.max_drawdown_usd = peak, max_dd, max_dd_money
    entries = {}
    profits = []
    for fill in broker.fills:
        if fill.position_id not in entries:
            entries[fill.position_id] = fill
        else:
            opening = entries.pop(fill.position_id)
            profits.append(broker.profit(opening.side, opening.quantity, opening.price, fill.price)
                           - opening.commission - fill.commission)
    gross_profit = math.fsum(p for p in profits if p > 0)
    gross_loss = -math.fsum(p for p in profits if p < 0)
    return {"net_profit_usd": final_equity - config.initial_cash, "final_equity_usd": final_equity,
            "period_net_profit_usd": final_equity - initial_balance,
            "final_cash_usd": broker.balance, "max_drawdown": max_dd,
            "max_drawdown_usd": max_dd_money, "closed_trades": len(profits),
            "wins": sum(p > 0 for p in profits), "open_positions": len(entries),
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "recovery_factor": (final_equity - config.initial_cash) / max(1000., max_dd_money),
            "ticks": end - start, "callbacks": visited,
            "fills_sha256": fills_fingerprint(broker.fills),
            "max_daily_openings": max(Counter(broker.opened_by_day).values(), default=0)}, broker
