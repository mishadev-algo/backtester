"""Single-position linear-contract broker for order-driven tick strategies.

Models long/short lots, GTC limit entries, SL/TP and account-currency PnL.
Contract specification is explicit; it is not inferred from an instrument name.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .datasets import export_timezone
from .models import BacktestConfig, BacktestResult, EquityPoint, Fill, History, Tick, finite


class BrokerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    symbol: str = Field(min_length=1)
    account_currency: str = Field(min_length=1)
    server_timezone: str
    point: float = Field(gt=0)
    tick_size: float = Field(gt=0)
    tick_value_per_lot: float = Field(gt=0)
    digits: int = Field(ge=0, le=12, strict=True)
    volume_min: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    volume_step: float = Field(gt=0)
    stops_level_points: float = Field(default=0, ge=0)
    freeze_level_points: float = Field(default=0, ge=0)
    margin_per_lot: float | None = Field(default=None, ge=0)
    commission_per_lot: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_spec(self):
        export_timezone(self.server_timezone)
        if self.volume_min > self.volume_max:
            raise ValueError("volume_min must not exceed volume_max")
        return self

    @property
    def value_per_price_unit(self):
        return self.tick_value_per_lot / self.tick_size


@dataclass(frozen=True, slots=True)
class Order:
    side: Literal["buy", "sell"]
    volume: float
    stop_loss: float
    take_profit: float = 0.0
    limit_price: float | None = None
    setup_bar: int = 0
    reason: str = "entry"


@dataclass(frozen=True, slots=True)
class PendingOrder:
    order_id: int
    request: Order
    timestamp: datetime
    sequence: int


@dataclass(frozen=True, slots=True)
class Position:
    position_id: int
    order_id: int
    side: str
    volume: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime


class OrderBroker:
    def __init__(self, config: BacktestConfig, spec: BrokerSpec):
        if config.spread_bps:
            raise ValueError("order ticks already contain bid/ask spread; spread_bps must be zero")
        self.config = replace(config, price_type="bid_ask")
        self.spec = spec
        self.zone = export_timezone(spec.server_timezone)
        self.balance = config.initial_cash
        self.position: Position | None = None
        self.pending: PendingOrder | None = None
        self.tick: Tick | None = None
        self.sequence = 0
        self.warmup = False
        self.fills: list[Fill] = []
        self.events: list[dict] = []
        self.opened_by_day: dict[str, int] = {}
        self._next_order = 1
        self._next_position = 1

    def event(self, kind: str, **details):
        if not self.warmup:
            server_time = self.tick.timestamp.astimezone(self.zone)
            self.events.append({"event": kind, "timestamp": self.tick.timestamp,
                                "server_time": server_time, "server_day": server_time.date().isoformat(),
                                "tick_sequence": self.sequence, **details})

    def opened_today(self):
        return self.opened_by_day.get(self.tick.timestamp.astimezone(self.zone).date().isoformat(), 0)

    def profit(self, side, volume, entry, exit):
        return (1 if side == "buy" else -1) * (exit - entry) * volume * self.spec.value_per_price_unit

    def equity(self):
        if self.position is None:
            return self.balance
        p = self.position
        exit = self.tick.bid if p.side == "buy" else self.tick.ask
        return self.balance + self.profit(p.side, p.volume, p.entry_price, exit)

    def _commission(self, price, volume):
        return (price * volume * self.spec.value_per_price_unit * self.config.commission_bps / 10_000
                + volume * self.spec.commission_per_lot)

    def _quote_price(self, side):
        reference = self.tick.ask if side == "buy" else self.tick.bid
        return reference, reference * (1 + self.config.slippage_bps / 10_000 if side == "buy"
                                        else 1 - self.config.slippage_bps / 10_000)

    def cancel_pending(self, reason):
        if self.pending is not None:
            self.event("order_cancelled", order_id=self.pending.order_id, reason=reason)
            self.pending = None

    def submit(self, order: Order) -> bool:
        if self.warmup:
            return False
        for name in ("volume", "stop_loss", "take_profit"):
            finite(getattr(order, name), name)
        if order.limit_price is not None:
            finite(order.limit_price, "limit_price")
        if order.side not in ("buy", "sell") or order.volume <= 0 or order.stop_loss <= 0 or order.take_profit < 0:
            raise ValueError("invalid order side, volume or protection")
        if self.position is not None or self.pending is not None:
            self.event("order_rejected", reason="position_or_order_exists")
            return False
        s = self.spec
        if not s.volume_min <= order.volume <= s.volume_max or not math.isclose(
            order.volume / s.volume_step, round(order.volume / s.volume_step), abs_tol=1e-8
        ):
            self.event("order_rejected", reason="invalid_lot_size")
            return False
        reference = order.limit_price if order.limit_price is not None else (self.tick.ask if order.side == "buy" else self.tick.bid)
        if (order.side == "buy" and (order.stop_loss >= reference or (order.take_profit and order.take_profit <= reference))) or (
            order.side == "sell" and (order.stop_loss <= reference or (order.take_profit and order.take_profit >= reference))
        ):
            self.event("order_rejected", reason="invalid_stop_geometry")
            return False
        if order.limit_price is not None:
            distance = max(s.stops_level_points, s.freeze_level_points) * s.point
            placeable = ((reference < self.tick.ask and self.tick.ask - reference >= distance) if order.side == "buy"
                         else (reference > self.tick.bid and reference - self.tick.bid >= distance))
            if not placeable:
                self.event("order_rejected", reason="limit_not_placeable")
                return False
        pending = PendingOrder(self._next_order, order, self.tick.timestamp, self.sequence)
        self._next_order += 1
        if order.limit_price is None:
            return self._open(pending)
        self.pending = pending
        self.event("order_placed", order_id=pending.order_id, side=order.side, volume=order.volume,
                   entry=order.limit_price, stop_loss=order.stop_loss, take_profit=order.take_profit)
        return True

    def _open(self, pending):
        order = pending.request
        reference, price = self._quote_price(order.side)
        if order.limit_price is not None:
            price = min(price, order.limit_price) if order.side == "buy" else max(price, order.limit_price)
        commission = self._commission(price, order.volume)
        margin = self.spec.margin_per_lot
        if margin is not None and self.equity() - commission < margin * order.volume:
            self.event("order_rejected", order_id=pending.order_id, reason="insufficient_margin")
            return False
        self.balance -= commission
        position = Position(self._next_position, pending.order_id, order.side, order.volume, price,
                            order.stop_loss, order.take_profit, self.tick.timestamp)
        self._next_position += 1
        self.position = position
        day = self.tick.timestamp.astimezone(self.zone).date().isoformat()
        self.opened_by_day[day] = self.opened_by_day.get(day, 0) + 1
        self.fills.append(Fill(self.tick.timestamp, pending.timestamp, order.side, order.volume, reference, price,
                               commission, self.sequence, pending.sequence, order.reason, pending.order_id,
                               position.position_id, "lots"))
        self.event("position_opened", position_id=position.position_id, order_id=pending.order_id,
                   side=order.side, volume=order.volume, price=price, stop_loss=order.stop_loss, take_profit=order.take_profit)
        return True

    def close_position(self, reason, *, take_profit=False):
        if self.position is None:
            return True
        p = self.position
        side = "sell" if p.side == "buy" else "buy"
        reference, price = self._quote_price(side)
        if take_profit:
            price = max(price, p.take_profit) if side == "sell" else min(price, p.take_profit)
        gross = self.profit(p.side, p.volume, p.entry_price, price)
        commission = self._commission(price, p.volume)
        self.balance += gross - commission
        self.fills.append(Fill(self.tick.timestamp, self.tick.timestamp, side, p.volume, reference, price,
                               commission, self.sequence, self.sequence, reason, p.order_id, p.position_id, "lots"))
        self.event("position_closed", position_id=p.position_id, reason=reason, price=price,
                   gross_profit=gross, exit_commission=commission)
        self.position = None
        return True

    def _protection(self):
        p = self.position
        if p is None:
            return
        quote = self.tick.bid if p.side == "buy" else self.tick.ask
        stopped = quote <= p.stop_loss if p.side == "buy" else quote >= p.stop_loss
        reached_tp = bool(p.take_profit) and (quote >= p.take_profit if p.side == "buy" else quote <= p.take_profit)
        if stopped:
            self.close_position("stop_loss")
        elif reached_tp:
            self.close_position("take_profit", take_profit=True)

    def advance(self, tick, sequence, *, warmup=False):
        self.tick, self.sequence, self.warmup = tick, sequence, warmup
        if warmup:
            return
        # Server-side protection/pending fills precede the EA's OnTick callback.
        self._protection()
        if self.position is None and self.pending is not None:
            p = self.pending
            triggered = self.tick.ask <= p.request.limit_price if p.request.side == "buy" else self.tick.bid >= p.request.limit_price
            if triggered:
                self.pending = None
                self._open(p)
                self._protection()


def run_order_backtest(ticks: Iterable[Tick], strategy, config: BacktestConfig,
                       *, warmup_ticks: Iterable[Tick] = ()) -> BacktestResult:
    spec = strategy.broker_spec
    if not isinstance(spec, BrokerSpec):
        raise ValueError("order strategy must provide an explicit BrokerSpec")
    broker = OrderBroker(config, spec)
    observed = []
    points = []

    def process(tick, warmup):
        if not isinstance(tick, Tick):
            raise ValueError("tick input must contain Tick instances")
        if observed:
            previous = observed[-1]
            if tick.timestamp < previous.timestamp or (tick.sequence is not None and previous.sequence is not None
                                                       and tick.sequence <= previous.sequence):
                raise ValueError("ticks must preserve nondecreasing time and increasing source sequence")
        sequence = tick.sequence if tick.sequence is not None else len(observed)
        broker.advance(tick, sequence, warmup=warmup)
        observed.append(tick)
        strategy.on_tick(History(observed), broker)
        if not warmup:
            equity = broker.equity()
            finite(equity, "account equity")
            quantity = 0 if broker.position is None else broker.position.volume * (1 if broker.position.side == "buy" else -1)
            points.append(EquityPoint(tick.timestamp, broker.balance, quantity, equity, sequence))

    for tick in warmup_ticks:
        process(tick, True)
    for tick in ticks:
        process(tick, False)
    if not points:
        raise ValueError("at least one tick is required")
    metadata = {"model": "order-driven bid/ask ticks; server protection before OnTick; market requests use current quote",
                "broker": spec.model_dump(mode="json"), "quantity_unit": "lots", "cash_semantics": "realized account balance",
                "profit_model": "linear constant account-currency tick value; no swaps or currency conversion",
                "time_basis": {"timestamps": "UTC instants", "server_timezone": spec.server_timezone,
                               "server_time": "offset-aware local time; daily limits and Friday cutoff use broker server time"},
                "margin_checked": spec.margin_per_lot is not None,
                "strategy": strategy.execution_metadata() if callable(getattr(strategy, "execution_metadata", None)) else {}}
    unfilled = broker.pending is not None or bool(getattr(strategy, "has_deferred_entry", False))
    return BacktestResult(broker.config, tuple(points), tuple(broker.fills), unfilled, "ticks", metadata, tuple(broker.events))
