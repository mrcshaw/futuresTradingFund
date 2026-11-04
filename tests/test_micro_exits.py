"""A micro contract's search starts at a $50 stop and a $100 take profit. A full contract still starts at $250 and $500."""

import unittest

from futuresfund import research_rules as rules


class MicroLadderTests(unittest.TestCase):
    def test_micro_roots(self):
        for root in ("MES", "MNQ", "MNQ1!", "CME_MINI:MES1!", "M2K", "MGC"):
            self.assertTrue(rules.is_micro(root), root)
        for root in ("ES", "NQ", "GC", "YM", ""):
            self.assertFalse(rules.is_micro(root), root)

    def test_a_micro_search_opens_at_50_and_100(self):
        from futuresfund.learn import _start_on_the_ladder

        with rules.chart_root("MES"):
            opened = _start_on_the_ladder({"stop_dollars": 250, "tp_dollars": 500, "length": 14})
            self.assertEqual(rules.stop_checkpoints("stop_dollars"), [50.0, 100.0, 150.0])
        self.assertEqual(opened, {"stop_dollars": 50, "tp_dollars": 100, "length": 14})

    def test_a_micro_stop_walks_50_100_150(self):
        from futuresfund.learn import _raise_stop_hundred

        with rules.chart_root("MNQ"):
            first = _raise_stop_hundred({"stop_dollars": 50, "tp_dollars": 100}, "stop_dollars", set(), 50_000.0)
            second = _raise_stop_hundred(first, "stop_dollars", set(), 50_000.0)
            top = _raise_stop_hundred(second, "stop_dollars", set(), 50_000.0)
        self.assertEqual(first, {"stop_dollars": 100, "tp_dollars": 150})
        self.assertEqual(second, {"stop_dollars": 150, "tp_dollars": 200})
        self.assertIsNone(top)

    def test_a_full_contract_is_unchanged(self):
        from futuresfund.learn import _start_on_the_ladder

        with rules.chart_root("ES"):
            opened = _start_on_the_ladder({"stop_dollars": 250, "tp_dollars": 500})
            self.assertEqual(rules.stop_checkpoints("stop_dollars"), [250.0, 500.0, 700.0])
        self.assertEqual(opened, {"stop_dollars": 250, "tp_dollars": 500})
        self.assertIn("$250", rules.ladder_text("ES"))
        self.assertIn("$50, $100, and $150", rules.ladder_text("MES"))

    def test_the_root_ends_with_the_block(self):
        with rules.chart_root("MES"):
            self.assertTrue(rules.is_micro())
        self.assertFalse(rules.is_micro())


class FloorTests(unittest.TestCase):
    def test_a_250_minval_does_not_push_a_micro_stop_back_up(self):
        from futuresfund.learn import _clamp_plan, _input_bounds

        facts = {"inputs": [
            {"name": "stop_dollars", "min": 250, "max": 700},
            {"name": "tp_dollars", "min": 250, "max": None},
            {"name": "length", "min": 5, "max": 50},
        ]}
        bounds = _input_bounds(facts)
        self.assertEqual(bounds["stop_dollars"], (50, 700))
        self.assertEqual(bounds["length"], (5, 50))
        self.assertEqual(_clamp_plan({"stop_dollars": 50, "tp_dollars": 100, "length": 2}, bounds), {"stop_dollars": 50, "tp_dollars": 100, "length": 5})

    def test_the_risk_block_minval_is_lowered_to_50(self):
        from futuresfund.strategy_team import open_exit_floor, prepare_backtest

        text = 'stop_dollars      = input.int(250, "Stop ($)", minval=250, maxval=700)\ntp_dollars = input.int(500, "Take Profit ($)", minval=250)\nlength = input.int(14, "Length", minval=250)\n'
        lowered = open_exit_floor(text)
        self.assertIn('input.int(250, "Stop ($)", minval=50, maxval=700)', lowered)
        self.assertIn('input.int(500, "Take Profit ($)", minval=50)', lowered)
        self.assertIn('"Length", minval=250', lowered)
        script = "\n".join([
            "//@version=5",
            'strategy("Micro", overlay=true, initial_capital=50000)',
            'pv = input.float(50, "Point Value ($)")',
            'stop_dollars = input.int(250, "Stop ($)", minval=250, maxval=700)',
            'tp_dollars = input.int(500, "Take Profit ($)", minval=250)',
            "if strategy.position_size != 0 and not na(stop_dollars)",
            "    if strategy.openprofit <= -stop_dollars",
            '        strategy.close_all(comment="Open P&L > SL Risk")',
            "",
        ])
        prepared = prepare_backtest(script, "MES")
        self.assertIn('minval=50, maxval=700', prepared)
        self.assertIn('pv = input.float(5, "Point Value ($)")', prepared)


if __name__ == "__main__":
    unittest.main()
