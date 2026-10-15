from datetime import datetime, timedelta, timezone
import math
import unittest

from backtester import BacktestConfig, Bar, run_backtest
from backtester.reporting import daily_pnl


def bar(index, opening=100, close=None):
    close = opening if close is None else close
    return Bar(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
               opening, max(opening, close), min(opening, close), close)


class Signals:
    def __init__(self, *values):
        self.values = iter(values)

    def on_bar(self, history):
        return next(self.values, None)


class EngineTests(unittest.TestCase):
    def test_orders_use_next_open_and_do_not_capture_signal_bar_profit(self):
        result = run_backtest([bar(0, 100, 200), bar(1, 110, 120), bar(2, 130)],
                              Signals(1, 0), BacktestConfig(initial_cash=1000))
        self.assertEqual(result.equity[0].equity, 1000)
        self.assertEqual(len(result.fills), 2)
        self.assertEqual(result.fills[0].price, 110)
        self.assertEqual(result.fills[0].signal_timestamp, bar(0).timestamp)
        self.assertGreater(result.fills[0].timestamp, result.fills[0].signal_timestamp)
        self.assertAlmostEqual(result.equity[-1].equity, 1000 / 110 * 130)
        self.assertEqual(result.equity[-1].quantity, 0)

    def test_full_investment_reserves_cash_for_spread_slippage_and_fees(self):
        config = BacktestConfig(initial_cash=1000, commission_bps=100, spread_bps=100, slippage_bps=50)
        result = run_backtest([bar(0), bar(1), bar(2)], Signals(1, 0), config)
        buy, sell = result.fills
        self.assertAlmostEqual(buy.price, 101)
        self.assertAlmostEqual(sell.price, 99)
        expected_units = 1000 / (101 * 1.01)
        self.assertAlmostEqual(buy.quantity, expected_units)
        self.assertAlmostEqual(buy.commission, expected_units * 101 * 0.01)
        self.assertAlmostEqual(sell.commission, expected_units * 99 * 0.01)
        self.assertAlmostEqual(result.equity[-1].cash, expected_units * 99 * 0.99)
        self.assertTrue(all(point.cash >= 0 and point.quantity >= 0 for point in result.equity))
        self.assertLess(result.summary()['total_return'], 0)

    def test_none_holds_without_rebalancing_and_final_equity_marks_open_position(self):
        result = run_backtest([bar(0), bar(1), bar(2, 100, 120)], Signals(0.5),
                              BacktestConfig(initial_cash=1000))
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.equity[-1].cash, 500)
        self.assertEqual(result.equity[-1].quantity, 5)
        self.assertEqual(result.equity[-1].equity, 1100)
        self.assertAlmostEqual(result.summary()['total_return'], 0.1)

    def test_rebalancing_sizes_against_equity_at_gapped_open(self):
        result = run_backtest([bar(0), bar(1), bar(2, 200)], Signals(0.5, 0.5),
                              BacktestConfig(initial_cash=1000))
        self.assertEqual(result.fills[-1].side, 'sell')
        self.assertEqual(result.fills[-1].quantity, 1.25)
        self.assertEqual(result.equity[-1].cash, 750)
        self.assertEqual(result.equity[-1].quantity, 3.75)
        self.assertEqual(result.equity[-1].equity, 1500)

    def test_last_bar_signal_is_not_filled_at_its_close(self):
        result = run_backtest([bar(0)], Signals(1))
        self.assertEqual(result.fills, ())
        self.assertTrue(result.final_signal_unfilled)
        self.assertEqual(result.summary()['total_return'], 0)

    def test_drawdown_includes_initial_capital_and_daily_pnl_reconciles(self):
        result = run_backtest([bar(0), bar(1, 100, 120), bar(2, 120, 90)], Signals(1),
                              BacktestConfig(initial_cash=1000))
        self.assertAlmostEqual(result.summary()['max_drawdown'], 0.25)
        self.assertAlmostEqual(math.fsum(value for _, value in daily_pnl(result)), -100)
        losing = run_backtest([bar(0), bar(1, 100, 90)], Signals(1), BacktestConfig(initial_cash=1000))
        self.assertAlmostEqual(losing.summary()['max_drawdown'], 0.1)

    def test_history_has_only_observed_bars_and_retained_views_do_not_grow(self):
        views = []
        class InspectHistory:
            def on_bar(self, history):
                views.append(history)
                with self_case.assertRaises(IndexError):
                    history[len(history)]
                self_case.assertEqual(history[-1], bar(len(history) - 1))
                return None
        self_case = self
        run_backtest([bar(0), bar(1), bar(2)], InspectHistory())
        self.assertEqual([len(view) for view in views], [1, 2, 3])
        self.assertEqual(views[0][:], (bar(0),))
        self.assertEqual(views[2][::-1], (bar(2), bar(1), bar(0)))
        with self.assertRaises(TypeError):
            views[0][0] = bar(1)

    def test_rejects_invalid_targets_before_they_can_trade(self):
        for target in (-1, 1.1, float('nan'), float('inf'), True, 'buy'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                run_backtest([bar(0)], Signals(target))

    def test_rejects_empty_duplicate_and_unsorted_bars(self):
        for bars in ([], [bar(0), bar(0)], [bar(1), bar(0)]):
            with self.subTest(bars=bars), self.assertRaises(ValueError):
                run_backtest(bars, Signals())

    def test_rejects_invalid_bar_and_cost_inputs(self):
        for values in ({'initial_cash': 0}, {'commission_bps': -1}, {'spread_bps': float('nan')},
                       {'slippage_bps': 10_000}, {'commission_bps': 10_000}, {'initial_cash': True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BacktestConfig(**values)
        with self.assertRaises(ValueError):
            Bar(datetime(2026, 1, 1), 100, 100, 100, 100)
        for prices in ((100, 90, 80, 100), (0, 100, 0, 100), (100, float('inf'), 100, 100)):
            with self.subTest(prices=prices), self.assertRaises(ValueError):
                Bar(bar(0).timestamp, *prices)
