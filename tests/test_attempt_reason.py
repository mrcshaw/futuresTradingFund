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
        from futuresfund import book as bookmod
        from futuresfund.attempt_reason import _brief

        earlier = {"net_profit": 100.0, "max_drawdown": 50.0, "trades": 20, "win_rate": 50.0, "profit_factor": 1.2, "params": {"length": 20}}
        later = {"net_profit": -100.0, "max_drawdown": 80.0, "trades": 20, "win_rate": 30.0, "profit_factor": 0.8, "params": {"length": 14}}
        # The win rate is the scale goal in this book, whatever boxes the owner's book has checked.
        with tempfile.TemporaryDirectory() as tmp, patch.object(bookmod, "BOOK_PATH", Path(tmp) / "book.json"):
            bookmod.save_strategy_acceptance({
                "min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 60, "goal": "win_rate",
                "use_drawdown": True, "use_trades": True, "use_factor": True, "use_win_rate": True,
            })
            text = _brief("strategy(\"Sample\")", [earlier, later], {"length": 14})
            first = _brief("strategy(\"Sample\")", [earlier], {"length": 20})
        # The win rate fell from 50 to 30 and the profit factor from 1.2 to 0.8.
        self.assertIn("moved farther from the goal", text)
        self.assertIn("ta.ema", text)
        self.assertIn("Never write GOLD", text)
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
            with patch("futuresfund.learn.LEARNING", folder), patch("futuresfund.attempt_reason.reasoned_change", reasoned), patch(
                "futuresfund.risk_style.SUGGESTIONS_PATH", folder / "risk_suggestions.json"
            ), patch("futuresfund.errors.JOURNAL", folder / "errors.json"):
                trials = _search(
                    {"length": 10, "stop_dollars": 500.0},
                    execute,
                    50000,
                    2,
                    board,
                    script,
                    None,
                )
        self.assertEqual(len(trials), 2)
        # The search opens at the script's own stop, then raises it to the next step.
        self.assertEqual(calls[0]["stop_dollars"], 500.0)
        self.assertEqual(calls[1]["stop_dollars"], 700.0)
        self.assertEqual(calls[1]["length"], 10)
        self.assertTrue(any("stop rises through" in item["text"] for item in board.snapshot()["messages"]))


class ContextWindowTests(unittest.TestCase):
    def test_every_request_uses_the_loaded_window_so_the_model_is_not_reloaded(self):
        import json

        from futuresfund.llm import CONTEXT_CAP, _post

        sent = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"message": {"content": "ok"}}).encode()

        def fake_open(request, timeout=0):
            sent.append(json.loads(request.data.decode())["options"]["num_ctx"])
            return Response()

        with patch("urllib.request.urlopen", fake_open):
            for room in (4096, 8192, 12288, CONTEXT_CAP):
                _post("desk-researcher", [{"role": "user", "content": "ok?"}], num_ctx=room)
        self.assertEqual(sent, [CONTEXT_CAP] * 4)

    def test_the_decision_uses_the_thinking_pass(self):
        from futuresfund import attempt_reason, llm

        captured = {}

        def fake_parts(model, messages, **kwargs):
            captured.update(kwargs)
            return {"content": "BECAUSE: x\nSETTING: length = 12", "thinking": "", "done": "stop"}

        with patch.object(llm, "_request_parts", fake_parts):
            attempt_reason._ask("brief")
        self.assertTrue(captured["think"])


class DescriptionThreadTests(unittest.TestCase):
    """The Trading Analyst's description does not hold the chart's first backtest."""

    def test_the_backtest_does_not_wait_for_the_description(self):
        import threading
        import time

        from futuresfund.learn import describe_later

        release = threading.Event()

        def slow(text, board=None):
            release.wait(5)
            return "// How this script works: described\n" + text

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "sample.pine"
            script.write_text('//@version=5\nstrategy("Sample")\n', encoding="utf-8")
            with patch("futuresfund.script_brief.prepare_description", slow):
                started = time.monotonic()
                thread = describe_later(None, script, script.read_text(encoding="utf-8"))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertIsNone(describe_later(None, script, script.read_text(encoding="utf-8")))
                release.set()
                thread.join(5)
            self.assertTrue(script.read_text(encoding="utf-8").startswith("// How this script works"))

    def test_a_file_that_changed_keeps_its_new_text(self):
        import threading

        from futuresfund.learn import describe_later

        release = threading.Event()

        def slow(text, board=None):
            release.wait(5)
            return "// How this script works: described\n" + text

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "sample.pine"
            script.write_text('//@version=5\nstrategy("Sample")\n', encoding="utf-8")
            with patch("futuresfund.script_brief.prepare_description", slow):
                thread = describe_later(None, script, script.read_text(encoding="utf-8"))
                script.write_text('//@version=5\nstrategy("Repaired")\n', encoding="utf-8")
                release.set()
                thread.join(5)
            self.assertEqual(script.read_text(encoding="utf-8"), '//@version=5\nstrategy("Repaired")\n')
