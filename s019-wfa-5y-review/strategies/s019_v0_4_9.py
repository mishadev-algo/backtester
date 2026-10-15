"""Core Python port of S-019_v0.4.9.mq5, source SHA256 recorded below.

Requires the order-driven backtester and an explicit linear contract spec.
External NewsManager/CommonFridayClose sources were not supplied. News must
be explicitly disabled; Friday handling follows the main file's stated rule.
See docs/S019_PORT.md for the correspondence and remaining parity limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import math
import random

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backtester.datasets import export_timezone
from backtester.order_engine import BrokerSpec, Order

MQL_SOURCE_SHA256 = "3091340e659e3c0165d045a4a76866141f98a3bf76687f1e6db09a1f848cbfd7"


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    InpWorkingTimeframe: str | int = "D1"
    InpADRPeriods: int = Field(default=3, ge=1, strict=True)
    InpLongADRMultiplier: float = Field(default=0.55, gt=0)
    InpShortADRMultiplier: float = Field(default=0.75, gt=0)
    InpAllowLong: bool = True
    InpAllowShort: bool = True
    InpAllowCounterTrend: bool = False
    InpStopBufferPoints: float = Field(default=1.0, ge=0)
    InpLimitEntryFromStop: float = Field(default=0.40, gt=0, le=1)
    InpLimitLifetimeBars: int = Field(default=1, ge=1, strict=True)
    InpTakeProfitRR: float = Field(default=2.0, ge=0)
    InpShowIndicators: bool = True
    InpFastEMAPeriod: int = Field(default=20, ge=1, strict=True)
    InpSlowEMAPeriod: int = Field(default=50, ge=1, strict=True)
    InpADXPeriod: int = Field(default=14, ge=1, strict=True)
    InpATRPeriod: int = Field(default=14, ge=1, strict=True)
    InpDrawLevels: bool = True
    InpRiskMoney: float = Field(default=100.0, gt=0)
    InpMagicNumber: int = Field(default=190400, ge=0, strict=True)
    InpDeviationPoints: int = Field(default=20, ge=0, strict=True)
    InpPrintLog: bool = True
    InpDailyPositionLimit: int = Field(default=1, ge=0, strict=True)
    InpUseFridayClose: bool = True
    InpFridayCloseHour: int = Field(default=21, ge=0, le=23, strict=True)
    InpFridayCloseMinute: int = Field(default=0, ge=0, le=59, strict=True)
    InpUseEntryDelay: bool = True
    InpEntryDelayMinSeconds: int = Field(default=10, ge=0, strict=True)
    InpEntryDelayMaxSeconds: int = Field(default=60, ge=0, strict=True)
    InpEntryDelaySeed: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def check_inputs(self):
        if self.InpSlowEMAPeriod <= self.InpFastEMAPeriod:
            raise ValueError("InpSlowEMAPeriod must exceed InpFastEMAPeriod")
        if self.InpEntryDelayMaxSeconds < self.InpEntryDelayMinSeconds:
            raise ValueError("entry delay maximum must not be smaller than minimum")
        return self


def timeframe_seconds(value, chart_timeframe=None):
    if value in (0, "PERIOD_CURRENT", "CURRENT"):
        if chart_timeframe is None:
            raise ValueError("PERIOD_CURRENT requires explicit chart_timeframe")
        value = chart_timeframe
    if type(value) is int:
        if value in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30):
            value = f"M{value}"
        elif value - 16384 in (1, 2, 3, 4, 6, 8, 12):
            value = f"H{value - 16384}"
        elif value == 16408:
            value = "D1"
    text = str(value).upper().removeprefix("PERIOD_")
    if text not in {"M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
                    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1"}:
        raise ValueError("port supports fixed intraday timeframes and D1; W1/MN1 are not implemented")
    return {"M": 60, "H": 3600, "D": 86400}[text[0]] * int(text[1:])


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Levels:
    reference_bar: datetime
    adr: float
    reference_close: float
    long_trigger: float
    short_trigger: float
    long_stop: float
    short_stop: float


@dataclass(frozen=True)
class DeferredEntry:
    side: str
    release: datetime
    signal_close: float
    raw_stop: float


class S019:
    execution_mode = "orders"

    def __init__(self, broker: dict, *, news_mode="unconfigured", chart_timeframe=None, **inputs):
        self.inputs = Inputs(**inputs)
        self.broker_spec = BrokerSpec.model_validate(broker)
        if news_mode != "disabled":
            raise ValueError("NewsManager.mqh was not supplied: provide its source for a full port, or explicitly set news_mode='disabled' for the documented no-news variant")
        self.news_mode = news_mode
        self.zone = export_timezone(self.broker_spec.server_timezone)
        self.seconds = timeframe_seconds(self.inputs.InpWorkingTimeframe, chart_timeframe)
        seed = self.inputs.InpEntryDelaySeed
        self.resolved_seed = seed if seed > 0 else random.SystemRandom().randrange(1, 2**63)
        self.rng = random.Random(self.resolved_seed)
        self.current: Candle | None = None
        self.closed: list[Candle] = []
        self.bar_index = -1
        self.slow_ema: float | None = None
        self.levels: Levels | None = None
        self.deferred: DeferredEntry | None = None

    @property
    def has_deferred_entry(self):
        return self.deferred is not None

    def execution_metadata(self):
        return {
            "name": "S-019", "version": "0.4.9", "mql_source_sha256": MQL_SOURCE_SHA256,
            "inputs": self.inputs.model_dump(), "news_mode": self.news_mode,
            "resolved_entry_delay_seed": self.resolved_seed,
            "closed_bars_observed": len(self.closed), "levels_ready": self.levels is not None,
            "slow_ema_available": len(self.closed) >= self.inputs.InpSlowEMAPeriod,
            "indicator_bar_source": "bid",
            "limitations": [
                "NewsManager.mqh missing; news entry block and TP removal/restoration explicitly disabled",
                "CommonFridayClose.mqh missing; Friday-through-weekend rule follows main-file comments, not verified helper code",
                "tick-driven delay release; no independent OnTimer events between quotes",
                "Python seeded PRNG; not claimed bit-identical to MQL MathRand",
                "Bid-derived indicator bars; EMA seeded at first observed closed bar; provide sufficient historical warmup",
                "linear constant tick-value PnL; no broker conversion, swaps, partial fills or MT5 execution parity validation",
                "chart-only indicators/objects omitted; InpDeviationPoints is not converted to deterministic slippage",
            ],
        }

    def _round(self, price, mode=ROUND_HALF_UP):
        spec = self.broker_spec
        units = (Decimal(str(price)) / Decimal(str(spec.tick_size))).to_integral_value(rounding=mode)
        return float((units * Decimal(str(spec.tick_size))).quantize(Decimal(1).scaleb(-spec.digits), rounding=ROUND_HALF_UP))

    def _bar_start(self, timestamp):
        local = timestamp.astimezone(self.zone)
        seconds = local.hour * 3600 + local.minute * 60 + local.second
        beginning = seconds // self.seconds * self.seconds
        # Construct calendar boundaries in broker local time, not by UTC-day rounding.
        return local.replace(hour=beginning // 3600, minute=beginning % 3600 // 60,
                             second=beginning % 60, microsecond=0).astimezone(timezone.utc)

    def _update_bars(self, tick):
        start = self._bar_start(tick.timestamp)
        if self.current is None or self.current.timestamp != start:
            if self.current is not None:
                self.closed.append(self.current)
                close = self.current.close
                alpha = 2 / (self.inputs.InpSlowEMAPeriod + 1)
                self.slow_ema = close if self.slow_ema is None else alpha * close + (1 - alpha) * self.slow_ema
            self.current = Candle(start, tick.bid, tick.bid, tick.bid, tick.bid)
            self.bar_index += 1
        else:
            self.current.high = max(self.current.high, tick.bid)
            self.current.low = min(self.current.low, tick.bid)
            self.current.close = tick.bid

    def calculate_levels(self):
        p = self.inputs
        # MQL requires Bars >= ADRPeriods + 2 (current + N closed + one older).
        if len(self.closed) < p.InpADRPeriods + 1:
            return False
        window = self.closed[-p.InpADRPeriods:]
        if any(bar.high <= bar.low or bar.low <= 0 for bar in window):
            return False
        adr = sum(bar.high - bar.low for bar in window) / p.InpADRPeriods
        previous_close = self.closed[-1].close
        levels = Levels(self.current.timestamp, adr, previous_close,
                        self._round(previous_close + p.InpLongADRMultiplier * adr),
                        self._round(previous_close - p.InpShortADRMultiplier * adr),
                        self._round(min(bar.low for bar in window) - p.InpStopBufferPoints * self.broker_spec.point),
                        self._round(max(bar.high for bar in window) + p.InpStopBufferPoints * self.broker_spec.point))
        if levels.long_trigger <= levels.short_trigger or adr <= 0:
            return False
        self.levels = levels
        return True

    def _friday_window(self, now):
        p = self.inputs
        local = now.astimezone(self.zone)
        return p.InpUseFridayClose and (local.weekday() > 4 or (local.weekday() == 4 and
               (local.hour, local.minute) >= (p.InpFridayCloseHour, p.InpFridayCloseMinute)))

    def _cancel_delay(self, broker, reason):
        if self.deferred is not None:
            broker.event("delay_cancelled", reason=reason)
            self.deferred = None

    def _daily_limit(self, broker):
        limit = self.inputs.InpDailyPositionLimit
        if not limit or broker.opened_today() < limit:
            return False
        self._cancel_delay(broker, "daily_position_limit")
        broker.cancel_pending("daily_position_limit")
        broker.event("entry_blocked", reason="daily_position_limit")
        return True

    def volume_by_risk(self, side, entry, stop):
        s = self.broker_spec
        if entry <= 0 or stop <= 0 or (side == "buy" and stop >= entry) or (side == "sell" and stop <= entry):
            return 0.0
        loss_per_lot = abs(entry - stop) * s.value_per_price_unit
        raw = self.inputs.InpRiskMoney / loss_per_lot
        if raw < s.volume_min:
            return 0.0
        volume = math.floor((min(s.volume_max, raw) + 1e-12) / s.volume_step) * s.volume_step
        return round(volume, 8) if volume >= s.volume_min else 0.0

    def _stop(self, side, raw_stop, reference):
        minimum = max(self.broker_spec.stops_level_points * self.broker_spec.point, self.broker_spec.tick_size)
        return (self._round(min(raw_stop, reference - minimum), ROUND_FLOOR) if side == "buy"
                else self._round(max(raw_stop, reference + minimum), ROUND_CEILING))

    def _tp(self, side, entry, stop, reference):
        if self.inputs.InpTakeProfitRR <= 0:
            return 0.0
        minimum = max(self.broker_spec.stops_level_points * self.broker_spec.point, self.broker_spec.tick_size)
        distance = abs(entry - stop) * self.inputs.InpTakeProfitRR
        raw = entry + distance if side == "buy" else entry - distance
        if raw <= 0:
            return 0.0
        return (self._round(max(raw, reference + minimum), ROUND_CEILING) if side == "buy"
                else self._round(min(raw, reference - minimum), ROUND_FLOOR))

    def _submit_entry(self, side, signal_close, raw_stop, broker):
        if broker.position is not None or self._friday_window(broker.tick.timestamp) or self._daily_limit(broker):
            return False
        p = self.inputs
        if p.InpLimitEntryFromStop >= 1:
            reference = broker.tick.bid if side == "buy" else broker.tick.ask
            stop = self._stop(side, raw_stop, reference)
            entry = broker.tick.ask if side == "buy" else broker.tick.bid
            tp = self._tp(side, entry, stop, reference)
            limit = None
        else:
            signal_distance = signal_close - raw_stop if side == "buy" else raw_stop - signal_close
            if signal_distance <= 0:
                broker.event("entry_blocked", reason="invalid_signal_stop_geometry")
                return False
            entry = self._round(raw_stop + p.InpLimitEntryFromStop * signal_distance if side == "buy"
                                else raw_stop - p.InpLimitEntryFromStop * signal_distance)
            stop = self._stop(side, raw_stop, entry)
            tp = self._tp(side, entry, stop, entry)
            limit = entry
        volume = self.volume_by_risk(side, entry, stop)
        if volume <= 0:
            broker.event("entry_blocked", reason="risk_volume_invalid")
            return False
        return broker.submit(Order(side, volume, stop, tp, limit, self.bar_index,
                                   f"S-019 {side} {'limit' if limit is not None else 'market'}"))

    def _queue_entry(self, side, signal_close, raw_stop, broker):
        if broker.position is not None or self._friday_window(broker.tick.timestamp) or self._daily_limit(broker):
            return
        if not self.inputs.InpUseEntryDelay:
            self._submit_entry(side, signal_close, raw_stop, broker)
            return
        if self.deferred is not None:
            broker.event("entry_blocked", reason="delay_already_pending")
            return
        seconds = self.rng.randint(self.inputs.InpEntryDelayMinSeconds, self.inputs.InpEntryDelayMaxSeconds)
        release = broker.tick.timestamp.replace(microsecond=0) + timedelta(seconds=seconds)
        self.deferred = DeferredEntry(side, release, signal_close, raw_stop)
        broker.event("entry_queued", side=side, delay_seconds=seconds, release=release, raw_stop=raw_stop)

    def _release_entry(self, broker):
        pending = self.deferred
        if pending is None or broker.tick.timestamp < pending.release:
            return
        if broker.position is not None or self._friday_window(broker.tick.timestamp):
            self._cancel_delay(broker, "position_exists_or_friday")
            return
        self.deferred = None
        broker.event("entry_released", side=pending.side)
        # The old signal/stop snapshot is intentionally not revalidated.
        self._submit_entry(pending.side, pending.signal_close, pending.raw_stop, broker)

    def _process_signal(self, side, signal_close, trend_allowed, broker):
        if broker.position is not None and broker.position.side != side:
            if not broker.close_position("S-019: opposite signal"):
                return
        if broker.position is not None:
            broker.event("entry_blocked", reason="same_direction_position")
            return
        if not trend_allowed:
            broker.event("entry_blocked", reason="slow_ema_filter")
            return
        if not (self.inputs.InpAllowLong if side == "buy" else self.inputs.InpAllowShort):
            broker.event("entry_blocked", reason="direction_disabled")
            return
        raw_stop = self.levels.long_stop if side == "buy" else self.levels.short_stop
        self._queue_entry(side, signal_close, raw_stop, broker)

    def on_tick(self, history, broker):
        tick = history[-1]
        # Platform price series update even when the EA returns for Friday/news.
        self._update_bars(tick)
        if broker.warmup:
            self.calculate_levels()
            return
        if self._friday_window(tick.timestamp):
            broker.close_position("S-019: Friday close")
            broker.cancel_pending("friday_cutoff")
            self._cancel_delay(broker, "friday_cutoff")
            return
        self._release_entry(broker)
        if self.levels is None:
            if self.calculate_levels():
                broker.event("levels_ready", **self.levels.__dict__)
            return
        if self.current.timestamp == self.levels.reference_bar:
            return
        if broker.pending is not None and self.bar_index - broker.pending.request.setup_bar >= self.inputs.InpLimitLifetimeBars:
            broker.cancel_pending("limit_lifetime")
        signal_close = self.closed[-1].close
        old = self.levels
        side = "buy" if signal_close > old.long_trigger else ("sell" if signal_close < old.short_trigger else None)
        if side is not None:
            self._cancel_delay(broker, "newer_confirmed_signal")
            broker.cancel_pending("newer_confirmed_signal")
        trend_allowed = True
        if side is not None and not self.inputs.InpAllowCounterTrend:
            trend_allowed = (len(self.closed) >= self.inputs.InpSlowEMAPeriod and self.slow_ema is not None
                             and (signal_close > self.slow_ema if side == "buy" else signal_close < self.slow_ema))
        # Critical source ordering: compare to OLD triggers, then recalculate stops.
        if not self.calculate_levels():
            broker.event("waiting", reason="adr_data_unavailable")
            return
        if side is not None:
            broker.event("signal", side=side, signal_close=signal_close, long_trigger_used=old.long_trigger,
                         short_trigger_used=old.short_trigger, slow_ema=self.slow_ema,
                         long_stop_reference=self.levels.long_stop, short_stop_reference=self.levels.short_stop)
            self._process_signal(side, signal_close, trend_allowed, broker)


def create_strategy(broker, news_mode="unconfigured", chart_timeframe=None, **inputs):
    return S019(broker, news_mode=news_mode, chart_timeframe=chart_timeframe, **inputs)
