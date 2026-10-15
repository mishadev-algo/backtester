from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from backtester import BacktestConfig, Tick, run_tick_backtest
from backtester.experiments import RunRequest, execute_experiment
from backtester.order_engine import Order, OrderBroker
from backtester.strategy_loader import load_strategy
from backtester.tick_data import TickCsvProfile, TickDatasetSpec, import_ticks

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / "strategies/s019_v0_4_9.py"
BROKER = dict(symbol="TEST_LINEAR", account_currency="USD", server_timezone="UTC", point=1, tick_size=1,
              tick_value_per_lot=1, digits=0, volume_min=1, volume_max=100, volume_step=1)


def params(**inputs):
    return dict(broker=BROKER, news_mode="disabled", InpUseEntryDelay=False, InpUseFridayClose=False,
                InpFastEMAPeriod=1, InpSlowEMAPeriod=2, **inputs)


def history_ticks(*, wick_only=False):
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday.
    rows = []
    for day in range(4):
        for second, price in enumerate((100, 110, 90, 100)):
            rows.append(Tick(start + timedelta(days=day, seconds=second), price, price + 1, sequence=len(rows)))
    for second, price in enumerate((100, 112, 100 if wick_only else 112)):
        rows.append(Tick(start + timedelta(days=4, seconds=second), price, price + 1, sequence=len(rows)))
    for second, price in enumerate((112, 97, 116)):
        rows.append(Tick(start + timedelta(days=5, seconds=second), price, price + 1, sequence=len(rows)))
    return rows


