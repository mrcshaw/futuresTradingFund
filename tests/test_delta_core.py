"""Delta volume breakout first: the core is recognized, studied first, and tracked by instrument."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import delta_core

LIMITS = {
    "min_profit": 6000.0, "profit_factor": 1.1, "win_rate": 40.0, "min_trades": 100, "drawdown_pct": 2.0, "goal": "win_rate",
    "use_drawdown": True, "use_trades": True, "use_profit": True, "use_factor": True, "use_win_rate": True,
}
PASSING = {
    "net_profit": 9000, "max_drawdown": 1000, "trades": 200, "days": 260, "win_rate": 50.0, "profit_factor": 1.5,
    "biggest_loser": -400, "stop_dollars": 500,
    "spans": {
        "7": {"net_profit": 300, "trades": 5, "biggest_loser": -100},
        "30": {"net_profit": 1200, "trades": 20, "biggest_loser": -100},
        "90": {"net_profit": 3000, "trades": 60, "biggest_loser": -100},
        "365": {"net_profit": 9000, "trades": 200, "biggest_loser": -100},
    },
}
CORE = (
    '//@version=5\nstrategy("Delta Volume Breakout", overlay=true)\n'
    'profile_lookback = input.int(120, "Profile Lookback (bars)", minval=20, maxval=2000)\n'
    'min_delta_pct = input.float(10.0, "Min Delta Imbalance (%)", minval=0, maxval=80)\n'
    "var array<float> vol_up = array.new_float(100, 0.0)\n"
    "float poc_hi = na\nfloat poc_delta_pct = na\n"
    "bool above_poc = not na(poc_hi) and close > poc_hi\n"
)
PLAIN = '//@version=5\nstrategy("EMA Cross", overlay=true)\nfast = ta.ema(close, 9)\nif ta.crossover(close, fast)\n    strategy.entry("L", strategy.long)\n'


class CoreTests(unittest.TestCase):
    def test_the_volume_profile_poc_and_delta_make_the_core(self):
        self.assertTrue(delta_core.has_core(CORE))
        self.assertFalse(delta_core.has_core(PLAIN))
        # A POC without the delta, or a delta without the profile, is not the core.
        self.assertFalse(delta_core.has_core('strategy("POC Momentum")\npoc = ta.sma(close, 20)\nprofile_lookback = 50\n'))
        self.assertFalse(delta_core.has_core('strategy("Delta VWMA")\ndelta_pct = 10\nvwma = ta.vwma(close, 20)\n'))
        # The other naming the researchers' redesigns use.
        self.assertTrue(delta_core.has_core("poc_idx = 0\npoc_up_vol = 0.0\nprofile_lookback = input.int(100)\n"))

    def test_the_golden_script_has_the_core(self):
        from futuresfund.golden import active

        row = active()
        if not row:
            self.skipTest("no golden strategy is set on this desk")
        self.assertTrue(delta_core.has_core(row["pine"]))


class CoverageTests(unittest.TestCase):
    def setUp(self):
        from futuresfund import learn

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.learning, self.learned, self.created, self.notes = (root / name for name in ("learning", "learned", "created", "notes"))
        for folder in (self.learning, self.learned, self.created, self.notes):
            folder.mkdir()
        for patch in (
            mock.patch.object(learn, "LEARNING", self.learning),
            mock.patch.object(learn, "LEARNED", self.learned),
            mock.patch.object(learn, "CREATED", self.created),
            mock.patch.object(learn, "NOTES", self.notes),
            mock.patch("futuresfund.book.strategy_acceptance", return_value=dict(LIMITS)),
            mock.patch("futuresfund.learn.research_instruments", lambda: ["ES", "GC", "NQ", "MES"]),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        delta_core._COVERAGE.update({"at": 0.0, "found": {}})
        delta_core._rounds.clear()

    def _script(self, folder: Path, name: str, text: str, charts: dict) -> None:
        (folder / name).write_text(text, encoding="utf-8")
        instruments = {
            root: {"charts": {timeframe: {"attempts": len(rows), "trials": rows, "best": rows[0] if rows else {}} for timeframe, rows in frames.items()}}
            for root, frames in charts.items()
        }
        (self.notes / f"{Path(name).stem}.json").write_text(json.dumps({"instruments": instruments}), encoding="utf-8")

    def test_only_a_delta_volume_breakout_run_that_clears_the_acceptance_covers_an_instrument(self):
        self._script(self.learned, "dvb.pine", CORE, {"GC": {"5m": [PASSING]}, "ES": {"2m": [{**PASSING, "net_profit": 700, "spans": {}}]}})
        self._script(self.learning, "ema.pine", PLAIN, {"NQ": {"15m": [PASSING]}})
        found = delta_core.coverage(fresh=True)
        self.assertEqual(set(found), {"GC"})
        self.assertEqual((found["GC"]["script"], found["GC"]["timeframe"], found["GC"]["net_profit"]), ("dvb.pine", "5m", 9000))
        self.assertEqual(delta_core.uncovered(), ["ES", "NQ", "MES"])
        self.assertEqual(delta_core.uncovered_first(["ES", "GC", "NQ", "MES"]), ["ES", "NQ", "MES", "GC"])
        lines = delta_core.coverage_lines()
        self.assertIn("GC: dvb.pine on 5m, profit 9000", lines[1])
        self.assertEqual(lines[0], "ES: none yet.")

    def test_the_three_developers_start_on_different_instruments_and_take_turns(self):
        self._script(self.learned, "dvb.pine", CORE, {"GC": {"5m": [PASSING]}})
        delta_core.coverage(fresh=True)
        self.assertEqual([delta_core.next_target("2m") for _ in range(4)], ["ES", "NQ", "MES", "ES"])
        self.assertEqual(delta_core.next_target("5m"), "NQ")
        self.assertEqual(delta_core.next_target("15m"), "MES")

    def test_delta_volume_breakout_scripts_are_studied_first(self):
        from futuresfund import learn

        (self.learning / "aaa_ema.pine").write_text(PLAIN, encoding="utf-8")
        (self.learning / "zzz_dvb.pine").write_text(CORE, encoding="utf-8")
        (self.learning / "created_dvb.pine").write_text(CORE, encoding="utf-8")
        names = [path.name for path in learn._pine_files(self.learning, False)]
        self.assertEqual(names, ["zzz_dvb.pine", "created_dvb.pine", "aaa_ema.pine"])


class PromptTests(unittest.TestCase):
    def test_the_prompt_asks_for_the_core_on_an_instrument_that_needs_one(self):
        from futuresfund.strategy_team import prompt_for

        with mock.patch("futuresfund.delta_core.coverage", return_value={}):
            text = prompt_for([], chart="15-minute", target="YM")
        self.assertIn("the script is a delta volume breakout", text)
        self.assertIn("array.new_float rows and for loops", text)
        self.assertIn("the array rows and for loops of the volume profile", text)
        self.assertNotIn("no request.security, arrays", text)
        self.assertIn("write this script for YM on the 15-minute chart", text)
        self.assertIn("last year of the YM 15-minute chart", text)
        self.assertIn("unprovenStrategies", text)
        self.assertNotIn("full five-year download", text)


if __name__ == "__main__":
    unittest.main()
