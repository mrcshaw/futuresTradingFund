"""Micro Nasdaq is its own contract at two dollars a point."""

from __future__ import annotations

import unittest


class MnqTests(unittest.TestCase):
    def test_mnq_is_two_dollars_a_point_and_not_nasdaq(self):
        from futuresfund.contracts import POINT_VALUE
        from futuresfund.historical_bars import outright_files
        from futuresfund.learn import _with_contract_point, research_instruments
        from futuresfund.library import contract_of
        from futuresfund.risk_style import point_value

        self.assertEqual(POINT_VALUE["MNQ"], 2)
        self.assertEqual(point_value("MNQ"), 2.0)
        self.assertEqual(_with_contract_point({"point_value": 50, "length": 10}, "MNQ")["point_value"], 2.0)
        self.assertEqual(contract_of("Night breakout", "mnq_breakout.pine"), ("MNQ1!", "Micro Nasdaq"))
        self.assertEqual(contract_of("Night breakout", "nq_breakout.pine"), ("NQ1!", "Nasdaq"))
        self.assertEqual(contract_of("Desk", "desk.pine", "MNQ"), ("MNQ1!", "Micro Nasdaq"))
        self.assertIn("MNQ", research_instruments())
        names = [path.name for path in outright_files("MNQ")]
        self.assertTrue(names)
        self.assertTrue(any(name.endswith("ohlcv-1m.csv") for name in names))

    def test_the_creation_prompt_states_the_mnq_point_value(self):
        from pathlib import Path

        source = Path(__file__).resolve().parents[1].joinpath("futuresfund", "strategy_team.py").read_text(encoding="utf-8")
        self.assertIn("On MNQ the point value is 2.", source)
        page = Path(__file__).resolve().parents[1].joinpath("futuresfund", "static", "app.js").read_text(encoding="utf-8")
        self.assertIn('"MNQ1!"', page)
        from futuresfund.live_trading import CONTRACTS

        self.assertEqual(CONTRACTS["MNQ1!"], "MNQ")
        self.assertIn("On ES the point value is 50. On NQ it is 20. On GC it is 100.", source)
