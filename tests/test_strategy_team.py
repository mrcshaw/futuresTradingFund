"""The creation team turns measured changes into one new Pine script."""

import json
import tempfile
import unittest
from pathlib import Path

from futuresfund.strategy_team import (
    baseline_ready,
    build_cards,
    extract_pine,
    finished_chart_studies,
    measured_cards,
    pine_problems,
    script_name,
)


class StrategyTeamTests(unittest.TestCase):
    def test_a_measured_change_becomes_a_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learning"
            notes = root / "notes"
            learning.mkdir()
            notes.mkdir()
            (learning / "ema_pullback.pine").write_text("//@version=5\nstrategy(\"EMA\")\n", encoding="utf-8")
            (learning / "created_other.pine").write_text("//@version=5\n", encoding="utf-8")
            (notes / "ema_pullback.json").write_text(json.dumps({
                "title": "EMA Pullback",
                "instruments": {
                    "ES": {
                        "trials": [
                            {"change": "Original", "net_profit": 1, "max_drawdown": 2, "trades": 3},
                            {
                                "change": "cooldown_bars 5 to 10",
                                "impact": "cooldown_bars made the book steadier.",
                                "net_profit": 4,
                                "max_drawdown": 2,
                                "trades": 3,
                            },
                        ]
                    }
                },
            }), encoding="utf-8")
            cards = build_cards(learning, notes)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "EMA Pullback")
        self.assertEqual(measured_cards(cards)[0]["changes"][0]["change"], "cooldown_bars 5 to 10")

    def test_a_script_needs_a_version_line_an_entry_and_an_exit(self):
        self.assertEqual(pine_problems("strategy.entry(\"L\", strategy.long)"), [
            "The version line is missing.",
            "strategy() is missing.",
            "An exit is missing.",
        ])
        source = "//@version=5\nstrategy(\"Night\")\nstrategy.entry(\"L\", strategy.long)\nstrategy.close(\"L\")\n"
        self.assertEqual(pine_problems(source), [])
        reply = "From the EMA card.\n```pine\n" + source + "```"
        self.assertTrue(extract_pine(reply).startswith("//@version=5"))

    def test_a_first_pass_needs_a_trade_list_and_a_clean_compile(self):
        self.assertFalse(baseline_ready([{"fatal": True, "error": "compile", "trades": 0}]))
        self.assertFalse(baseline_ready([{"trades": 0, "net_profit": 0}]))
        self.assertTrue(baseline_ready([
            {"timeframe": "5m", "trades": 0, "net_profit": 0},
            {"timeframe": "2m", "trades": 4, "net_profit": 10},
        ]))

    def test_a_new_name_does_not_replace_an_existing_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "created_night.pine").write_text("//@version=5\n", encoding="utf-8")
            self.assertEqual(script_name("Night", folder), "created_night_2.pine")

    def test_a_script_is_ready_only_after_all_three_charts_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learning"
            notes = root / "notes"
            learning.mkdir()
            notes.mkdir()
            (learning / "day.pine").write_text("//@version=5\n", encoding="utf-8")
            (learning / "night.pine").write_text("//@version=5\n", encoding="utf-8")
            done = {"attempts": 1, "best": {"net_profit": 1}, "trials": [{}]}
            (notes / "day.json").write_text(json.dumps({
                "instruments": {"ES": {"charts": {"2m": done, "5m": done, "15m": done}}},
            }), encoding="utf-8")
            (notes / "night.json").write_text(json.dumps({
                "instruments": {"ES": {"charts": {"2m": done, "5m": done}}},
            }), encoding="utf-8")
            self.assertEqual(finished_chart_studies(learning, notes), ["day.pine"])

    def test_the_creation_search_stops_when_the_result_is_profitable(self):
        from futuresfund.learn import _search

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            profit = 25 if calls["n"] >= 3 else -10
            return {"net_profit": profit, "max_drawdown": 4, "trades": 2, "runner": "backtrader", "engine": "backtrader"}

        trials = _search({"length": 10}, execute, 50000, 20, None, None, None, until_profitable=True)
        self.assertGreaterEqual(calls["n"], 3)
        self.assertLess(len(trials), 20)
        self.assertGreater(trials[-1]["net_profit"], 0)
