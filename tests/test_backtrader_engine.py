"""The Backtrader engine against the TradingView trade export."""

import unittest

from futuresfund.backtrader_engine import slot_for, verify
from futuresfund.board import Board
from futuresfund.learn import _clamp_plan, _search, _widen


class BacktraderEngineTests(unittest.TestCase):
    def test_poc_confluence_matches_the_tradingview_export(self):
        result = verify()
        self.assertEqual(result["expected"], 105)
        self.assertGreaterEqual(result["matched"], 92)

    def test_each_chart_has_its_own_engine(self):
        self.assertEqual(slot_for("2m"), 0)
        self.assertEqual(slot_for("5m"), 1)
        self.assertEqual(slot_for("15m"), 2)

    def test_a_default_outside_the_input_range_is_pulled_back_inside(self):
        from futuresfund.strategy import _legal_defaults

        source = 'ema_backcandles = input.int(35, "EMA Back Candles", minval=2, maxval=20, group="EMA")'
        self.assertIn("input.int(20,", _legal_defaults(source))
        plan = _clamp_plan({"ema_backcandles": 35}, {"ema_backcandles": (2, 20)})
        self.assertEqual(plan["ema_backcandles"], 20)

    def test_stop_keeps_the_researchers_from_starting_another_attempt(self):
        board = Board()
        board.set_research("running")
        board.set_activity("Chart researcher", "Studying a script")
        board.mark_stopped()
        board.set_activity("Chart researcher", "Studying the next script")
        self.assertEqual(board.snapshot()["activity"]["task"], "Stopping the researchers.")
        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"stopped": True, "runner": "backtrader", "engine": "backtrader", "net_profit": None, "max_drawdown": None, "trades": 0}

        trials = _search({"length": 10}, execute, 50000, 5, board, None, None)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(trials, [])

    def test_a_pine_search_does_not_add_a_python_indicator(self):
        plan = _widen({"length": 10}, {"length": 10}, set(), 4, extras=False)
        self.assertNotIn("added_indicator", plan)

    def test_one_finished_chart_does_not_count_as_the_whole_instrument(self):
        from futuresfund.learn import pending_for_chart, tested_instruments

        partial = {"instruments": {"ES": {"charts": {"2m": {"attempts": 1, "best": {"net_profit": 1}, "trials": [{}]}}}}}
        self.assertEqual(tested_instruments(partial), set())
        self.assertEqual(pending_for_chart(partial, "2m", ["ES"]), [])
        self.assertEqual(pending_for_chart(partial, "15m", ["ES"]), ["ES"])
