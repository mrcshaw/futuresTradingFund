"""The skip window: the time of day that keeps losing is found, written into a script, and skipped in PineTS."""

import shutil
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import skip_window
from futuresfund.backtrader_engine import Bar, script_trades

EVERY_BAR = (
    '//@version=5\n'
    'strategy("Every bar", overlay=true, process_orders_on_close=true, default_qty_type=strategy.fixed, default_qty_value=1)\n'
    'if strategy.position_size != 0\n'
    '    strategy.close_all()\n'
    'else\n'
    '    strategy.entry("Long", strategy.long,\n'
    '      comment="in")\n'
)


def _trade(when, pnl):
    return {"entry_time": f"2026-09-02 {when}", "exit_time": f"2026-09-02 {when}", "pnl": pnl}


class ClockTests(unittest.TestCase):
    def test_trades_are_kept_by_the_half_hour_they_entered(self):
        found = skip_window.clock([_trade("09:31", -100), _trade("09:59", -50), _trade("10:00", 75), {"entry_time": "2026-09-02 11:00", "pnl": 5}])
        self.assertEqual(found, {"09:30": [2, -150.0], "10:00": [1, 75.0]})

    def test_the_stretch_that_lost_the_most_is_the_window(self):
        trades = [_trade("02:05", -200) for _ in range(3)] + [_trade("02:40", -150) for _ in range(2)]
        trades += [_trade("20:00", 100) for _ in range(10)] + [_trade("12:10", -40)]
        window = skip_window.worst_window(skip_window.clock(trades))
        self.assertEqual(window, {"start": "02:00", "end": "03:00", "trades": 5, "profit": -900.0})

    def test_a_window_can_run_past_midnight(self):
        trades = [_trade("23:40", -300) for _ in range(3)] + [_trade("00:10", -300) for _ in range(3)] + [_trade("05:00", 50) for _ in range(8)]
        window = skip_window.worst_window(skip_window.clock(trades))
        self.assertEqual((window["start"], window["end"]), ("23:30", "00:30"))

    def test_too_few_trades_or_no_standout_loss_gives_no_window(self):
        self.assertIsNone(skip_window.worst_window(skip_window.clock([_trade("02:05", -500) for _ in range(5)])))
        spread = [_trade(f"{hour:02d}:00", -10) for hour in range(24)]
        self.assertIsNone(skip_window.worst_window(skip_window.clock(spread)))


class HabitTests(unittest.TestCase):
    @staticmethod
    def _run(open_loss, other=60):
        trades = [_trade("09:35", open_loss) for _ in range(3)] + [_trade("09:50", open_loss) for _ in range(2)]
        return skip_window.clock(trades + [_trade("20:00", other) for _ in range(10)])

    def test_the_open_losing_on_every_passed_run_is_the_window(self):
        window = skip_window.consistent_window([self._run(-120), self._run(-80), self._run(-150)])
        self.assertEqual((window["start"], window["end"], window["runs"]), ("09:30", "10:00", 3))

    def test_one_passed_run_or_a_run_that_made_money_there_is_not_a_habit(self):
        self.assertIsNone(skip_window.consistent_window([self._run(-120)]))
        self.assertIsNone(skip_window.consistent_window([self._run(-200), self._run(-200), self._run(30)]))


class ScriptTests(unittest.TestCase):
    WINDOW = {"start": "02:00", "end": "03:00", "trades": 5, "profit": -900.0}

    def test_the_section_is_added_and_every_entry_is_guarded(self):
        text = skip_window.add_skip_window(EVERY_BAR, self.WINDOW)
        self.assertIn('skip_start_hr = input.int(2, "Skip Start Hour (ET)"', text)
        self.assertIn('skip_end_hr   = input.int(3, "Skip End Hour (ET)"', text)
        self.assertIn(
            '    if skip_window_open\n'
            '        strategy.entry("Long", strategy.long,\n'
            '          comment="in")\n',
            text,
        )
        self.assertTrue(skip_window.has_skip(text))

    def test_a_script_with_the_section_only_has_the_window_moved(self):
        text = skip_window.add_skip_window(EVERY_BAR, self.WINDOW)
        moved = skip_window.add_skip_window(text, {"start": "14:30", "end": "15:00", "trades": 4, "profit": -300})
        self.assertIn("input.int(14, \"Skip Start Hour (ET)\"", moved)
        self.assertIn("input.int(30, \"Skip Start Minute\"", moved)
        self.assertEqual(moved.count("if skip_window_open"), 1)

    @unittest.skipUnless(shutil.which("node"), "PineTS needs Node")
    def test_pinets_opens_no_trade_inside_the_window(self):
        # Five-minute bars from 20:00 New York time, a little over a day.
        bars = [Bar(1_788_307_200 + k * 300, 100, 101, 99, 100, 10) for k in range(320)]
        text = skip_window.add_skip_window(EVERY_BAR, self.WINDOW)
        trades = script_trades(text, bars, None, "ES", "5m", as_written=True)
        times = [trade["entry_time"][11:16] for trade in trades]
        self.assertGreater(len(times), 100)
        self.assertFalse([when for when in times if "02:00" <= when < "03:00"])
        self.assertTrue([when for when in times if "01:00" <= when < "02:00"])
        off = script_trades(text, bars, {"use_skip": False}, "ES", "5m", as_written=True)
        self.assertTrue([trade for trade in off if "02:00" <= trade["entry_time"][11:16] < "03:00"])


