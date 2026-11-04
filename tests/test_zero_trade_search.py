"""A script that trades is never parked because the search walked into settings that do not enter.

The GC 5m year search of the Day Shift BOS script started at 281 trades, moved fractal_len and
min_delta_pct to values that never enter, kept stepping from those values, and after 20 zero runs
moved the file to cannotRunStrategies. These tests hold the fixes for that.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from futuresfund import learn
from futuresfund.frequency import frequency_plan
from futuresfund.objective import scale_move


def _trading(trades=120, profit=4000.0, drawdown=1500.0, **params):
    return {"params": params, "net_profit": profit, "max_drawdown": drawdown, "trades": trades,
            "win_rate": 45.0, "profit_factor": 1.3, "days": 260}


def _dead(**params):
    return {"params": params, "net_profit": 0.0, "max_drawdown": 0.0, "trades": 0, "days": 260}


class _Folders:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.learning = root / "learningStrategies"
        self.aside = root / "cannotRunStrategies"
        self.learning.mkdir()
        self.script = self.learning / "day_shift_bos.pine"
        self.script.write_text('strategy("Day Shift BOS")\n', encoding="utf-8")
        self.saved = (learn.LEARNING, learn.CANNOT_RUN)
        learn.LEARNING, learn.CANNOT_RUN = self.learning, self.aside
        learn._flat_runs.clear()

    def close(self):
        learn._flat_runs.clear()
        learn.LEARNING, learn.CANNOT_RUN = self.saved
        self.tmp.cleanup()


def _executor(enters, holder=None):
    """A fake engine: trades while enters(params) is true, and takes no trades otherwise."""
    calls = []

    def execute(params):
        calls.append(dict(params))
        if enters(params):
            length = int(params.get("length") or 10)
            return {"net_profit": 3000.0 + 10 * length, "max_drawdown": 2500.0, "trades": 150 - length, "days": 260,
                    "win_rate": 38.0, "profit_factor": 1.05}
        return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0, "days": 260}

    if holder is not None:
        execute.source = holder
    execute.calls = calls
    return execute


class SearchKeepsATradingScript(unittest.TestCase):
    def setUp(self):
        self.folders = _Folders()

    def tearDown(self):
        self.folders.close()

    def test_a_search_that_traded_is_never_parked(self):
        # Like fractal_len on the Day Shift script: a long length never enters.
        execute = _executor(lambda params: int(params.get("length") or 0) < 15)
        trials = learn._search(
            {"length": 10, "cooldown": 20, "zone": 1.5}, execute, 50000, 40, None, self.folders.script, None,
            extras=False, developer="5min chart developer",
        )
        self.assertFalse(getattr(trials, "flat_stop", False))
        self.assertTrue(self.folders.script.is_file())
        self.assertFalse(self.folders.aside.exists() and any(self.folders.aside.glob("*.pine")))
        self.assertTrue(any(int(row.get("trades") or 0) > 0 for row in trials))

    def test_after_a_zero_run_the_next_attempt_starts_from_a_trading_setting(self):
        execute = _executor(lambda params: int(params.get("length") or 0) < 15)
        learn._search(
            {"length": 10, "cooldown": 20, "zone": 1.5}, execute, 50000, 40, None, self.folders.script, None,
            extras=False, developer="5min chart developer",
        )
        streak = longest = 0
        for params in execute.calls:
            streak = streak + 1 if int(params.get("length") or 0) >= 15 else 0
            longest = max(longest, streak)
        # Each dead length is tried once and the search goes back. It never stacks changes on a dead length.
        self.assertLessEqual(longest, 2)

    def test_two_dead_inputs_do_not_build_a_long_zero_streak(self):
        # Like the reviewer's case: fractal 8 or more, or cooldown 40 or more, never enters.
        execute = _executor(lambda params: int(params.get("fractal_len") or 0) < 8 and int(params.get("cooldown_bars") or 0) < 40)
        trials = learn._search(
            {"fractal_len": 5, "cooldown_bars": 30, "length": 10}, execute, 50000, 40, None, self.folders.script, None,
            extras=False, developer="5min chart developer",
        )
        streak = longest = 0
        for params in execute.calls:
            dead = int(params.get("fractal_len") or 0) >= 8 or int(params.get("cooldown_bars") or 0) >= 40
            streak = streak + 1 if dead else 0
            longest = max(longest, streak)
        self.assertLess(longest, 5)
        self.assertTrue(self.folders.script.is_file())
        self.assertTrue(any(int(row.get("trades") or 0) > 0 for row in trials))

    def test_the_climb_does_not_anchor_on_a_dead_row_before_a_trading_row(self):
        trials = [
            _trading(trades=281, fractal_len=5, cooldown_bars=20),
            _trading(trades=34, fractal_len=10, cooldown_bars=20),
            _dead(fractal_len=20, cooldown_bars=20),
            _trading(trades=30, fractal_len=15, cooldown_bars=20),
        ]
        seen = set()
        for _ in range(6):
            plan = learn.next_plan({"fractal_len": 5, "cooldown_bars": 20}, trials, 40, extras=False, extra_seen=seen)
            if plan is None:
                break
            self.assertNotEqual(plan.get("fractal_len"), 20)
            seen.add(learn._key(plan))

    def test_a_script_that_passed_a_stage_is_not_parked_by_its_year_search(self):
        holder = {"text": "", "root": "GC", "timeframe": "5m", "stage_runs": {90: {"trades": 77, "net_profit": 22103.0}}}
        execute = _executor(lambda params: False, holder)
        for _ in range(learn.FLAT_RUNS + 2):
            trials = learn._search(
                {"length": 10}, execute, 50000, 30, None, self.folders.script, None,
                extras=False, developer="5min chart developer",
            )
            self.assertFalse(getattr(trials, "flat_stop", False))
        self.assertTrue(self.folders.script.is_file())

    def test_the_zero_count_is_kept_per_chart(self):
        learn._flat_runs[learn._flat_key(self.folders.script, "ES|2m")] = learn.FLAT_RUNS - 1
        holder = {"text": "", "root": "GC", "timeframe": "5m"}
        trials = learn._search(
            {"length": 10}, _executor(lambda params: False, holder), 50000, 30, None, self.folders.script, None,
            extras=False, developer="5min chart developer",
        )
        # Another chart's 19 zero runs do not count against GC 5m.
        self.assertFalse(getattr(trials, "flat_stop", False))
        self.assertTrue(self.folders.script.is_file())

    def test_a_script_that_never_enters_is_still_parked(self):
        holder = {"text": "", "root": "GC", "timeframe": "5m"}
        execute = _executor(lambda params: False, holder)
        parked = False
        for _ in range(learn.FLAT_RUNS):
            trials = learn._search(
                {"length": 10}, execute, 50000, 30, None, self.folders.script, None,
                extras=False, developer="5min chart developer",
            )
            if getattr(trials, "flat_stop", False):
                parked = True
                break
        self.assertTrue(parked)
        self.assertFalse(self.folders.script.is_file())


class ZeroIsNotProgress(unittest.TestCase):
    def test_a_move_that_stops_every_entry_is_farther(self):
        self.assertEqual(scale_move(_trading(trades=281, profit=6869.0, drawdown=20097.5), _dead(), 50000), "farther")
        self.assertEqual(scale_move(_trading(trades=34, profit=4096.0, drawdown=1500.0), _dead(), 50000), "farther")

    def test_the_climb_turns_back_from_a_change_that_stopped_every_entry(self):
        self.assertEqual(learn._climb_choice(_trading(fractal_len=10), _dead(fractal_len=20), True, "fractal_len"), "reverse")

    def test_the_trade_floor_does_not_loosen_filters_on_a_dead_setting(self):
        source = "cooldown_bars = input.int(95)\nmin_delta_pct = input.float(5)\n"
        trials = [_trading(cooldown_bars=95, min_delta_pct=5.0, fractal_len=5), _dead(cooldown_bars=95, min_delta_pct=5.0, fractal_len=20)]
        self.assertIsNone(frequency_plan(trials[0]["params"], trials, source))
        # A script that has never traded still loosens its filters.
        never = [_dead(cooldown_bars=95, min_delta_pct=5.0, fractal_len=5)]
        self.assertIsNotNone(frequency_plan(never[0]["params"], never, source))

    def test_the_next_plan_after_a_zero_run_starts_from_the_trading_run(self):
        trials = [_trading(length=10, zone=1.5), _dead(length=20, zone=1.5)]
        plan = learn.next_plan({"length": 10, "zone": 1.5}, trials, 40, extras=False)
        self.assertIsNotNone(plan)
        self.assertLess(int(plan["length"]), 20)


if __name__ == "__main__":
    unittest.main()
