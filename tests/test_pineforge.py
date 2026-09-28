import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from futuresfund.board import Board
from futuresfund.learn import _search, _widen
from futuresfund.pine_compat import prepare_for_pineforge
from futuresfund.pineforge_engine import bars_to_csv, metrics_from, parse_report
from futuresfund.strategy import _version_five


class PineCompatTests(unittest.TestCase):
    def test_a_changing_highest_length_uses_the_input_and_headquarters_stays_version_five(self):
        source = "\n".join([
            "//@version=5",
            'strategy("night")',
            'profile_lookback = input.int(60, "Profile Lookback")',
            "int lb = math.min(profile_lookback, bar_index)",
            "float prof_hi = ta.highest(high, lb > 0 ? lb : 1)",
            "float prof_lo = ta.lowest(low, lb > 0 ? lb : 1)",
        ])
        revised, notes = prepare_for_pineforge(source)
        self.assertIn("ta.highest(high, profile_lookback)", revised)
        self.assertIn("ta.lowest(low, profile_lookback)", revised)
        self.assertTrue(revised.startswith("//@version=6"))
        self.assertTrue(any("version 5" in note for note in notes))
        shown = _version_five(source, "// Headquarters.\n")
        self.assertTrue(shown.startswith("//@version=5\n"))
        self.assertIn("ta.highest(high, lb > 0 ? lb : 1)", shown)

    def test_a_default_outside_the_input_range_is_pulled_back_inside(self):
        from futuresfund.strategy import _legal_defaults
        from futuresfund.learn import _clamp_plan

        source = 'ema_backcandles = input.int(35, "EMA Back Candles", minval=2, maxval=20, group="EMA")'
        self.assertIn("input.int(20,", _legal_defaults(source))
        plan = _clamp_plan({"ema_backcandles": 35}, {"ema_backcandles": (2, 20)})
        self.assertEqual(plan["ema_backcandles"], 20)

    def test_array_methods_keep_the_same_calls(self):
        source = "//@version=6\narr = vol\nif arr.size() != rows\n    arr.clear()\n    arr.push(0.0)\n    vol_total.set(r, vol_total.get(r) + per_row)\n"
        revised, notes = prepare_for_pineforge(source)
        self.assertIn("array.size(arr)", revised)
        self.assertIn("array.clear(arr)", revised)
        self.assertIn("array.push(arr, 0.0)", revised)
        self.assertIn("array.set(vol_total, r, array.get(vol_total, r) + per_row)", revised)
        self.assertTrue(notes)

    def test_a_report_keeps_the_trade_count_profit_and_drawdown(self):
        report = parse_report('{"engine":"pineforge","summary":{"total_trades":281,"net_pnl":-21102.5,"max_drawdown":-22737.5}}')
        row = metrics_from(report)
        self.assertEqual(row["runner"], "pineforge")
        self.assertEqual(row["trades"], 281)
        self.assertEqual(row["net_profit"], -21102.5)
        self.assertEqual(row["max_drawdown"], 22737.5)

    def test_chart_times_are_eastern_and_volume_is_written(self):
        csv_text = bars_to_csv([
            {"t": "2026-09-01T09:30:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 0},
        ])
        stamp = int(datetime(2026, 9, 1, 9, 30, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
        self.assertIn(f"{stamp},1,2,0.5,1.5,0", csv_text)


class StopTests(unittest.TestCase):
    def test_stop_keeps_the_researchers_from_starting_another_attempt(self):
        board = Board()
        board.set_research("running")
        board.set_activity("Quantitative Researcher", "Studying a script")
        board.mark_stopped()
        board.set_activity("Quantitative Researcher", "Studying the next script")
        self.assertEqual(board.snapshot()["activity"]["task"], "Stopping the researchers.")
        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"stopped": True, "runner": "pineforge", "engine": "pineforge", "net_profit": None, "max_drawdown": None, "trades": 0}

        trials = _search({"length": 10}, execute, 50000, 5, board, None, None)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(trials, [])

    def test_a_pine_search_does_not_add_a_python_indicator(self):
        plan = _widen({"length": 10}, {"length": 10}, set(), 4, extras=False)
        self.assertNotIn("added_indicator", plan)
