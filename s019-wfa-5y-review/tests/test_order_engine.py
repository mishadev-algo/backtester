from datetime import datetime, timedelta, timezone
import unittest

from backtester import BacktestConfig, Tick, run_tick_backtest
from backtester.order_engine import BrokerSpec, Order, OrderBroker


def spec(**changes):
    return BrokerSpec(symbol="TEST", account_currency="USD", server_timezone="UTC", point=1,
                      tick_size=1, tick_value_per_lot=2, digits=0, volume_min=1, volume_max=100, volume_step=1,
                      **changes)


def quote(index, bid, ask):
    return Tick(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index), bid, ask, sequence=index)


class OrderBrokerTests(unittest.TestCase):
    def broker(self, **config):
        broker = OrderBroker(BacktestConfig(initial_cash=1000, **config), spec())
        broker.advance(quote(0, 100, 101), 0)
        return broker

    def test_long_lot_profit_marks_bid_and_does_not_pay_notional(self):
        broker = self.broker()
        self.assertTrue(broker.submit(Order("buy", 2, 90, 120)))
        self.assertEqual(broker.balance, 1000)
        self.assertEqual(broker.position.entry_price, 101)
        self.assertEqual(broker.equity(), 996)  # 2 lots * $2/point * one point spread.
        broker.advance(quote(1, 110, 111), 1)
        broker.close_position("manual")
        self.assertEqual(broker.balance, 1036)
        self.assertEqual(broker.fills[-1].quantity_unit, "lots")

    def test_short_profit_marks_ask_and_stop_closes_at_gapped_ask(self):
        broker = self.broker()
        self.assertTrue(broker.submit(Order("sell", 2, 110, 80)))
        self.assertEqual(broker.equity(), 996)
        broker.advance(quote(1, 112, 113), 1)
        self.assertIsNone(broker.position)
        self.assertEqual(broker.balance, 948)  # (100 - 113) * 2 lots * $2.
        self.assertEqual(broker.fills[-1].reason, "stop_loss")

    def test_limit_waits_for_correct_quote_side_and_can_improve_price(self):
        broker = self.broker()
        self.assertTrue(broker.submit(Order("buy", 1, 80, 120, limit_price=95)))
        broker.advance(quote(1, 94, 96), 1)  # Bid crossed, Ask did not.
        self.assertIsNone(broker.position)
        broker.advance(quote(2, 93, 94), 2)
        self.assertEqual(broker.position.entry_price, 94)
        self.assertEqual(broker.fills[0].signal_tick_sequence, 0)
        self.assertEqual(broker.fills[0].tick_sequence, 2)

    def test_sell_limit_uses_bid_and_tp_uses_ask(self):
        broker = self.broker()
        broker.submit(Order("sell", 1, 130, 90, limit_price=110))
        broker.advance(quote(1, 109, 111), 1)
        self.assertIsNone(broker.position)
        broker.advance(quote(2, 112, 113), 2)
        self.assertEqual(broker.position.entry_price, 112)
        broker.advance(quote(3, 89, 91), 3)
        self.assertIsNotNone(broker.position)
        broker.advance(quote(4, 88, 89), 4)
        self.assertIsNone(broker.position)
        self.assertEqual(broker.fills[-1].reason, "take_profit")
        self.assertEqual(broker.balance, 1046)

    def test_limit_fill_and_gapped_stop_can_happen_on_same_quote(self):
        broker = self.broker()
        broker.submit(Order("buy", 1, 90, 120, limit_price=95))
        broker.advance(quote(1, 85, 94), 1)
        self.assertIsNone(broker.position)
        self.assertEqual([fill.price for fill in broker.fills], [94, 85])
        self.assertEqual(broker.balance, 982)

    def test_margin_and_lot_rules_reject_without_partial_fills(self):
        broker = OrderBroker(BacktestConfig(initial_cash=1000), spec(margin_per_lot=600))
        broker.advance(quote(0, 100, 101), 0)
        self.assertFalse(broker.submit(Order("buy", 2, 90, 120)))
        self.assertEqual(broker.balance, 1000)
        self.assertEqual(broker.fills, [])
        self.assertFalse(broker.submit(Order("buy", 1.5, 90, 120)))

    def test_commission_and_slippage_reconcile_realized_balance(self):
        broker = self.broker(commission_bps=10, slippage_bps=10)
        broker.submit(Order("buy", 2, 80, 130))
        broker.advance(quote(1, 110, 111), 1)
        broker.close_position("manual")
        opening, closing = broker.fills
        gross = (closing.price - opening.price) * 2 * 2
        self.assertAlmostEqual(broker.balance, 1000 + gross - opening.commission - closing.commission)

    def test_daily_count_tracks_actual_fills_in_server_timezone(self):
        broker = OrderBroker(BacktestConfig(), spec())
        broker.advance(quote(0, 100, 101), 0)
        broker.submit(Order("buy", 1, 90, 120, limit_price=95))
        self.assertEqual(broker.opened_today(), 0)
        broker.advance(quote(1, 94, 95), 1)
        self.assertEqual(broker.opened_today(), 1)
        broker.close_position("manual")
        tomorrow = Tick(datetime(2026, 1, 2, tzinfo=timezone.utc), 100, 101, sequence=2)
        broker.advance(tomorrow, 2)
        self.assertEqual(broker.opened_today(), 0)

    def test_server_protection_precedes_strategy_callback(self):
        class Strategy:
            execution_mode = "orders"
            broker_spec = spec()
            def on_tick(self, history, broker):
                if len(history) == 1:
                    broker.submit(Order("buy", 1, 90, 120))
                else:
                    self_case.assertIsNone(broker.position)
        self_case = self
        result = run_tick_backtest([quote(0, 100, 101), quote(1, 89, 90)], Strategy())
        self.assertEqual(result.fills[-1].reason, "stop_loss")
        self.assertEqual(result.execution_metadata["quantity_unit"], "lots")