class ResearchTests(unittest.TestCase):
    def _trials(self):
        losing = [_trade("02:05", -200) for _ in range(5)] + [_trade("20:00", 100) for _ in range(8)]
        return [{"net_profit": -200, "trades": 13, "params": {"stop_dollars": 500}, "clock": skip_window.clock(losing)}]

    def test_a_failed_stage_gets_the_section_and_searches_from_the_window(self):
        from futuresfund import learn

        board = mock.Mock()
        board.cancel.is_set.return_value = False
        execute = mock.Mock()
        execute.source = {"text": EVERY_BAR}
        with mock.patch.object(learn, "_install_checked", return_value="installed") as installed:
            settings = learn._try_skip_window(board, Path("every.pine"), self._trials(), execute, "Chart researcher", 0)
        revised = installed.call_args.args[2]
        self.assertIn("if skip_window_open", revised)
        self.assertEqual(settings, {"stop_dollars": 500, "use_skip": True, "skip_start_hr": 2, "skip_start_mn": 0, "skip_end_hr": 2, "skip_end_mn": 30})

    def test_a_rewrite_that_does_not_hold_leaves_the_stage_failed(self):
        from futuresfund import learn

        board = mock.Mock()
        board.cancel.is_set.return_value = False
        execute = mock.Mock()
        execute.source = {"text": EVERY_BAR}
        with mock.patch.object(learn, "_install_checked", return_value=None):
            self.assertIsNone(learn._try_skip_window(board, Path("every.pine"), self._trials(), execute, "Chart researcher", 0))

    def test_a_script_with_the_section_is_searched_from_the_new_window_without_a_rewrite(self):
        from futuresfund import learn

        board = mock.Mock()
        board.cancel.is_set.return_value = False
        execute = mock.Mock()
        execute.source = {"text": skip_window.add_skip_window(EVERY_BAR, {"start": "14:00", "end": "15:00"})}
        with mock.patch.object(learn, "_install_checked") as installed:
            settings = learn._try_skip_window(board, Path("every.pine"), self._trials(), execute, "Chart researcher", 0)
        installed.assert_not_called()
        self.assertEqual(settings["skip_start_hr"], 2)

    def test_after_a_pass_a_moved_window_is_kept_only_when_it_raises_the_result(self):
        from futuresfund import learn

        board = mock.Mock()
        board.cancel.is_set.return_value = False
        source = {"text": skip_window.add_skip_window(EVERY_BAR, {"start": "14:00", "end": "15:00"})}
        passed = [{"net_profit": 1500, "max_drawdown": 400, "trades": 20, "params": {"stop_dollars": 500}}]
        habit = {"start": "09:30", "end": "10:00", "trades": 5, "profit": -500}
        worse = mock.Mock(return_value={"net_profit": 1200, "max_drawdown": 400, "trades": 15})
        worse.source = source
        self.assertIsNone(learn._try_skip_window(board, Path("every.pine"), passed, worse, "Chart researcher", 0, window=habit, must_improve=True))
        better = mock.Mock(return_value={"net_profit": 2100, "max_drawdown": 300, "trades": 15})
        better.source = source
        settings = learn._try_skip_window(board, Path("every.pine"), passed, better, "Chart researcher", 0, window=habit, must_improve=True)
        self.assertEqual((settings["skip_start_hr"], settings["skip_start_mn"], settings["skip_end_hr"]), (9, 30, 10))


if __name__ == "__main__":
    unittest.main()
