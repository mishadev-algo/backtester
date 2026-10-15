from __future__ import annotations

from collections.abc import Iterable
import math

from .models import BacktestConfig, BacktestResult, Bar, EquityPoint, Fill, History, Strategy, finite


def run_backtest(bars: Iterable[Bar], strategy: Strategy,
                 config: BacktestConfig | None = None, *, warmup_bars: Iterable[Bar] = ()) -> BacktestResult:
    """Simulate one asset, long/flat, with fractional units and next-open fills."""
    config = config or BacktestConfig()
    if config.price_type == "bid_ask":
        raise ValueError("bid_ask quotes require the tick engine")
    cash = config.initial_cash
    quantity = 0.0
    pending: tuple[float, Bar] | None = None
    observed: list[Bar] = []
    fills: list[Fill] = []
    points: list[EquityPoint] = []
    buy_spread = config.spread_bps if config.price_type == "bid" else (0 if config.price_type == "ask" else config.spread_bps / 2)
    sell_spread = config.spread_bps if config.price_type == "ask" else (0 if config.price_type == "bid" else config.spread_bps / 2)
    buy_impact = (buy_spread + config.slippage_bps) / 10_000
    sell_impact = (sell_spread + config.slippage_bps) / 10_000
    fee_rate = config.commission_bps / 10_000

    def decision(bar):
        target = strategy.on_bar(History(observed))
        if target is None:
            return None
        finite(target, "strategy target")
        if not 0 <= target <= 1:
            raise ValueError("strategy target must be between 0 and 1")
        return (float(target), bar)

    for bar in warmup_bars:
        if not isinstance(bar, Bar):
            raise ValueError("warmup bars must contain Bar instances")
        if observed and bar.timestamp <= observed[-1].timestamp:
            raise ValueError("warmup bars must be strictly increasing")
        observed.append(bar)
        # Warmup updates indicators but never creates trades, fees, or equity observations.
        # Carry the last requested target into the fresh account at the evaluation boundary.
        target = decision(bar)
        if target is not None:
            pending = target

    for bar in bars:
        if not isinstance(bar, Bar):
            raise ValueError("bars must contain Bar instances")
        if observed and bar.timestamp <= observed[-1].timestamp:
            raise ValueError("bars must have strictly increasing, unique timestamps")
        if observed and bar.open_timestamp is not None and observed[-1].close_timestamp is not None:
            if bar.open_timestamp < observed[-1].close_timestamp:
                raise ValueError("bar intervals overlap; check timeframe and timestamp convention")
        if pending is not None:
            target, signal_bar = pending
            open_equity = cash + quantity * bar.open
            desired = target * open_equity / bar.open
            delta = desired - quantity
            if not math.isclose(delta, 0, abs_tol=1e-12):
                price = bar.open * (1 + buy_impact if delta > 0 else 1 - sell_impact)
                if delta > 0:
                    # Costs must not turn a fully invested long position into borrowing.
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
                    fills.append(Fill(bar.open_timestamp or bar.timestamp, signal_bar.close_timestamp or signal_bar.timestamp,
                                      "buy" if delta > 0 else "sell", abs(delta), bar.open, price, commission))
        pending = None
        equity = cash + quantity * bar.close
        finite(equity, "account equity")
        points.append(EquityPoint(bar.close_timestamp or bar.timestamp, cash, quantity, equity))
        observed.append(bar)
        pending = decision(bar)

    if not points:
        raise ValueError("at least one price bar is required")
    return BacktestResult(config, tuple(points), tuple(fills), pending is not None)
