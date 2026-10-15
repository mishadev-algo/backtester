from __future__ import annotations

from dataclasses import dataclass, field
import math

from .models import History, Tick


@dataclass
class MovingAverageCross:
    fast: int = 10
    slow: int = 30
    _last_target: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if type(self.fast) is not int or type(self.slow) is not int or not 0 < self.fast < self.slow:
            raise ValueError("moving-average windows must be integers with 0 < fast < slow")

    def on_bar(self, history: History) -> float | None:
        if len(history) < self.slow:
            return None
        fast = math.fsum(bar.close for bar in history[-self.fast:]) / self.fast
        slow = math.fsum(bar.close for bar in history[-self.slow:]) / self.slow
        target = 1.0 if fast > slow else 0.0
        if target == self._last_target:
            return None
        self._last_target = target
        return target


def create_strategy(**parameters) -> MovingAverageCross:
    return MovingAverageCross(**parameters)


@dataclass
class TickMovingAverageCross:
    fast: int = 10
    slow: int = 30
    _last_target: float | None = field(default=None, init=False)

    def __post_init__(self):
        if type(self.fast) is not int or type(self.slow) is not int or not 0 < self.fast < self.slow:
            raise ValueError("moving-average windows must be integers with 0 < fast < slow")

    def on_tick(self, history: History[Tick]) -> float | None:
        if len(history) < self.slow:
            return None
        def mean(window):
            return math.fsum((tick.bid + tick.ask) / 2 for tick in history[-window:]) / window
        target = 1.0 if mean(self.fast) > mean(self.slow) else 0.0
        if target == self._last_target:
            return None
        self._last_target = target
        return target


def create_tick_strategy(**parameters) -> TickMovingAverageCross:
    return TickMovingAverageCross(**parameters)
