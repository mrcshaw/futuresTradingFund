"""The desk refuses what the TradingView Pine editor refuses, and a model's rewrite cannot damage a script."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund.pine_check import rewrite_problem, tv_problems

ROOT = Path(__file__).resolve().parent.parent
HEAD = '//@version=5\nstrategy("S", overlay=true,\n  initial_capital=50000)\n'


class TradingViewRuleTests(unittest.TestCase):
    def test_a_plot_inside_a_block_is_refused(self):
        problems = tv_problems(HEAD + "x = close\nif bar_index > 5\n    plot(x)\n    plotshape(x > 1)\n")
        self.assertEqual(len(problems), 2)
        self.assertIn("Cannot use 'plot' in local scope", problems[0])

    def test_an_input_inside_a_block_is_refused(self):
        self.assertIn("input.int", tv_problems(HEAD + "if close > 1\n    n = input.int(3)\n")[0])

    def test_a_plot_at_the_margin_and_a_wrapped_call_are_fine(self):
        source = HEAD + 'x = close\nplot(x, "plot(inside text)")\n// plot(x)\nif x > 1\n    strategy.entry("L", strategy.long)\n'
        self.assertEqual(tv_problems(source), [])

    def test_a_name_declared_twice_in_one_block_is_refused(self):
        self.assertIn("already defined", tv_problems(HEAD + "a = 1\nb = a\na = 2\n")[0])

    def test_the_same_name_in_two_blocks_is_fine(self):
        source = HEAD + "if close > 1\n    float e = close\nelse\n    float e = open\nx = 1\nx := 2\n"
        self.assertEqual(tv_problems(source), [])

    def test_a_name_used_above_its_declaration_is_refused(self):
        problems = tv_problems(HEAD + "long_stop = close - stop_dollars\nstop_dollars = input.int(5)\n")
        self.assertIn("Undeclared identifier 'stop_dollars'", problems[0])

    def test_a_named_argument_is_not_a_use_of_a_later_name(self):
        source = HEAD + 'a = input.int(1, title="A")\ntitle = "x"\n'
        self.assertEqual(tv_problems(source), [])

    def test_scripts_that_compiled_on_tradingview_pass(self):
        scripts = sorted((ROOT / "futuresfund" / "checkScripts").glob("*.pine"))
        self.assertTrue(scripts)
        for path in scripts:
            self.assertEqual(tv_problems(path.read_text(encoding="utf-8")), [], path.name)


class RewriteTests(unittest.TestCase):
    ORIGINAL = HEAD + "\n".join(f"x{n} = {n}" for n in range(60)) + "\n"

    def test_a_rewrite_that_loses_the_logic_is_refused(self):
        self.assertIn("dropped", rewrite_problem(self.ORIGINAL, HEAD + "x = 1\n"))

    def test_a_rewrite_that_says_it_gave_up_is_refused(self):
        revised = self.ORIGINAL + "// the profile logic was truncated in the prompt\n"
        self.assertIn("truncated", rewrite_problem(self.ORIGINAL, revised))

    def test_a_small_change_is_fine(self):
        self.assertEqual(rewrite_problem(self.ORIGINAL, self.ORIGINAL + "y = 1\n"), "")

    def test_the_model_sees_the_whole_script(self):
        from futuresfund.attempt_reason import _excerpt

        script = "x" * 20000
        self.assertEqual(_excerpt(script), script)
        self.assertIn("Do not reply with SCRIPT", _excerpt("x" * 40000))

    def test_a_script_too_long_to_show_takes_no_rewrite(self):
        from futuresfund.attempt_reason import parse_reasoned_reply

        reply = "BECAUSE: no trades.\nSCRIPT:\n" + self.ORIGINAL + "END\n"
        self.assertIsNone(parse_reasoned_reply(reply, {}, script="x" * 40000))

    def test_a_rewrite_that_stops_the_engine_is_not_installed(self):
        from futuresfund import attempt_reason

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning" / "s.pine"
            path.parent.mkdir()
            path.write_text("original", encoding="utf-8")
            with mock.patch.object(attempt_reason, "accept_script", lambda source, original="": source), \
                    mock.patch("futuresfund.strategy_team.engine_problem", lambda source: "stopped"):
                self.assertIsNone(attempt_reason.install_script(path, "revised", None))
            self.assertEqual(path.read_text(encoding="utf-8"), "original")

    def test_a_rewrite_keeps_the_original_and_can_be_taken_back(self):
        from futuresfund import attempt_reason

        def execute(_plan):
            return {}

        execute.source = {"text": "original", "stamp": 0}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning" / "s.pine"
            path.parent.mkdir()
            path.write_text("original", encoding="utf-8")
            with mock.patch.object(attempt_reason, "accept_script", lambda source, original="": source), \
                    mock.patch("futuresfund.strategy_team.engine_problem", lambda source: ""):
                self.assertEqual(attempt_reason.install_script(path, "revised", execute), "revised")
            kept = Path(tmp) / "researchNotes" / "script_originals" / "s.pine"
            self.assertEqual(kept.read_text(encoding="utf-8"), "original")
            self.assertEqual(attempt_reason.undo_rewrite(path, execute), "original")
            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertEqual(execute.source["text"], "original")

    def test_the_repair_prompt_shows_the_script(self):
        from futuresfund import strategy_team

        prompts = []
        with mock.patch.object(strategy_team, "_ask", lambda board, agent, activity, prompt: prompts.append(prompt) or ""):
            strategy_team.repair_with_developer(mock.Mock(cancel=mock.Mock(is_set=lambda: False)), "dev", "THE SOURCE", "bad")
        self.assertIn("THE SOURCE", prompts[0])


class LibraryCopyTests(unittest.TestCase):
    def test_a_row_copies_the_script_that_was_measured_not_a_later_rewrite(self):
        from futuresfund import library
        from futuresfund.attempt_reason import keep_version

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learnedStrategies" / "s.pine"
            measured = '//@version=5\nstrategy("Measured")\nlength = input.int(10, "Length")\n'
            name = keep_version(path, measured)
            row = {"id": "s-1", "file": "s.pine", "contract": "ES1!", "source_sha": name, "params": {"length": 14}}
            with mock.patch.object(library, "_NOTES", Path(tmp) / "researchNotes"), \
                    mock.patch.object(library, "_cached_row", lambda strategy_id, contract: row), \
                    mock.patch("futuresfund.strategy.pine_text", lambda filename: "rewritten later"):
                pine = library.library_script("s-1")["pine"]
        self.assertIn('input.int(14, "Length")', pine)
        self.assertNotIn("rewritten later", pine)


class RiskBlockTests(unittest.TestCase):
    def test_the_exit_prices_move_below_the_inputs_they_read(self):
        from futuresfund.strategy_team import ensure_risk_management

        source = (
            HEAD + "long_stop = close - stop_dollars / pv\nlong_limit = close + tp_dollars / pv\n"
            "short_stop = close + stop_dollars / pv\nshort_limit = close - tp_dollars / pv\n"
            'pv = input.float(50, "PV")\nstop_dollars = input.int(750, "Stop ($)")\ntp_dollars = input.int(1250, "TP ($)")\n'
            "if strategy.position_size != 0 and not na(stop_dollars)\n    if strategy.openprofit <= -stop_dollars\n"
            '        strategy.close_all(comment="Open P&L > SL Risk")\n'
        )
        self.assertEqual(tv_problems(ensure_risk_management(source)), [])


class SearchTests(unittest.TestCase):
    def test_a_setting_that_stops_the_script_does_not_end_the_search(self):
        from futuresfund.learn import _search

        runs = []

        def execute(plan):
            runs.append(plan)
            if len(runs) == 2:
                return {"error": "The script stopped in PineTS: Index 51 is out of bounds", "fatal": True, "script_error": True, "net_profit": None, "max_drawdown": None, "trades": 0}
            return {"net_profit": 100.0 + len(runs), "max_drawdown": 50.0, "trades": 150, "win_rate": 50.0, "profit_factor": 1.2, "days": 260, "engine": "pinets"}

        trials = _search({"stop_dollars": 500, "ema_len": 20}, execute, 50000, 4, None, None, None)
        self.assertGreater(len(runs), 2)
        self.assertFalse(any(row.get("engine_fault") or row.get("unrunnable") for row in trials))


if __name__ == "__main__":
    unittest.main()
