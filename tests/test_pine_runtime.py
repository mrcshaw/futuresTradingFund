"""The Pine runtime against TradingView's rules and against TradingView's own trade lists."""

import unittest
from pathlib import Path

from futuresfund.backtrader_engine import Bar
from futuresfund.pine_runtime import PineUnsupported, run_pine

ROOT = Path(__file__).resolve().parents[1]
HEADER = (
    '//@version=5\n'
    'strategy("Rule check", overlay=true, initial_capital=25000, default_qty_type=strategy.fixed, default_qty_value=1,\n'
    '  commission_type=strategy.commission.cash_per_contract, commission_value=5.50, slippage=1, pyramiding=0,\n'
    '  process_orders_on_close={close})\n'
)


def _bars(closes, start=1_788_307_200, width=120):
    bars = []
    for k, close in enumerate(closes):
        open_ = closes[k - 1] if k else close
        bars.append(Bar(start + k * width, open_, max(open_, close) + 0.5, min(open_, close) - 0.5, close, 10))
    return bars


class PineRuleTests(unittest.TestCase):
    def run_script(self, body, closes, on_close=True):
        source = HEADER.format(close="true" if on_close else "false") + body
        return run_pine(source, _bars(closes), {}, root="ES", timeframe="2m")

    def test_macd_signal_and_histogram_can_be_read(self):
        body = (
            "hist = ta.macd(close, 12, 26, 9).hist\n"
            "signal = ta.macd(close, 12, 26, 9).signal\n"
            "if bar_index > 40 and hist > 0 and signal < hist\n"
            "    strategy.entry(\"Long\", strategy.long)\n"
        )
        closes = [100 + (k % 9) * 0.25 for k in range(80)]
        self.run_script(body, closes)

    def test_an_order_on_close_fills_at_that_close_plus_a_tick(self):
        body = 'if bar_index == 2\n    strategy.entry("Long", strategy.long)\n    strategy.exit("XL", "Long", limit=close + 2)\n'
        trades = self.run_script(body, [100, 100.25, 100.5, 101, 102, 103])
        self.assertEqual(trades[0]["entry_price"], 100.75)
        self.assertEqual(trades[0]["exit_price"], 102.5)
        self.assertEqual(trades[0]["exit_signal"], "XL")
        self.assertEqual(trades[0]["pnl"], round((102.5 - 100.75) * 50 - 11, 2))

    def test_a_close_on_the_entry_bar_does_not_flatten_the_new_trade(self):
        body = (
            'if bar_index == 2\n'
            '    strategy.entry("Long", strategy.long)\n'
            '    strategy.close("Long", comment="Same")\n'
            'if bar_index == 5\n'
            '    strategy.close("Long", comment="Later")\n'
        )
        trades = self.run_script(body, [100, 100.25, 100.5, 101, 102, 103, 104])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_signal"], "Later")
        self.assertNotEqual(trades[0]["entry_price"], trades[0]["exit_price"])

    def test_a_one_bar_stop_is_not_yet_a_closed_position_on_that_bar(self):
        body = (
            'var bool in_trade = false\n'
            'if strategy.position_size == 0 and strategy.position_size[1] != 0\n'
            '    in_trade := false\n'
            'if not in_trade and (bar_index == 2 or bar_index == 6)\n'
            '    in_trade := true\n'
            '    strategy.entry("Long", strategy.long)\n'
            '    strategy.exit("XL", "Long", stop=close - 1)\n'
        )
        stopped = self.run_script(body, [100, 100, 100, 98, 98, 98, 100, 97])
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["exit_signal"], "XL")
        held = self.run_script(body, [100, 100, 100, 100, 98, 98, 100, 97])
        self.assertEqual(len(held), 2)
        self.assertEqual([trade["exit_signal"] for trade in held], ["XL", "XL"])

    def test_a_rewritten_stop_replaces_the_old_one(self):
        body = (
            'if bar_index == 1\n'
            '    strategy.entry("Long", strategy.long)\n'
            'strategy.exit("XL", "Long", stop=close - 10)\n'
        )
        trades = self.run_script(body, [100, 100, 95, 84])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_signal"], "XL")
        self.assertEqual(trades[0]["exit_price"], 84.75)

    def test_without_orders_on_close_the_order_fills_at_the_next_open(self):
        body = 'if bar_index == 2\n    strategy.entry("Long", strategy.long)\nif bar_index == 4\n    strategy.close("Long", comment="Done")\n'
        trades = self.run_script(body, [100, 100.25, 100.5, 101, 102, 103], on_close=False)
        self.assertEqual(trades[0]["entry_price"], 100.75)
        self.assertEqual(trades[0]["entry_time"], _bars([0] * 6)[3].when.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(trades[0]["exit_price"], 101.75)
        self.assertEqual(trades[0]["exit_signal"], "Done")

    def test_a_stop_is_rounded_to_the_tick_and_fills_a_tick_beyond(self):
        body = 'if bar_index == 1\n    strategy.entry("Short", strategy.short)\n    strategy.exit("XS", "Short", stop=close + 1.13)\n'
        trades = self.run_script(body, [100, 100, 100.5, 101.5, 102])
        self.assertEqual(trades[0]["entry_price"], 99.75)
        self.assertEqual(trades[0]["exit_price"], 101.5)

    def test_an_open_trade_stop_is_moved_to_the_next_tick_away_from_the_fill(self):
        body = (
            'if bar_index == 1\n'
            '    strategy.entry("Long", strategy.long)\n'
            'if strategy.position_size > 0\n'
            '    strategy.exit("XL", "Long", stop=strategy.position_avg_price - 1.10)\n'
        )
        trades = self.run_script(body, [100, 100, 100, 98, 97], on_close=False)
        self.assertEqual(trades[0]["entry_price"], 100.25)
        self.assertEqual(trades[0]["exit_price"], 98.75)

    def test_a_session_day_changes_at_18_00_new_york_and_not_on_every_bar(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from futuresfund.pine_runtime import run_pine_live

        et = ZoneInfo("America/New_York")
        stamps = [
            int(datetime(2026, 10, 7, hour, minute, tzinfo=et).timestamp())
            for hour, minute in ((16, 0), (16, 15), (18, 0), (18, 15))
        ]
        bars = [Bar(stamp, 100, 101, 99, 100, 10) for stamp in stamps]
        body = (
            'var int days = 0\n'
            'if ta.change(time("D")) != 0\n'
            '    days += 1\n'
            'if days == 1 and bar_index == 2\n'
            '    strategy.entry("Long", strategy.long)\n'
            'if days == 2\n'
            '    strategy.close_all(comment="Next day")\n'
        )
        state = run_pine_live(HEADER.format(close="true") + body, bars, {}, root="ES", timeframe="15m")
        self.assertEqual(state["trades"], [])
        self.assertIsNotNone(state["open"])
        self.assertEqual(state["open"]["entry_signal"], "Long")

    def test_an_overnight_session_uses_the_day_it_ends(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from futuresfund.pine_lib import isna
        from futuresfund.pine_runtime import Runtime

        et = ZoneInfo("America/New_York")
        # 1800-1700:12345 is Sunday through Thursday. Thursday evening would
        # start Friday, and Friday is not in the list, so Friday stays shut.
        moments = (
            (datetime(2026, 10, 4, 12, 0, tzinfo=et), True),
            (datetime(2026, 10, 4, 18, 0, tzinfo=et), True),
            (datetime(2026, 10, 5, 16, 59, tzinfo=et), True),
            (datetime(2026, 10, 5, 17, 0, tzinfo=et), False),
            (datetime(2026, 10, 5, 18, 0, tzinfo=et), True),
            (datetime(2026, 10, 8, 16, 30, tzinfo=et), True),
            (datetime(2026, 10, 8, 18, 0, tzinfo=et), False),
            (datetime(2026, 10, 9, 10, 0, tzinfo=et), False),
            (datetime(2026, 10, 9, 16, 0, tzinfo=et), False),
            (datetime(2026, 10, 9, 17, 0, tzinfo=et), False),
            (datetime(2026, 10, 9, 18, 0, tzinfo=et), False),
            (datetime(2026, 10, 10, 12, 0, tzinfo=et), False),
        )
        bars = [Bar(int(moment.timestamp()), 100, 101, 99, 100, 10) for moment, _open in moments]
        runtime = Runtime(bars, {}, {}, root="ES", timeframe="1m")
        for index, (_moment, open_) in enumerate(moments):
            stamp = runtime.session_time(index, "1800-1700:12345", "America/New_York")
            self.assertEqual(not isna(stamp), open_)

    def test_history_var_and_a_loop_that_counts_down(self):
        body = (
            'var int count = 0\n'
            'count += 1\n'
            'int total = 0\n'
            'for k = 3 to 1\n'
            '    total += k\n'
            'if count == 3 and close[1] == 101 and total == 6 and not (close[50] > 0)\n'
            '    strategy.entry("Long", strategy.long)\n'
            'if count == 4\n'
            '    strategy.close_all(comment="Out")\n'
        )
        trades = self.run_script(body, [100, 101, 102, 103, 104])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_price"], 102.25)
        self.assertEqual(trades[0]["exit_signal"], "Out")

    def test_if_and_switch_values_and_a_function_that_returns_two_values(self):
        body = (
            'pair(x) =>\n'
            '    if x > 1\n'
            '        [x, x * 2]\n'
            '    else\n'
            '        [0, 0]\n'
            '[a, b] = pair(bar_index)\n'
            'side = switch\n'
            '    a == 2 => "long"\n'
            '    => "none"\n'
            'level = if b == 4\n'
            '    close\n'
            'else\n'
            '    na\n'
            'if side == "long" and not na(level)\n'
            '    strategy.entry("Long", strategy.long)\n'
            '    strategy.exit("XL", "Long", limit=level + 1)\n'
        )
        trades = self.run_script(body, [100, 100, 100, 100.5, 101.5, 102])
        self.assertEqual(trades[0]["entry_price"], 100.25)
        self.assertEqual(trades[0]["exit_price"], 101.0)

    def test_a_trade_without_a_session_close_stays_open_past_1645(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        start = datetime(2026, 10, 7, 10, 0, tzinfo=et)
        stamps = [int((start + timedelta(minutes=5 * i)).timestamp()) for i in range(24 * 12)]
        bars = [Bar(stamp, 4400, 4402, 4398, 4401, 10) for stamp in stamps]
        source = (
            "//@version=5\n"
            "strategy(\"Hold\", overlay=true, process_orders_on_close=true)\n"
            "if bar_index == 0\n"
            "    strategy.entry(\"Long\", strategy.long)\n"
        )
        trades = run_pine(source, bars, {}, root="ES", timeframe="5m")
        self.assertEqual(trades, [])

    def test_a_tied_high_is_still_the_pivot_tradingview_reports(self):
        import math

        from futuresfund.pine_lib import _pivot

        state = type("State", (), {"vals": []})()
        bars = type("Bars", (), {"H": [10.0, 12.0, 12.0, 11.0], "L": [9.0, 8.0, 8.0, 7.0]})()
        found = [_pivot(state, i, bars, (1, 1), True) for i in range(4)]
        self.assertTrue(math.isnan(found[0]))
        self.assertTrue(math.isnan(found[2]))
        self.assertEqual(found[3], 12.0)
        strict = type("State", (), {"vals": []})()
        higher = type("Bars", (), {"H": [10.0, 12.0, 13.0], "L": [1.0, 1.0, 1.0]})()
        self.assertTrue(all(math.isnan(value) for value in (_pivot(strict, i, higher, (1, 1), True) for i in range(3))))

    def test_an_indicator_is_not_a_strategy(self):
        with self.assertRaises(PineUnsupported):
            run_pine('//@version=5\nindicator("x")\nplot(close)\n', _bars([1, 2, 3]), {})

    def test_a_valuewhen_on_the_right_of_and_still_sees_every_bar(self):
        # The left side is true on the last bars only. valuewhen must still have
        # recorded every earlier close, or the previous value is missing.
        body = (
            "ready = close == 5 and ta.valuewhen(bar_index >= 0, close, 1) == 4\n"
            "if ready\n"
            "    strategy.entry(\"Long\", strategy.long)\n"
            "if bar_index == 6\n"
            "    strategy.close_all(comment=\"Out\")\n"
        )
        trades = self.run_script(body, [1, 2, 3, 4, 5, 5, 5])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_price"], 5.25)


class TradingViewExportTests(unittest.TestCase):
    """The same script on the same bars gives TradingView's trades."""

    def test_bos_breakout_evening_matches_the_strategy_tester(self):
        from futuresfund.tv_checks import check_export

        report = check_export(ROOT / "BOS_Breakout_Evening_ES_2min_CME_MINI_ES1!_2026-10-02_48b4d.csv")
        self.assertEqual(report["expected"], 31)
        self.assertGreaterEqual(report["matched"], 29)
        same = report["identical_from"]
        self.assertLessEqual(same["from"], "2026-09-14 19:52")
        self.assertEqual(same["engine"], same["tradingview"])
        self.assertEqual(report["tradingview"]["net_profit"], 8409.0)
        self.assertEqual(report["tradingview"]["max_drawdown"], 2729.0)
        self.assertEqual(report["tradingview"]["win_rate"], 67.74)
        self.assertEqual(report["tradingview"]["profit_factor"], 2.035)

    def test_poc_confluence_matches_the_strategy_tester(self):
        from futuresfund.tv_checks import check_export

        report = check_export(ROOT / "POC_Confluence_CME_MINI_ES1!_2026-10-02_ea063.csv")
        self.assertEqual(report["expected"], 105)
        self.assertGreaterEqual(report["matched"], 97)
        self.assertFalse(any(trade["entry_time"] == "2026-09-07 06:45" for trade in report["missed"]))
        self.assertFalse(any(trade["entry_time"] == "2026-09-07 06:45" for trade in report["extras"]))
        same = report["identical_from"]
        self.assertEqual(same["from"], "2026-10-01 00:30")
        self.assertEqual(same["engine"], same["tradingview"])