class S019Tests(unittest.TestCase):
    def test_original_inputs_and_missing_news_require_explicit_configuration(self):
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            with self.assertRaisesRegex(ValueError, "NewsManager"):
                loaded.factory(broker=BROKER)
            strategy = loaded.factory(broker=BROKER, news_mode="disabled")
            self.assertEqual(strategy.inputs.InpADRPeriods, 3)
            self.assertEqual(strategy.inputs.InpLimitEntryFromStop, 0.40)
            self.assertTrue(strategy.inputs.InpAllowShort)
            self.assertEqual(strategy.inputs.InpDailyPositionLimit, 1)
            self.assertEqual(strategy.inputs.InpEntryDelayMinSeconds, 10)
            self.assertEqual(strategy.inputs.InpEntryDelayMaxSeconds, 60)

    def test_old_triggers_new_stop_limit_risk_lots_and_tp_match_hand_calculation(self):
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(**params())
            result = run_tick_backtest(history_ticks(), strategy, BacktestConfig(initial_cash=10000))
        signals = [event for event in result.events if event["event"] == "signal"]
        self.assertEqual(signals[0]["long_trigger_used"], 111)
        self.assertEqual(signals[0]["signal_close"], 112)
        self.assertEqual(signals[0]["long_stop_reference"], 89)
        placed = [event for event in result.events if event["event"] == "order_placed"][0]
        self.assertEqual((placed["entry"], placed["stop_loss"], placed["take_profit"]), (98, 89, 116))
        self.assertEqual(placed["volume"], 11)  # floor(100 / 9), risk=99.
        self.assertEqual([fill.side for fill in result.fills], ["buy", "sell"])
        self.assertEqual([fill.price for fill in result.fills], [98, 116])
        self.assertEqual(result.equity[-1].equity, 10198)

    def test_wick_only_breakout_does_not_generate_signal(self):
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            result = run_tick_backtest(history_ticks(wick_only=True), loaded.factory(**params()))
        self.assertFalse(any(event["event"] == "signal" for event in result.events))
        self.assertEqual(result.fills, ())

    def test_new_stop_window_includes_signal_bar_without_replacing_old_trigger(self):
        ticks = history_ticks()
        ticks[17] = replace(ticks[17], bid=80, ask=81)
        ticks[20] = replace(ticks[20], bid=91, ask=92)
        ticks[21] = replace(ticks[21], bid=118, ask=119)
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            result = run_tick_backtest(ticks, loaded.factory(**params()))
        signal = next(event for event in result.events if event["event"] == "signal")
        placed = next(event for event in result.events if event["event"] == "order_placed")
        self.assertEqual(signal["long_trigger_used"], 111)
        self.assertEqual(signal["long_stop_reference"], 79)
        self.assertEqual((placed["entry"], placed["stop_loss"], placed["take_profit"], placed["volume"]), (92, 79, 118, 7))
        self.assertEqual(result.equity[-1].equity, 10182)

    def test_short_breakout_limit_entry_and_tp_preserve_short_rules(self):
        ticks = history_ticks()
        for index, bid in ((17, 84), (18, 84), (19, 84), (20, 100), (21, 77)):
            ticks[index] = replace(ticks[index], bid=bid, ask=bid + 1)
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            result = run_tick_backtest(ticks, loaded.factory(**params()))
        placed = next(event for event in result.events if event["event"] == "order_placed")
        self.assertEqual(placed["side"], "sell")
        self.assertEqual((placed["entry"], placed["stop_loss"], placed["take_profit"], placed["volume"]), (100, 111, 78, 9))
        self.assertEqual([fill.side for fill in result.fills], ["sell", "buy"])
        self.assertEqual(result.equity[-1].equity, 10198)

    def test_original_source_fingerprint_and_exposed_input_defaults(self):
        import re
        source = (ROOT / "strategies/source/S-019_v0.4.9.mq5").read_bytes()
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(broker=BROKER, news_mode="disabled")
            self.assertEqual(strategy.execution_metadata()["mql_source_sha256"], hashlib.sha256(source).hexdigest())
            for type_name, name, value in re.findall(r"^input\s+(int|double|bool|ulong|ENUM_TIMEFRAMES)\s+(Inp\w+)\s*=\s*([^;]+);", source.decode(), re.M):
                actual = getattr(strategy.inputs, name)
                expected = (value == "true" if type_name == "bool" else value.removeprefix("PERIOD_") if type_name == "ENUM_TIMEFRAMES"
                            else float(value) if type_name == "double" else int(value))
                self.assertEqual(actual, expected, name)

    def test_first_initialized_bar_does_not_retroactively_trade(self):
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            result = run_tick_backtest(history_ticks()[:17], loaded.factory(**params()))
        self.assertEqual(result.fills, ())
        self.assertFalse(any(event["event"] == "signal" for event in result.events))

    def test_volume_rounding_and_broker_protective_distances(self):
        broker_spec = {**BROKER, "tick_size": 0.5, "digits": 1, "volume_min": 0.1, "volume_step": 0.1,
                       "stops_level_points": 2, "freeze_level_points": 3}
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(broker=broker_spec, news_mode="disabled")
            self.assertEqual(strategy._round(100.25), 100.5)
            self.assertEqual(strategy._stop("buy", 99.9, 100), 98)
            self.assertEqual(strategy._stop("sell", 100.1, 100), 102)
            self.assertEqual(strategy.volume_by_risk("buy", 100, 90), 5)
            self.assertEqual(strategy.volume_by_risk("buy", 100, 101), 0)
            self.assertEqual(strategy._tp("sell", 10, 100, 10), 0)

    def test_reversal_closes_opposite_before_trend_or_direction_entry_filter(self):
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(**params(InpAllowShort=False))
            broker = OrderBroker(BacktestConfig(), strategy.broker_spec)
            tick = Tick(datetime(2026, 1, 5, tzinfo=timezone.utc), 100, 101, sequence=0)
            broker.advance(tick, 0)
            broker.submit(Order("buy", 1, 80, 130))
            strategy._process_signal("sell", 100, False, broker)
            self.assertIsNone(broker.position)
            self.assertEqual(broker.fills[-1].reason, "S-019: opposite signal")
            self.assertTrue(any(event.get("reason") == "slow_ema_filter" for event in broker.events))

    def test_daily_limit_blocks_new_leg_after_reversal_and_cancels_pending(self):
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(**params())
            broker = OrderBroker(BacktestConfig(), strategy.broker_spec)
            tick = Tick(datetime(2026, 1, 5, tzinfo=timezone.utc), 100, 101, sequence=0)
            broker.advance(tick, 0)
            broker.submit(Order("buy", 1, 80, 130))
            broker.close_position("manual")
            strategy._queue_entry("sell", 100, 120, broker)
            self.assertIsNone(broker.position)
            self.assertIsNone(broker.pending)
            self.assertTrue(any(event.get("reason") == "daily_position_limit" for event in broker.events))

    def test_three_openings_reset_at_broker_midnight_in_winter_and_summer(self):
        for first, next_midnight in (("2026-01-05T22:01:00+00:00", "2026-01-06T22:00:00+00:00"),
                                     ("2026-07-06T21:01:00+00:00", "2026-07-07T21:00:00+00:00")):
            with self.subTest(first=first), load_strategy(str(STRATEGY), "ticks") as loaded:
                settings = params(InpDailyPositionLimit=3, InpLimitEntryFromStop=1)
                settings["broker"] = {**BROKER, "server_timezone": "Europe/Kyiv"}
                strategy = loaded.factory(**settings)
                broker = OrderBroker(BacktestConfig(), strategy.broker_spec)
                start = datetime.fromisoformat(first)
                for index in range(3):
                    tick = Tick(start + timedelta(seconds=index), 100, 101, sequence=index)
                    broker.advance(tick, index)
                    strategy._queue_entry("buy", 100, 80, broker)
                    self.assertIsNotNone(broker.position)
                    broker.close_position("manual")
                self.assertEqual(broker.opened_today(), 3)
                # Crossing UTC midnight must not reset the broker's day counter.
                utc_midnight = start.replace(hour=0, minute=1) + timedelta(days=1)
                broker.advance(Tick(utc_midnight, 100, 101, sequence=3), 3)
                strategy._queue_entry("buy", 100, 80, broker)
                self.assertIsNone(broker.position)
                self.assertEqual(broker.events[-1]["reason"], "daily_position_limit")
                midnight = datetime.fromisoformat(next_midnight)
                broker.advance(Tick(midnight, 100, 101, sequence=4), 4)
                strategy._queue_entry("buy", 100, 80, broker)
                self.assertIsNotNone(broker.position)
                self.assertEqual(broker.opened_today(), 1)

    def test_friday_cutoff_uses_seasonal_broker_offset(self):
        for cutoff in ("2026-01-09T19:00:00+00:00", "2026-07-10T18:00:00+00:00"):
            with self.subTest(cutoff=cutoff), load_strategy(str(STRATEGY), "ticks") as loaded:
                settings = params()
                settings["InpUseFridayClose"] = True
                settings["broker"] = {**BROKER, "server_timezone": "Europe/Kyiv"}
                strategy = loaded.factory(**settings)
                stamp = datetime.fromisoformat(cutoff)
                self.assertFalse(strategy._friday_window(stamp - timedelta(microseconds=1)))
                self.assertTrue(strategy._friday_window(stamp))

    def test_delayed_entry_uses_captured_stop_and_second_resolution_release(self):
        settings = params()
        settings.update(InpUseEntryDelay=True, InpEntryDelayMinSeconds=10, InpEntryDelayMaxSeconds=10,
                        InpLimitEntryFromStop=1.0)
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(**settings)
            broker = OrderBroker(BacktestConfig(), strategy.broker_spec)
            now = datetime(2026, 1, 5, 12, 0, 0, 900000, tzinfo=timezone.utc)
            broker.advance(Tick(now, 100, 101, sequence=0), 0)
            strategy._queue_entry("buy", 100, 90, broker)
            self.assertEqual(strategy.deferred.release, now.replace(microsecond=0) + timedelta(seconds=10))
            broker.advance(Tick(now.replace(microsecond=0) + timedelta(seconds=9), 110, 111, sequence=1), 1)
            strategy._release_entry(broker)
            self.assertIsNone(broker.position)
            broker.advance(Tick(now.replace(microsecond=0) + timedelta(seconds=10), 110, 111, sequence=2), 2)
            strategy._release_entry(broker)
            self.assertEqual(broker.position.stop_loss, 90)
            self.assertEqual(broker.position.entry_price, 111)

    def test_expiry_counts_observed_bars_instead_of_calendar_gap(self):
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(**params())
            ticks = history_ticks()[:20]  # Signal and limit placement, before the fill quote.
            result = run_tick_backtest(ticks, strategy)
            self.assertTrue(any(event["event"] == "order_placed" for event in result.events))
            # Extend next observed bar without crossing the 98 limit or old breakout levels.
            last_day = ticks[-1].timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
            ticks.append(Tick(last_day + timedelta(days=3), 112, 113, sequence=len(ticks)))
            result = run_tick_backtest(ticks, loaded.factory(**params()))
            self.assertTrue(any(event.get("reason") == "limit_lifetime" for event in result.events))

    def test_friday_window_closes_position_and_stops_weekend_entries(self):
        settings = params()
        settings.update(InpUseFridayClose=True)
        with load_strategy(str(STRATEGY), "ticks") as loaded:
            strategy = loaded.factory(**settings)
            broker = OrderBroker(BacktestConfig(), strategy.broker_spec)
            before = Tick(datetime(2026, 1, 9, 20, 59, tzinfo=timezone.utc), 100, 101, sequence=0)
            broker.advance(before, 0)
            broker.submit(Order("buy", 1, 80, 130))
            cutoff = Tick(datetime(2026, 1, 9, 21, tzinfo=timezone.utc), 100, 101, sequence=1)
            broker.advance(cutoff, 1)
            strategy.on_tick([before, cutoff], broker)
            self.assertIsNone(broker.position)
            self.assertEqual(broker.fills[-1].reason, "S-019: Friday close")
            self.assertTrue(strategy._friday_window(cutoff.timestamp + timedelta(days=1)))
            self.assertFalse(strategy._friday_window(cutoff.timestamp + timedelta(days=3)))

    def test_full_cli_file_strategy_with_json_parameter_file(self):
        ticks = history_ticks()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "quotes.csv"
            csv_path.write_text("DateTime,Bid,Ask,Volume\n" + "".join(
                f"{tick.timestamp.isoformat()},{tick.bid},{tick.ask},0\n" for tick in ticks))
            data = root / "dataset"
            import_ticks(csv_path, data, TickDatasetSpec(symbol="TEST_LINEAR", source="synthetic-candles"),
                         TickCsvProfile(timestamp_format=None))
            parameters = root / "params.json"
            parameters.write_text(json.dumps(params()))
            output = root / "run"
            result = subprocess.run([sys.executable, "-m", "backtester", "run", "--data", str(data),
                                     "--strategy", str(STRATEGY), "--params", "@" + str(parameters), "--output", str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["final_equity"], 10198)
            document = json.loads((output / "result.json").read_text())
            self.assertEqual(document["metadata"]["order_execution"]["strategy"]["news_mode"], "disabled")
            self.assertEqual(document["fills"][0]["quantity_unit"], "lots")
            self.assertTrue((output / "report.html").is_file())

    def test_wrong_contract_symbol_is_rejected_before_backtesting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "quotes"
            import_ticks(ROOT / "examples/quote-ticks.csv", data,
                         TickDatasetSpec(symbol="ANOTHER_INSTRUMENT", source="fixture"),
                         TickCsvProfile(timezone="Europe/Kyiv"))
            request = RunRequest(data=data, output=root / "report", strategy=str(STRATEGY), parameters=params())
            with self.assertRaisesRegex(ValueError, "symbol does not match"):
                execute_experiment(request)
            self.assertFalse(request.output.exists())
