from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from backtester import BacktestConfig, Tick, run_tick_backtest
from backtester.s019_search import EquityState, QuoteTape, run_search_trial
from backtester.order_engine import OrderBroker
from backtester.strategy_loader import load_strategy

STRATEGY = Path(__file__).resolve().parents[1] / "strategies/s019_v0_4_9.py"


class S019SearchTests(unittest.TestCase):
    def test_event_search_matches_reference_on_both_sides_delays_expiry_and_warmup(self):
        import random
        rng = random.Random(17)
        ticks = []
        start = datetime(2026, 1, 9, 16, tzinfo=timezone.utc)  # Friday, includes cutoff/weekend.
        price = 1000.
        for minute in range(0, 60 * 80):
            for second in (0, 11, 37):
                price = max(500, price + rng.choice([-8, -3, 1, 4, 9]))
                ticks.append(Tick(start + timedelta(minutes=minute, seconds=second), price, price + 2,
                                  sequence=len(ticks)))
        tape = QuoteTape.from_ticks(ticks)
        settings = dict(broker=dict(symbol="TEST", account_currency="USD", server_timezone="Europe/Kyiv",
                                    point=1, tick_size=1, tick_value_per_lot=1, digits=0,
                                    volume_min=1, volume_max=100, volume_step=1),
                        news_mode="disabled", InpWorkingTimeframe=20, InpADRPeriods=3,
                        InpFastEMAPeriod=2, InpSlowEMAPeriod=5, InpEntryDelaySeed=99,
                        InpDailyPositionLimit=3, InpLimitLifetimeBars=3)
        config = BacktestConfig(initial_cash=100000)
        for use_delay in (False, True):
            for fraction in (.5, 1.):
                with self.subTest(delay=use_delay, fraction=fraction), load_strategy(str(STRATEGY), "ticks") as loaded:
                    params = {**settings, "InpUseEntryDelay": use_delay, "InpLimitEntryFromStop": fraction}
                    reference = run_tick_backtest(ticks[300:], loaded.factory(**params), config,
                                                  warmup_ticks=ticks[:300])
                    metric, broker = run_search_trial(tape, loaded.factory(**params), config, 300, len(ticks))
                    self.assertEqual(broker.fills, list(reference.fills))
                    self.assertEqual(metric["final_equity_usd"], reference.equity[-1].equity)
                    self.assertAlmostEqual(metric["max_drawdown"], reference.summary()["max_drawdown"], places=13)
                    self.assertLess(metric["callbacks"], len(ticks) / 5)
                    self.assertTrue(any(fill.side == "buy" for fill in reference.fills))
                    self.assertTrue(any(fill.side == "sell" for fill in reference.fills))

    def test_search_rejects_non_m20_strategy(self):
        ticks = [Tick(datetime(2026, 1, 1, tzinfo=timezone.utc), 100, 101, sequence=0)]
        class Unsupported:
            seconds = 60
        with self.assertRaisesRegex(ValueError, "S019 M20"):
            run_search_trial(QuoteTape.from_ticks(ticks), Unsupported(), BacktestConfig(), 0, 1)

    def test_walk_forward_reuses_cash_daily_counters_and_full_resolution_drawdown(self):
        import random
        rng = random.Random(221)
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        ticks = []
        price = 1000.
        for i in range(2400):
            price += rng.choice([-8, -3, 1, 4, 9])
            ticks.append(Tick(start + timedelta(seconds=20*i), price, price+2, sequence=i))
        settings = dict(broker=dict(symbol="TEST", account_currency="USD", server_timezone="Europe/Kyiv",
                                    point=1, tick_size=1, tick_value_per_lot=1, digits=0,
                                    volume_min=1, volume_max=100, volume_step=1),
                        news_mode="disabled", InpWorkingTimeframe=20, InpADRPeriods=3,
                        InpFastEMAPeriod=2, InpSlowEMAPeriod=5, InpEntryDelaySeed=99,
                        InpDailyPositionLimit=3, InpLimitLifetimeBars=3)
        config = BacktestConfig(initial_cash=100000)
        split = 1200
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            old = loaded.factory(**settings)
            reference = OrderBroker(config, old.broker_spec)
            equity = []
            for i in range(split):
                reference.advance(ticks[i], i, warmup=i < 100)
                old.on_tick([ticks[i]], reference)
                if i >= 100:
                    equity.append(reference.equity())
            reference.advance(ticks[split], split)
            reference.close_position("wfa_roll")
            reference.cancel_pending("wfa_roll")
            new_params = {**settings, "InpTakeProfitRR": 3, "InpSlowEMAPeriod": 8}
            new = loaded.factory(**new_params)
            for i in range(split):
                reference.advance(ticks[i], i, warmup=True)
                new.on_tick([ticks[i]], reference)
            for i in range(split, len(ticks)):
                reference.advance(ticks[i], i)
                new.on_tick([ticks[i]], reference)
                equity.append(reference.equity())

            tape = QuoteTape.from_ticks(ticks)
            state = EquityState(config.initial_cash)
            observed = []
            sink = lambda indices, values: observed.extend(zip(map(int, indices), map(float, values)))
            _, broker = run_search_trial(tape, loaded.factory(**settings), config, 100, split,
                                         equity_state=state, equity_observer=sink)
            broker.advance(ticks[split], split)
            broker.close_position("wfa_roll")
            broker.cancel_pending("wfa_roll")
            metric, broker = run_search_trial(tape, loaded.factory(**new_params), config, split, len(ticks),
                                              broker=broker, equity_state=state, equity_observer=sink)
            self.assertEqual(broker.fills, reference.fills)
            self.assertEqual(broker.opened_by_day, reference.opened_by_day)
            self.assertEqual(metric["final_equity_usd"], reference.equity())
            peak = 100000.
            max_relative = max_absolute = 0.
            for value in equity:
                peak = max(peak, value)
                max_relative = max(max_relative, (peak-value)/peak)
                max_absolute = max(max_absolute, peak-value)
            self.assertAlmostEqual(state.max_drawdown, max_relative, places=13)
            self.assertAlmostEqual(state.max_drawdown_usd, max_absolute, places=9)
            self.assertEqual([i for i, value in observed], sorted(i for i, value in observed))
            for i, value in observed:
                self.assertEqual(value, equity[i-100])


if __name__ == "__main__":
    unittest.main()
