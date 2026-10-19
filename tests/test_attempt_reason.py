"""The researcher chooses the next change from the measured attempt."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from futuresfund.attempt_reason import parse_reasoned_reply


class ReplyTests(unittest.TestCase):
    def test_a_reply_moves_one_input_and_keeps_the_reason(self):
        reply = "\n".join([
            "BECAUSE: Win rate is 40, under the line. A shorter length tightens the entry.",
            "SETTING: length = 14",
        ])
        found = parse_reasoned_reply(reply, {"length": 10, "stop_dollars": 1000.0}, {"length": (5, 40)})
        self.assertEqual(found["params"]["length"], 14)
        self.assertEqual(found["params"]["stop_dollars"], 1000.0)
        self.assertIn("Win rate is 40", found["because"])
        self.assertIsNone(found["script"])

    def test_a_reply_cannot_invent_an_input_or_pass_the_limit(self):
        current = {"length": 10, "pv": 20.0}
        self.assertIsNone(parse_reasoned_reply("BECAUSE: Try it.\nSETTING: mystery = 3", current, {}))
        self.assertIsNone(parse_reasoned_reply("BECAUSE: Raise the point value.\nSETTING: pv = 100", current, {}))
        self.assertIsNone(parse_reasoned_reply("BECAUSE: Too long.\nSETTING: length = 80", current, {"length": (5, 40)}))
        self.assertIsNone(parse_reasoned_reply("BECAUSE: Same length.\nSETTING: length = 10", current, {"length": (5, 40)}))

    def test_a_script_block_is_the_change_when_the_entry_is_the_miss(self):
        reply = "\n".join([
            "BECAUSE: The last run took no trades, so the entry did not pass.",
            "SCRIPT:",
            "//@version=5",
            'strategy("Next", overlay=true)',
            "if close > open",
            '    strategy.entry("L", strategy.long)',
            "END",
        ])
        found = parse_reasoned_reply(reply, {"length": 10}, {})
        self.assertIsNone(found["params"])
        self.assertIn("strategy.entry", found["script"])

    def test_a_library_indicator_is_added_and_a_new_input_is_refused(self):
        script = "\n".join([
            "//@version=5",
            'strategy("Sample", overlay=true)',
            'length = input.int(20, "Length", minval=1, maxval=100)',
            "if close > open",
            '    strategy.entry("Long", strategy.long)',
            '    strategy.exit("XL", "Long", stop=close - 1, limit=close + 2)',
            "if close < open",
            '    strategy.entry("Short", strategy.short)',
            '    strategy.exit("XS", "Short", stop=close + 1, limit=close - 2)',
            'before1645 = not na(time(timeframe.period, "1800-1645", "America/New_York"))',
            "if not before1645",
            '    strategy.close_all(comment="Session")',
        ])
        reply = "\n".join([
            "BECAUSE: The profit factor is under the line. An average from the library can filter the weak trades.",
            "INDICATOR: ta.ema 21",
        ])
        found = parse_reasoned_reply(reply, {"length": 20, "stop_dollars": 1000.0}, {}, script=script)
        self.assertIsNone(found["params"])
        self.assertIn("ta.ema(close, 21)", found["script"])
        self.assertIn("and _desk_long", found["script"])
        self.assertIn("and _desk_short", found["script"])
        self.assertNotIn("mystery", found["script"])
        invented = "\n".join([
            "BECAUSE: Add a filter.",
            "SCRIPT:",
            script,
            'mystery = input.int(3, "Mystery", minval=1, maxval=10)',
            "END",
        ])
        self.assertIsNone(parse_reasoned_reply(invented, {"length": 20}, {}, script=script))
        self.assertIsNone(parse_reasoned_reply("BECAUSE: Not in the library.\nINDICATOR: ta.madeup 10", {"length": 20}, {}, script=script))

    def test_the_scale_says_whether_the_last_change_moved_closer(self):
        from futuresfund.attempt_reason import _brief

        earlier = {"net_profit": 100.0, "max_drawdown": 50.0, "trades": 20, "win_rate": 50.0, "profit_factor": 1.2, "params": {"length": 20}}
        later = {"net_profit": -100.0, "max_drawdown": 80.0, "trades": 20, "win_rate": 30.0, "profit_factor": 0.8, "params": {"length": 14}}
        text = _brief("strategy(\"Sample\")", [earlier, later], {"length": 14})
        self.assertIn("left the scale flat", text)
        self.assertIn("ta.ema", text)
        self.assertIn("Never write GOLD", text)
        first = _brief("strategy(\"Sample\")", [earlier], {"length": 20})
        self.assertIn("has not moved yet", first)


class RootTests(unittest.TestCase):
    def test_gold_is_the_gc_root(self):
        from futuresfund.contracts import root_of

        self.assertEqual(root_of("GOLD"), "GC")
        self.assertEqual(root_of("GC1!"), "GC")
        self.assertEqual(root_of("ES"), "ES")


class SearchTests(unittest.TestCase):
    def test_the_search_uses_the_researchers_change_after_the_attempt(self):
        from futuresfund.board import Board
        from futuresfund.learn import _search

        calls = []

        def execute(params):
            calls.append(dict(params))
            return {
                "net_profit": -float(params["stop_dollars"]),
                "max_drawdown": 80.0,
                "trades": 20,
                "days": 20,
                "win_rate": 40.0,
                "profit_factor": 0.8,
            }

        def reasoned(script, trials, bounds, choices):
            raise AssertionError("the stop ladder runs before the researcher")

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            script = folder / "sample.pine"
            script.write_text('//@version=5\nstrategy("Sample")\n', encoding="utf-8")
            board = Board()
            with patch("futuresfund.learn.LEARNING", folder), patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                trials = _search(
                    {"length": 10, "stop_dollars": 1000.0},
                    execute,
                    50000,
                    2,
                    board,
                    script,
                    None,
                )
        self.assertEqual(len(trials), 2)
        self.assertEqual(calls[0]["stop_dollars"], 250.0)
        self.assertEqual(calls[1]["stop_dollars"], 500.0)
        self.assertEqual(calls[1]["length"], 10)
        self.assertTrue(any("stop rises through" in item["text"] for item in board.snapshot()["messages"]))
