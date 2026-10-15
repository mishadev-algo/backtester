from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Protocol, TypeVar, overload


def finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


@dataclass(frozen=True, slots=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    open_timestamp: datetime | None = None
    close_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime) or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be a timezone-aware datetime")
        if (self.open_timestamp is None) != (self.close_timestamp is None):
            raise ValueError("provide both open_timestamp and close_timestamp, or neither")
        if self.open_timestamp is not None:
            if self.open_timestamp.utcoffset() is None or self.close_timestamp.utcoffset() is None:
                raise ValueError("bar interval timestamps must be timezone-aware")
            if not self.open_timestamp <= self.timestamp <= self.close_timestamp or self.open_timestamp >= self.close_timestamp:
                raise ValueError("bar label must lie inside a positive open/close interval")
        for name in ("open", "high", "low", "close", "volume"):
            finite(getattr(self, name), name)
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("OHLC prices must satisfy low <= open/close <= high")
        if self.volume < 0:
            raise ValueError("volume must be nonnegative")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_cash: float = 10_000.0
    commission_bps: float = 0.0
    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    price_type: str = "mid"

    def __post_init__(self) -> None:
        for name in ("initial_cash", "commission_bps", "spread_bps", "slippage_bps"):
            finite(getattr(self, name), name)
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if min(self.commission_bps, self.spread_bps, self.slippage_bps) < 0:
            raise ValueError("costs must be nonnegative")
        if self.price_type not in ("mid", "bid", "ask", "last", "bid_ask"):
            raise ValueError("price_type must be mid, bid, ask, last, or bid_ask")
        spread_impact = self.spread_bps if self.price_type in ("bid", "ask") else self.spread_bps / 2
        if spread_impact + self.slippage_bps >= 10_000 or self.commission_bps >= 10_000:
            raise ValueError("execution impact and commission must each be below 100%")


@dataclass(frozen=True, slots=True)
class Tick:
    timestamp: datetime
    bid: float
    ask: float
    volume: float = 0.0
    sequence: int | None = None

    def __post_init__(self):
        if not isinstance(self.timestamp, datetime) or self.timestamp.utcoffset() is None:
            raise ValueError("tick timestamp must be timezone-aware")
        for name in ("bid", "ask", "volume"):
            finite(getattr(self, name), name)
        if self.bid <= 0 or self.ask < self.bid:
            raise ValueError("quotes must satisfy 0 < bid <= ask")
        if self.volume < 0:
            raise ValueError("tick volume must be nonnegative")
        if self.sequence is not None and (type(self.sequence) is not int or self.sequence < 0):
            raise ValueError("tick sequence must be a nonnegative integer")


Observation = TypeVar("Observation")


class History(Sequence[Observation]):
    """Read-only prefix of observations; slicing copies only that slice."""

    __slots__ = ("__bars", "__length")

    def __init__(self, bars: list[Observation]) -> None:
        self.__bars = bars
        self.__length = len(bars)

    def __len__(self) -> int:
        return self.__length

    @overload
    def __getitem__(self, index: int) -> Observation: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Observation, ...]: ...

    def __getitem__(self, index: int | slice) -> Observation | tuple[Observation, ...]:
        if isinstance(index, slice):
            return tuple(self.__bars[i] for i in range(*index.indices(self.__length)))
        if index < 0:
            index += self.__length
        if not 0 <= index < self.__length:
            raise IndexError("bar is outside observed history")
        return self.__bars[index]


class Strategy(Protocol):
    def on_bar(self, history: History) -> float | None:
        """Return a long target fraction in [0, 1], or None to keep holding.

        Decisions made at this bar's close execute at the next bar's open.
        """
        ...


class TickStrategy(Protocol):
    def on_tick(self, history: History[Tick]) -> float | None:
        """Return a target in [0, 1]; execution happens at the next source tick."""
        ...


@dataclass(frozen=True, slots=True)
class Fill:
    timestamp: datetime
    signal_timestamp: datetime
    side: str
    quantity: float
    reference_price: float
    price: float
    commission: float
    tick_sequence: int | None = None
    signal_tick_sequence: int | None = None
    reason: str | None = None
    order_id: int | None = None
    position_id: int | None = None
    quantity_unit: str = "units"


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    cash: float
    quantity: float
    equity: float
    tick_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    equity: tuple[EquityPoint, ...]
    fills: tuple[Fill, ...]
    final_signal_unfilled: bool
    data_kind: str = "bars"
    execution_metadata: dict = field(default_factory=dict)
    events: tuple[dict, ...] = ()

    def summary(self) -> dict[str, float | int | bool | str]:
        peak = self.config.initial_cash
        drawdown = 0.0
        for point in self.equity:
            peak = max(peak, point.equity)
            drawdown = max(drawdown, (peak - point.equity) / peak)
        final = self.equity[-1]
        return {
            "initial_cash": self.config.initial_cash,
            "final_cash": final.cash,
            "final_equity": final.equity,
            "open_quantity": final.quantity,
            "total_return": final.equity / self.config.initial_cash - 1,
            "max_drawdown": drawdown,
            "commissions_paid": math.fsum(fill.commission for fill in self.fills),
            "fill_count": len(self.fills),
            "data_kind": self.data_kind,
            "ticks_processed" if self.data_kind == "ticks" else "bars_processed": len(self.equity),
            "first_timestamp": self.equity[0].timestamp.isoformat(),
            "last_timestamp": final.timestamp.isoformat(),
            "final_signal_unfilled": self.final_signal_unfilled,
        }
