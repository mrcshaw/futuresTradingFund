"""A script that runs its own exit keeps it, research changes values only, and the loaded file is not rewritten."""

import unittest
from unittest import mock

from futuresfund.strategy_team import ensure_risk_management, own_exit, prepare_backtest

OWN = "\n".join([
    "//@version=5",
    'strategy("Own", overlay=true, initial_capital=25000)',
    'pv                = input.float(50.0, "Point Value ($)")',
    'stop_dollars      = input.float(500.0, "Stop ($)")',
    'tp_dollars        = input.float(1100.0, "Take Profit ($)")',
    'be_trigger_dollars = input.float(150.0, "BE Trigger ($)", minval=50)',
    "stop_pts = stop_dollars / pv",
    "tp_pts = tp_dollars / pv",
    "var float current_stop = na",
    "if strategy.position_size > 0 and strategy.openprofit >= be_trigger_dollars",
    "    current_stop := strategy.position_avg_price",
    "if close > open",
    '    strategy.entry("Long", strategy.long)',
    "    current_stop := close - stop_pts",
    '    strategy.exit("XL", "Long", stop=current_stop, limit=close + tp_pts)',
    "",
])


class OwnExitTests(unittest.TestCase):
    def test_a_break_even_exit_is_kept_as_written(self):
        self.assertTrue(own_exit(OWN))
        fixed = ensure_risk_management(OWN, 5)
        self.assertIn('strategy.exit("XL", "Long", stop=current_stop, limit=close + tp_pts)', fixed)
        self.assertIn('stop_dollars      = input.float(500.0, "Stop ($)")', fixed)
        self.assertIn('tp_dollars        = input.float(1100.0, "Take Profit ($)")', fixed)
        self.assertIn('pv                = input.float(5, "Point Value ($)")', fixed)
        self.assertIn("initial_capital=50000", fixed)
        self.assertNotIn("long_stop", fixed)
        self.assertNotIn("// ===== RISK =====", fixed)
        self.assertEqual(fixed.count('strategy.close_all(comment="Open P&L > SL Risk")'), 1)
        # The backstop goes right after the stop input, before anything reads it.
        lines = fixed.splitlines()
        at = next(index for index, line in enumerate(lines) if line.startswith("stop_dollars"))
        self.assertEqual(lines[at + 1], "if strategy.position_size != 0 and not na(stop_dollars)")
        self.assertEqual(ensure_risk_management(fixed, 5), fixed)

    def test_the_desk_exit_still_goes_on_a_script_without_its_own(self):
        plain = OWN.replace("stop=current_stop", "stop=close - stop_pts")
        self.assertFalse(own_exit(plain))
        self.assertIn("stop=long_stop", ensure_risk_management(plain, 50))
        self.assertFalse(own_exit('//@version=5\nstrategy("x")\nstrategy.exit("X", "L", stop=my_stop)\n'))

    def test_the_backtest_runs_the_scripts_own_exit(self):
        prepared = prepare_backtest(OWN, "ES")
        self.assertIn("stop=current_stop", prepared)
        self.assertIn("if strategy.openprofit <= -stop_dollars", prepared)


class ValuesOnlyTests(unittest.TestCase):
    def test_a_researcher_rewrite_is_not_used(self):
        from pathlib import Path

        from futuresfund import learn
        from futuresfund.board import Board

        asked = []

        def reasoned(board, path, script, trials, bounds, choices, why, response, redesign):
            asked.append((why, redesign))
            return {"because": "Swap the entry.", "script": "//@version=5\nstrategy('new')\n", "params": {"length": 12}, "redesigned": "x"}

        saved = {}
        job = {
            "key": "k|stall", "kind": "stall", "review": "", "board": Board(), "path": Path("s.pine"),
            "script": OWN, "trials": [{"params": {"length": 10}, "net_profit": -5}], "bounds": {}, "choices": {},
            "why": "It stalled.", "response": None, "researcher": "Chart researcher",
        }
        with mock.patch.object(learn, "_reasoned_next", reasoned), \
                mock.patch("futuresfund.script_diagnosis.load", lambda script: {}), \
                mock.patch("futuresfund.script_diagnosis.save", lambda script, record: saved.update(record)), \
                mock.patch.object(learn, "_reopen_for_answer", lambda *args: False):
            learn._decide(job)
        self.assertFalse(asked[0][1])
        self.assertIn("Only a SETTING line or KEEP is used", asked[0][0])
        self.assertIsNone(saved["answer"]["script"])
        self.assertEqual(saved["answer"]["params"], {"length": 12})
        self.assertFalse(saved["answer"]["redesign"])

    def test_no_skip_window_is_added(self):
        from futuresfund import learn

        self.assertTrue(learn.VALUES_ONLY)
        self.assertIsNone(learn._try_skip_window(None, None, [{"clock": {}}], None, "Chart researcher", None))


if __name__ == "__main__":
    unittest.main()
