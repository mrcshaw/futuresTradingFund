"""Prove in stages: the last 7 days, then 30, then 90, then the year, each from the settings that passed before."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import book
from futuresfund.objective import PROVING_STAGES, stage_passed


def _default_book(test):
    """The acceptance boxes at their defaults, not the ones saved on this desk."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    patcher = mock.patch.object(book, "BOOK_PATH", Path(folder.name) / "book.json")
    patcher.start()
    test.addCleanup(patcher.stop)


def _row(profit, trades, drawdown=200.0, worst=-150.0):
    return {"net_profit": profit, "trades": trades, "max_drawdown": drawdown, "biggest_loser": worst, "win_rate": 50.0}


class StageGateTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)

    def test_a_profitable_week_with_three_trades_passes(self):
        self.assertTrue(stage_passed(_row(400, 3), 7))

    def test_a_loss_or_too_few_trades_stays_on_the_stage(self):
        self.assertFalse(stage_passed(_row(-50, 6), 7))
        self.assertFalse(stage_passed(_row(400, 2), 7))

    def test_a_stage_needs_three_trades_a_week(self):
        # 3 trades for every 7 days: 13 on 30 days, 39 on 90, 157 on the year.
        self.assertFalse(stage_passed(_row(900, 12), 30))
        self.assertTrue(stage_passed(_row(900, 13), 30))
        self.assertFalse(stage_passed(_row(900, 38), 90))
        self.assertTrue(stage_passed(_row(900, 39), 90))
        self.assertFalse(stage_passed(_row(9000, 156), 365))
        self.assertTrue(stage_passed(_row(9000, 157), 365))

    def test_the_drawdown_cap_and_the_single_trade_loss_still_apply(self):
        # Drawdown under 5 percent of $50,000, and no trade down more than $1,700.
        self.assertFalse(stage_passed(_row(4000, 15, drawdown=2600), 30))
        self.assertFalse(stage_passed(_row(4000, 15, worst=-1800), 30))
        self.assertFalse(stage_passed({"error": "stopped", "net_profit": 10, "trades": 9}, 7))


class RecentWindowCheckTests(unittest.TestCase):
    """The year chooses the settings. The last 7, 30, and 90 days check them, shortest first."""

    def setUp(self):
        _default_book(self)

    def _executor(self, results):
        windows = []

        def execute(params):
            windows.append(execute.source["sessions"])
            return results(execute.source["sessions"], params)

        execute.source = {"text": "", "stamp": 0, "sessions": None}
        return execute, windows

    def test_settings_that_pass_every_window_pass_the_check(self):
        from futuresfund.learn import _stage_checks

        execute, windows = self._executor(lambda sessions, params: {"net_profit": 600 * sessions, "max_drawdown": 100, "trades": 40, "days": sessions})
        check = _stage_checks(execute)
        self.assertIsNone(check({"params": {"length": 10}}))
        self.assertEqual(windows, [sessions for _days, sessions in PROVING_STAGES])
        self.assertEqual(sorted(check.runs), [7, 30, 90])
        self.assertIsNone(execute.source["sessions"])

    def test_the_first_window_that_fails_ends_the_check(self):
        from futuresfund.learn import _stage_checks

        def results(sessions, params):
            if sessions == 5:
                return {"net_profit": 600, "max_drawdown": 100, "trades": 6, "days": 5}
            return {"net_profit": -300, "max_drawdown": 400, "trades": 30, "days": sessions}

        execute, windows = self._executor(results)
        check = _stage_checks(execute)
        self.assertEqual(check({"params": {"length": 10}}), 30)
        self.assertNotIn(64, windows)

    def test_the_same_settings_are_checked_once(self):
        from futuresfund.learn import _stage_checks

        execute, windows = self._executor(lambda sessions, params: {"net_profit": -50, "max_drawdown": 100, "trades": 6, "days": sessions})
        check = _stage_checks(execute)
        self.assertEqual(check({"params": {"length": 10}}), 7)
        self.assertEqual(check({"params": {"length": 10}, "net_profit": 9000}), 7)
        self.assertEqual(windows, [5])

    def test_an_executor_without_a_window_has_no_check(self):
        from futuresfund.learn import _stage_checks

        self.assertIsNone(_stage_checks(lambda params: {}))

    def test_a_year_that_fails_a_recent_window_is_not_filed(self):
        from futuresfund import learn

        board = mock.Mock()
        row = {"net_profit": 9000, "engine": "pinets", "params": {"length": 10}}
        with mock.patch("futuresfund.library.library_ready", return_value=True),                 mock.patch("futuresfund.pine_check.tv_problems", return_value=[]),                 mock.patch("futuresfund.library.record_untested") as filed:
            learn._stage_gate.check = lambda checked: 30
            try:
                self.assertFalse(learn._offer_library(board, {}, Path("year.pine"), row, "5 min chart developer"))
            finally:
                learn._stage_gate.check = None
        filed.assert_not_called()
        self.assertIn("did not pass the last 30 days", board.post.call_args.args[1])


class ProvingTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)

    def _executor(self, results):
        windows = []

        def execute(params):
            windows.append(execute.source["sessions"])
            return results(execute.source["sessions"], params)

        execute.source = {"text": "", "stamp": 0, "sessions": None}
        return execute, windows

    def test_every_stage_passes_and_the_year_starts_from_those_settings(self):
        from futuresfund.learn import _prove_stages

        execute, windows = self._executor(lambda sessions, params: {"net_profit": 600, "max_drawdown": 100, "trades": 40, "days": sessions})
        with mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: None):
            settings, stopped = _prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None)
        self.assertIsNone(stopped)
        self.assertEqual(settings["length"], 10)
        self.assertEqual(windows, [sessions for _days, sessions in PROVING_STAGES])
        self.assertIsNone(execute.source["sessions"])
        # The run that passed each stage is kept for the losing-time check at the end of the study.
        self.assertEqual(sorted(execute.source["stage_runs"]), [7, 30, 90])

    def test_a_script_that_fails_a_stage_never_reaches_the_longer_windows(self):
        from futuresfund.learn import STAGE_ATTEMPTS, _prove_stages

        def results(sessions, params):
            if sessions == 5:
                return {"net_profit": 600, "max_drawdown": 100, "trades": 6, "days": 5}
            return {"net_profit": -300 - float(params.get("length", 0)), "max_drawdown": 400, "trades": 30, "days": sessions}

        execute, windows = self._executor(results)
        with mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: None):
            settings, stopped = _prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None)
        self.assertIsNone(settings)
        self.assertEqual(stopped.stage_failed, 30)
        self.assertNotIn(64, windows)
        self.assertLessEqual(windows.count(21), STAGE_ATTEMPTS)
        self.assertTrue(all(row.get("stage_days") == 30 for row in stopped))

    def test_a_stage_keeps_trying_when_a_change_moves_no_trade(self):
        from futuresfund.learn import STAGE_ATTEMPTS, _prove_stages

        # Every setting gives the same losing week, as a stop change often does on a short window.
        execute, windows = self._executor(lambda sessions, params: {"net_profit": -200, "max_drawdown": 300, "trades": 5, "days": sessions})
        with mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: None):
            settings, stopped = _prove_stages(
                {"length": 10, "stop_dollars": 500.0, "tp_dollars": 1000.0}, execute, None, None, None, None,
                "5 min chart developer", "Chart researcher", None,
            )
        self.assertIsNone(settings)
        self.assertEqual(stopped.stage_failed, 7)
        self.assertGreater(len(windows), 3)
        self.assertLessEqual(len(windows), 2 * STAGE_ATTEMPTS + 1)

    def test_an_executor_without_a_window_goes_straight_to_the_year(self):
        from futuresfund.learn import _prove_stages

        settings, stopped = _prove_stages({"length": 10}, lambda params: {}, None, None, None, None, "", "", None)
        self.assertEqual(settings, {"length": 10})
        self.assertIsNone(stopped)

    def test_the_study_proves_the_stages_before_the_year(self):
        import inspect

        from futuresfund import learn

        source = inspect.getsource(learn)
        study = source[source.index("        proven, stopped = _prove_stages("):]
        self.assertLess(study.index("_prove_stages("), study.index("trials = stopped if stopped is not None else _search("))


@unittest.skipUnless(shutil.which("node"), "PineTS needs Node")
class StageWindowTests(unittest.TestCase):
    def test_only_trades_inside_the_last_sessions_count(self):
        from datetime import datetime

        from futuresfund.backtrader_engine import execute_script
        from futuresfund.objective import window_start

        source = (
            '//@version=5\n'
            'strategy("Every hour", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1)\n'
            'if bar_index % 12 == 0\n'
            '    strategy.entry("Long", strategy.long)\n'
            'if bar_index % 12 == 6\n'
            '    strategy.close_all(comment="Out")\n'
        )
        start = 1_788_307_200  # 2026-09-02 00:00 UTC, a Wednesday
        bars = [{"t": start + k * 300, "o": 100 + k % 7, "h": 101 + k % 7, "l": 99 + k % 7, "c": 100 + k % 7, "v": 10} for k in range(288 * 20)]
        whole = execute_script(source, bars, "5m", None, "ES", "Every hour", 1, full=True)
        week = execute_script(source, bars, "5m", None, "ES", "Every hour", 1, sessions=5)
        cut = window_start((row["t"] for row in bars), 5)
        self.assertEqual(week["window"], "5 sessions")
        self.assertLess(week["trades"], whole["trades"])
        self.assertGreater(week["trades"], 0)
        self.assertEqual(week["tested_from"], datetime.fromtimestamp(cut).astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M"))


if __name__ == "__main__":
    unittest.main()
