"""Quote-by-quote simulation using the actual bid/ask, in source order."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import math

from .models import BacktestConfig, BacktestResult, EquityPoint, Fill, History, Tick, TickStrategy, finite


def run_tick_backtest(ticks: Iterable[Tick], strategy: TickStrategy, config: BacktestConfig | None = None,
                      *, warmup_ticks: Iterable[Tick] = ()) -> BacktestResult:
    config = config or BacktestConfig()
    if getattr(strategy, "execution_mode", None) == "orders":
        from .order_engine import run_order_backtest
        return run_order_backtest(ticks, strategy, config, warmup_ticks=warmup_ticks)
    if config.spread_bps != 0:
        raise ValueError("tick data already contains bid/ask spread; spread_bps must be zero")
    config = replace(config, price_type="bid_ask")
    cash, quantity = config.initial_cash, 0.0
    observed: list[Tick] = []
    points: list[EquityPoint] = []
    fills: list[Fill] = []
    pending = None
    fee_rate, slippage = config.commission_bps / 10_000, config.slippage_bps / 10_000

    def validate(tick):
        if not isinstance(tick, Tick):
            raise ValueError("tick input must contain Tick instances")
        if observed:
            previous = observed[-1]
            if tick.timestamp < previous.timestamp:
                raise ValueError("ticks must have nondecreasing timestamps in source order")
            if tick.sequence is not None and previous.sequence is not None and tick.sequence <= previous.sequence:
                raise ValueError("tick sequence must be strictly increasing")

    def decision(tick):
        target = strategy.on_tick(History(observed))
        if target is None:
            return None
        finite(target, "strategy target")
        if not 0 <= target <= 1:
            raise ValueError("strategy target must be between 0 and 1")
        sequence = tick.sequence if tick.sequence is not None else len(observed) - 1
        return float(target), tick, sequence

    for tick in warmup_ticks:
        validate(tick)
        observed.append(tick)
        target = decision(tick)
        if target is not None:
            pending = target

    for tick in ticks:
        validate(tick)
        sequence = tick.sequence if tick.sequence is not None else len(observed)
        if pending is not None:
            target, signal, signal_sequence = pending
            equity = cash + quantity * tick.bid
            delta = target * equity / tick.bid - quantity
            if not math.isclose(delta, 0, abs_tol=1e-12):
                reference = tick.ask if delta > 0 else tick.bid
                price = reference * (1 + slippage if delta > 0 else 1 - slippage)
                if delta > 0:
                    delta = min(delta, max(0.0, cash / (price * (1 + fee_rate))))
                else:
                    delta = max(delta, -quantity)
                if abs(delta) > 1e-12:
                    commission = abs(delta) * price * fee_rate
                    cash -= delta * price + commission
                    quantity += delta
                    if abs(cash) < 1e-10:
                        cash = 0.0
                    if abs(quantity) < 1e-12:
                        quantity = 0.0
                    fills.append(Fill(tick.timestamp, signal.timestamp, "buy" if delta > 0 else "sell",
                                      abs(delta), reference, price, commission, sequence, signal_sequence))
        pending = None
        equity = cash + quantity * tick.bid
        finite(equity, "account equity")
        points.append(EquityPoint(tick.timestamp, cash, quantity, equity, sequence))
        observed.append(tick)
        pending = decision(tick)
    if not points:
        raise ValueError("at least one tick is required")
    return BacktestResult(config, tuple(points), tuple(fills), pending is not None, "ticks")
