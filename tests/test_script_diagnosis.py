"""The researcher reads what a script does before anyone tunes it."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import script_diagnosis as diagnosis

HEAD = '//@version=5\nstrategy("Sample", overlay=true)\npv = input.float(50, "Point")\n'
EXIT_BLOCK = (
    'stop_dollars = input.int(250, "Stop ($)")\n'
    'tp_dollars = input.int(500, "Take Profit ($)")\n'
    'use_trail = input.bool(false, "Enable Trailing Stop")\n'
    "long_limit = close + tp_dollars / pv\n"
    "if strategy.openprofit <= -stop_dollars\n"
    "    strategy.close_all()\n"
)


class UnreadInputTests(unittest.TestCase):
    def test_a_take_profit_that_feeds_only_an_unused_price_is_unread(self):
        script = HEAD + EXIT_BLOCK + 'i_tp = input.float(500, "Take Profit ($)")\ntp_pts = i_tp / pv\nif high >= close + tp_pts\n    strategy.close("Long")\n'
        self.assertEqual(diagnosis.unread_inputs(script), ["tp_dollars", "use_trail"])

    def test_a_price_passed_on_a_continued_line_counts_as_read(self):
        script = HEAD + EXIT_BLOCK.replace('use_trail = input.bool(false, "Enable Trailing Stop")\n', "") + (
            'strategy.exit("x", "Long",\n'
            "  limit=long_limit)\n"
        )
        self.assertEqual(diagnosis.unread_inputs(script), [])

    def test_a_comment_or_a_string_is_not_a_read(self):
        script = HEAD + 'length = input.int(14, "Length")\n// length matters\nplot(close, "length")\nstrategy.close_all(comment="pv")\n'
        self.assertEqual(diagnosis.unread_inputs(script), ["pv", "length"])

    def test_the_key_ignores_comments_and_the_contract_point_value(self):
        written = HEAD + 'x = input.int(1, "X")\n'
        other = written.replace("input.float(50,", "input.float(20,") + "// How this script works\n"
        self.assertEqual(diagnosis.script_key(written), diagnosis.script_key(other))
        self.assertNotEqual(diagnosis.script_key(written), diagnosis.script_key(written + "plot(x)\n"))


class ProbeTests(unittest.TestCase):
    def test_a_number_goes_to_both_ends_inside_its_limits(self):
        self.assertEqual(diagnosis.probes("stop_dollars", 500, (250, 750)), [250, 750])
        self.assertEqual(diagnosis.probes("length", 14), [4, 56])
        self.assertEqual(diagnosis.probes("stop_dollars", 250, (250, 750)), [750])

    def test_a_switch_a_choice_and_a_zero(self):
        self.assertEqual(diagnosis.probes("use_x", True), [False])
        self.assertEqual(diagnosis.probes("mode", "Profit", None, ["Profit", "Consistent"]), ["Consistent"])
        self.assertEqual(diagnosis.probes("max_stop", 0.0, (0, 500)), [500])

    def test_an_hour_stays_on_the_clock(self):
        self.assertEqual(diagnosis.probes("sess_end_hr", 16), [4, 23])


def _executor(rule):
    calls = []

    def execute(params):
        calls.append(dict(params))
        return rule(params)

    execute.source = {"text": "", "stamp": 0, "sessions": 5}
    execute.calls = calls
    return execute


class ResponseTests(unittest.TestCase):
    SCRIPT = HEAD + 'length = input.int(10, "Length")\nnoise = input.int(5, "Noise")\nplot(length + noise + pv)\n'

    def _flat_noise(self, params):
        return {"net_profit": 100.0 + params["length"], "max_drawdown": 50.0, "trades": 8, "exits": [{"exit": "SL", "trades": 8, "pnl": -1.0}]}

    def test_it_sorts_inputs_that_move_the_result_from_inputs_that_do_not(self):
        execute = _executor(self._flat_noise)
        record = diagnosis.measure_response(self.SCRIPT, {"length": 10, "noise": 5}, execute, ["length", "noise"])
        self.assertEqual(list(record["moved"]), ["length"])
        self.assertEqual(record["flat"], ["noise"])
        self.assertEqual(diagnosis.ignored_inputs(record), {"noise"})
        # One move is enough for length, and noise needed both ends. The stage window comes back.
        self.assertEqual(len(execute.calls), 1 + 1 + 2)
        self.assertEqual(execute.source["sessions"], 5)

    def test_it_uses_the_last_month_not_the_stage_window(self):
        seen = []
        execute = _executor(self._flat_noise)
        original = execute

        def spying(params):
            seen.append(original.source["sessions"])
            return original(params)

        spying.source = original.source
        diagnosis.measure_response(self.SCRIPT, {"length": 10, "noise": 5}, spying, ["length"])
        self.assertEqual(set(seen), {diagnosis.RESPONSE_SESSIONS})

    def test_a_script_with_no_trades_is_not_called_flat(self):
        execute = _executor(lambda params: {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0})
        record = diagnosis.measure_response(self.SCRIPT, {"length": 10, "noise": 5}, execute, ["length", "noise"])
        self.assertEqual(record["flat"], [])
        self.assertEqual(len(execute.calls), 1)
        self.assertIn("took no trades", "\n".join(diagnosis.response_lines(record)))

    def test_a_setting_that_stops_the_script_says_nothing_about_that_input(self):
        def rule(params):
            if params["noise"] != 5:
                return {"error": "bad value", "net_profit": None, "max_drawdown": None, "trades": 0}
            return self._flat_noise(params)

        record = diagnosis.measure_response(self.SCRIPT, {"length": 10, "noise": 5}, _executor(rule), ["noise"])
        self.assertEqual(record["flat"], [])

    def test_a_skip_leaves_the_test_unfinished_so_it_is_not_kept(self):
        calls = {"n": 0}

        def rule(params):
            calls["n"] += 1
            return {"skipped": True} if calls["n"] > 1 else self._flat_noise(params)

        record = diagnosis.measure_response(self.SCRIPT, {"length": 10, "noise": 5}, _executor(rule), ["length"])
        self.assertTrue(record["partial"])

    def test_the_run_limit_names_what_was_not_tested(self):
        record = diagnosis.measure_response(self.SCRIPT, {"length": 10, "noise": 5}, _executor(self._flat_noise), ["noise", "length"], limit=2)
        self.assertEqual(record["untested"], ["length"])

    def test_the_researcher_reads_the_findings(self):
        record = diagnosis.measure_response(self.SCRIPT.replace("length + noise + pv", "length + pv"), {"length": 10, "noise": 5}, _executor(self._flat_noise), ["length", "noise"])
        text = "\n".join(diagnosis.response_lines(record))
        self.assertIn("Nothing that reaches a trade reads these inputs: noise", text)
        self.assertIn("How those trades closed: SL 8 trades", text)

    def test_a_record_is_shared_by_every_copy_of_a_script(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            diagnosis.save(self.SCRIPT, {"flat": ["noise"]})
            self.assertEqual(diagnosis.load(self.SCRIPT + "// described\n")["flat"], ["noise"])
            self.assertEqual(diagnosis.load(self.SCRIPT + "plot(1)\n"), {})


class SharedLabelTests(unittest.TestCase):
    def test_two_inputs_with_one_label_each_keep_their_own_value(self):
        from futuresfund.backtrader_engine import input_names
        from futuresfund.learn import _input_overrides

        script = (
            'strategy("S")\n'
            'tp_dollars = input.int(500, "Take Profit ($)")\n'
            'i_tp = input.float(500.0, "Take Profit ($)")\n'
            'length = input.int(14, "Length")\n'
        )
        facts = {"inputs": [
            {"name": "tp_dollars", "label": "Take Profit ($)"},
            {"name": "i_tp", "label": "Take Profit ($)"},
            {"name": "length", "label": "Length"},
        ]}
        overrides = _input_overrides(facts, {"tp_dollars": 750, "i_tp": 2000.0, "length": 21})
        self.assertEqual(overrides, {"tp_dollars": 750, "i_tp": 2000.0, "Length": 21})
        self.assertEqual(input_names(script, overrides), {"tp_dollars": 750, "i_tp": 2000.0, "length": 21})


class ExitSummaryTests(unittest.TestCase):
    def test_trades_are_grouped_by_how_they_closed(self):
        rows = diagnosis.exit_summary([
            {"exit_signal": "SL", "pnl": -250.0, "favorable": 100.0, "adverse": -250.0},
            {"exit_signal": "SL", "pnl": -250.0, "favorable": 300.0, "adverse": -250.0},
            {"exit_signal": "TP", "pnl": 500.0, "favorable": 500.0, "adverse": -50.0},
        ])
        self.assertEqual(rows[0], {"exit": "SL", "trades": 2, "pnl": -500.0, "favorable": 200.0, "adverse": -250.0})
        self.assertIn("TP 1 trades, profit 500.0", diagnosis.exits_line(rows))


class RewriteHoldsTests(unittest.TestCase):
    BEFORE = {"net_profit": 500.0, "max_drawdown": 400.0, "trades": 40, "win_rate": 50.0, "profit_factor": 1.2}

    def _holds(self, after, repaired=()):
        from futuresfund.learn import rewrite_holds

        return rewrite_holds(self.BEFORE, {**self.BEFORE, **after}, list(repaired))

    def test_connecting_an_input_at_the_same_result_stays(self):
        self.assertTrue(self._holds({}, ["tp_dollars"]))

    def test_connecting_an_input_does_not_excuse_a_large_loss(self):
        self.assertFalse(self._holds({"net_profit": 100.0}, ["tp_dollars"]))

    def test_the_same_result_with_nothing_repaired_is_taken_back(self):
        self.assertFalse(self._holds({}))

    def test_starting_to_trade_at_a_loss_is_not_better(self):
        from futuresfund.learn import rewrite_holds

        silent = {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}
        losing = {"net_profit": -7211.0, "max_drawdown": 8680.5, "trades": 41, "win_rate": 40.0, "profit_factor": 0.5, "biggest_loser": -500.0}
        earning = {**losing, "net_profit": 300.0, "max_drawdown": 200.0, "profit_factor": 1.3}
        self.assertFalse(rewrite_holds(silent, losing, []))
        self.assertTrue(rewrite_holds(silent, earning, []))

    def test_making_a_broken_exit_work_stays_while_the_script_trades(self):
        from futuresfund.learn import rewrite_holds

        lower = {**self.BEFORE, "net_profit": -100.0}
        self.assertFalse(rewrite_holds(self.BEFORE, lower, ["tp_dollars"]))
        self.assertTrue(rewrite_holds(self.BEFORE, lower, ["tp_dollars"], True))
        self.assertFalse(rewrite_holds(self.BEFORE, {**lower, "trades": 0}, ["tp_dollars"], True))

    def test_a_rewrite_that_stops_trading_or_errors_is_taken_back(self):
        self.assertFalse(self._holds({"trades": 0, "net_profit": 0.0}, ["tp_dollars"]))
        self.assertFalse(self._holds({"error": "stopped"}, ["tp_dollars"]))


class DecisionPointTests(unittest.TestCase):
    """No engine waits for the researcher. The answer is used the next time a chart opens the script."""

    def _board(self):
        from futuresfund.board import Board

        return Board()

    def setUp(self):
        from futuresfund import learn

        patcher = mock.patch.object(learn, "_ensure_worker", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(learn.drain_decisions)

    def test_asking_returns_at_once_while_the_model_thinks(self):
        from futuresfund import learn

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            asked = []
            trials = [{"params": {"length": 10}}]
            self.assertTrue(learn.ask_researcher(self._board(), Path("s.pine"), "strategy()", trials, {}, "why", None, "Chart researcher", "read"))
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(learn.ask_researcher(self._board(), Path("s.pine"), "strategy()", trials, {}, "why", None, "Chart researcher", "read"))
            with mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: asked.append(1)):
                learn.drain_decisions()
            self.assertEqual(asked, [1])

    def test_an_answer_waits_for_the_next_chart_and_is_used_once(self):
        from futuresfund import learn
        from futuresfund.pine_report import pine_facts

        script = 'strategy("S")\nlength = input.int(10, "Length")\nplot(length)\n'
        answer = {"because": "The length is too short.", "params": {"length": 33}, "script": None}

        def execute(params):
            return {"net_profit": 1.0, "max_drawdown": 1.0, "trades": 5}

        execute.source = {"text": script, "stamp": 0, "sessions": None, "root": "ES", "timeframe": "2m"}
        board = self._board()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            settings, _facts = learn._read_before_testing(board, Path("s.pine"), execute, pine_facts(script), {"length": 10}, "Chart researcher", None)
            self.assertEqual(settings["length"], 10)
            with mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: answer):
                learn.drain_decisions()
            settings, _facts = learn._read_before_testing(board, Path("s.pine"), execute, pine_facts(script), {"length": 10}, "Chart researcher", None)
            self.assertEqual(settings["length"], 33)
            self.assertNotIn("answer", diagnosis.load(script))
            settings, _facts = learn._read_before_testing(board, Path("s.pine"), execute, pine_facts(script), {"length": 10}, "Chart researcher", None)
            self.assertEqual(settings["length"], 10)

    def test_settings_that_stop_moving_the_result_bring_the_researcher_in_once(self):
        from futuresfund import learn
        from futuresfund.learn import _search

        asked = []

        def reasoned(script, trials, bounds, choices, why, response, redesign):
            asked.append(why)
            self.assertTrue(redesign)
            return None

        def execute(params):
            return {"net_profit": -100.0, "max_drawdown": 80.0, "trades": 5, "days": 5}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp) / "d"):
            script = Path(tmp) / "s.pine"
            script.write_text('//@version=5\nstrategy("S")\n', encoding="utf-8")
            with mock.patch("futuresfund.learn.LEARNING", Path(tmp)), mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                _search({"length": 10, "width": 20}, execute, 50000, 12, self._board(), script, None, stage_days=7)
                learn.drain_decisions()
        self.assertEqual(len(asked), 1)
        self.assertIn("changed with no effect: length", asked[0])

    def test_the_search_leaves_measured_flat_inputs_alone(self):
        from futuresfund.learn import _search

        seen = []

        def execute(params):
            seen.append(dict(params))
            return {"net_profit": -float(params.get("length", 0)), "max_drawdown": 80.0, "trades": 5, "days": 5}

        execute.source = {"text": "", "stamp": 0, "sessions": 5, "flat": {"noise"}}
        with mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: None):
            _search({"length": 10, "noise": 5}, execute, 50000, 6, None, None, None)
        self.assertTrue(seen)
        self.assertTrue(all("noise" not in row for row in seen))

    def test_a_stage_is_reviewed_once_on_a_chart(self):
        from futuresfund import learn
        from futuresfund.learn import _review_failed_stage

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            asked = []

            def reasoned(*args):
                asked.append(1)
                return {"because": "It is sound.", "params": None, "script": None, "keep": True}

            def execute(params):
                return {}

            execute.source = {"text": "strategy()", "stamp": 0}
            trials = [{"params": {"length": 10}, "net_profit": -100.0, "max_drawdown": 80.0, "trades": 5}]
            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                for _ in range(2):
                    _review_failed_stage(self._board(), Path("s.pine"), trials, 7, execute, {}, "Chart researcher")
                    learn.drain_decisions()
        self.assertEqual(len(asked), 1)

    def test_a_rewrite_that_does_not_hold_is_taken_back(self):
        from futuresfund.learn import _install_checked

        board = self._board()
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "s.pine"
            script.write_text("original", encoding="utf-8")

            def execute(params):
                return {"net_profit": -900.0, "max_drawdown": 800.0, "trades": 5}

            execute.source = {"text": "original", "stamp": 0, "sessions": 5}

            def install(path, source, run):
                run.source.update(previous="original", text=source, stamp=1)
                path.write_text(source, encoding="utf-8")
                return source

            before = {"params": {"length": 10}, "net_profit": 100.0, "max_drawdown": 80.0, "trades": 5}
            with mock.patch("futuresfund.attempt_reason.install_script", install), mock.patch.object(diagnosis, "HOME", Path(tmp) / "d"):
                found = _install_checked(board, script, "worse", execute, before, "Chart researcher", None, "Try it.")
            self.assertIsNone(found)
            self.assertEqual(script.read_text(encoding="utf-8"), "original")
        self.assertTrue(any("taken back" in item["text"] for item in board.snapshot()["messages"]))

    def test_a_repair_that_connects_an_input_stays_and_the_input_returns_to_the_search(self):
        from futuresfund.learn import _install_checked

        board = self._board()
        revised = '//@version=5\nstrategy("S")\ntp_dollars = input.int(500, "Take Profit ($)")\nif high > close + tp_dollars\n    strategy.close_all()\n'
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp) / "d"):
            script = Path(tmp) / "s.pine"
            script.write_text("broken", encoding="utf-8")

            def execute(params):
                return {"net_profit": 100.0 + (params.get("tp_dollars", 500) - 500) / 10, "max_drawdown": 80.0, "trades": 5}

            execute.source = {"text": "broken", "stamp": 0, "sessions": 5, "flat": {"tp_dollars"}, "response": {"unread": ["tp_dollars"], "flat": []}}

            def install(path, source, run):
                run.source.update(previous="broken", text=source, stamp=1)
                path.write_text(source, encoding="utf-8")
                return source

            before = {"params": {}, "net_profit": 100.0, "max_drawdown": 80.0, "trades": 5}
            with mock.patch("futuresfund.attempt_reason.install_script", install):
                found = _install_checked(board, script, revised, execute, before, "Chart researcher", None, "The exit read another input.")
            self.assertEqual(found, revised)
            self.assertEqual(script.read_text(encoding="utf-8"), revised)
            self.assertNotIn("tp_dollars", execute.source["flat"])
            self.assertIn("tp_dollars", execute.source["response"]["moved"])
            self.assertEqual(diagnosis.load(revised)["read"], True)


class DecisionReplyTests(unittest.TestCase):
    MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    def _reply(self, *parts):
        from futuresfund import llm

        seen = []
        queue = list(parts)

        def fake(model, messages, **kwargs):
            seen.append({"messages": messages, **kwargs})
            return queue.pop(0)

        with mock.patch.object(llm, "_request_parts", fake):
            return llm.decision_reply("m", self.MESSAGES), seen

    def test_a_finished_reply_is_used(self):
        text, seen = self._reply({"content": "BECAUSE: x\nKEEP", "thinking": "t", "done": "stop"})
        self.assertEqual(text, "BECAUSE: x\nKEEP")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["think"])

    def test_notes_that_used_the_window_get_a_second_pass_and_are_never_the_reply(self):
        text, seen = self._reply(
            {"content": "", "thinking": "drafted BECAUSE: ...\nKEEP and found i_tp", "done": "length"},
            {"content": "BECAUSE: i_tp is the live target.\nKEEP", "thinking": "", "done": "stop"},
        )
        self.assertEqual(text, "BECAUSE: i_tp is the live target.\nKEEP")
        self.assertFalse(seen[1]["think"])
        self.assertIn("found i_tp", seen[1]["messages"][-1]["content"])

    def test_a_reply_cut_short_is_not_used(self):
        text, seen = self._reply(
            {"content": "BECAUSE: half a", "thinking": "n", "done": "length"},
            {"content": "still cut", "thinking": "", "done": "length"},
        )
        self.assertEqual(text, "")
        self.assertEqual(len(seen), 2)

    def test_a_model_that_cannot_answer_gives_nothing(self):
        text, _seen = self._reply({"error": "The meeting model could not answer: down"})
        self.assertEqual(text, "")


class LabelTests(unittest.TestCase):
    def test_a_dropdown_label_is_its_title_not_its_default(self):
        from futuresfund.pine_report import pine_facts

        facts = pine_facts(
            'strategy("S")\n'
            'mode = input.string("Off", "POC Filter Mode", options=["Off", "Reject"])\n'
            'length = input.int(14, "Length")\n'
            'other = input.string("A", title="Other", options=["A", "B"])\n'
        )
        self.assertEqual([item["label"] for item in facts["inputs"]], ["POC Filter Mode", "Length", "Other"])

    def test_a_moved_dropdown_reaches_the_script(self):
        from futuresfund.backtrader_engine import input_names
        from futuresfund.learn import _input_overrides
        from futuresfund.pine_report import pine_facts

        script = 'strategy("S")\nmode = input.string("Off", "POC Filter Mode", options=["Off", "Reject"])\n'
        overrides = _input_overrides(pine_facts(script), {"mode": "Reject"})
        self.assertEqual(input_names(script, overrides), {"mode": "Reject"})


class EmptyChartTests(unittest.TestCase):
    """A chart with no trades cannot show what a repair does, so it does not use up the script's one read."""

    SCRIPT = 'strategy("S")\nlength = input.int(10, "Length")\nif close > ta.sma(close, length)\n    strategy.entry("L", strategy.long)\n'

    def _read(self, chart, trades, asked, answer=None):
        from futuresfund import learn
        from futuresfund.board import Board
        from futuresfund.learn import _read_before_testing
        from futuresfund.pine_report import pine_facts

        root, timeframe = chart.split()

        def execute(params):
            profit = float(params["length"]) if trades else 0.0
            return {"net_profit": profit, "max_drawdown": 1.0 if trades else 0.0, "trades": trades}

        execute.source = {"text": self.SCRIPT, "stamp": 0, "sessions": None, "root": root, "timeframe": timeframe}

        def reasoned(script, rows, bounds, choices, why, response, redesign):
            asked.append(why)
            self.assertFalse(redesign)
            return answer

        with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned), mock.patch.object(learn, "_ensure_worker", lambda: None):
            _read_before_testing(Board(), Path("s.pine"), execute, pine_facts(self.SCRIPT), {"length": 10}, "Chart researcher", None)
            learn.drain_decisions()
        return execute

    def test_a_chart_with_no_trades_waits_for_one_that_trades(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            asked = []
            self._read("NQ 2m", 0, asked)
            self.assertEqual(asked, [])
            self.assertEqual(diagnosis.load(self.SCRIPT)["empty_charts"], ["NQ 2m"])
            self._read("ES 2m", 40, asked)
            self.assertEqual(len(asked), 1)
            self.assertIn("before its first backtest", asked[0])
            record = diagnosis.load(self.SCRIPT)
            self.assertEqual(record["baseline"]["trades"], 40)
            self.assertEqual(list(record["moved"]), ["length"])

    def test_a_chart_does_not_wait_while_another_chart_reads_the_script(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            asked = []
            lock = diagnosis.turn(self.SCRIPT)
            lock.acquire()
            try:
                started = time.monotonic()
                execute = self._read("NQ 2m", 40, asked)
                self.assertLess(time.monotonic() - started, 2)
            finally:
                lock.release()
            self.assertEqual(asked, [])
            self.assertEqual(execute.source["flat"], set())
            self.assertEqual(diagnosis.load(self.SCRIPT), {})

    def test_a_chart_that_trades_settles_it_for_every_later_chart(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            asked = []
            self._read("ES 2m", 40, asked, {"because": "It is sound.", "params": None, "script": None, "keep": True})
            self._read("NQ 2m", 0, asked)
            self._read("GC 2m", 40, asked)
            self.assertEqual(len(asked), 1)
            self.assertEqual(diagnosis.load(self.SCRIPT)["baseline"]["trades"], 40)

    def test_an_earlier_read_of_a_chart_with_no_trades_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            diagnosis.save(self.SCRIPT, {"baseline": {"params": {"length": 10}, "trades": 0}, "unread": [], "flat": [], "moved": {}, "read": True, "asks": 1})
            asked = []
            self._read("ES 2m", 40, asked)
            self.assertEqual(len(asked), 1)

    def test_no_trades_anywhere_asks_why_the_entry_never_passes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            asked = []
            for chart in ("ES 2m", "NQ 2m"):
                self._read(chart, 0, asked)
            self.assertEqual(asked, [])
            self._read("GC 2m", 0, asked)
            self.assertEqual(len(asked), 1)
            self.assertIn("no trades in the last month on 3 charts", asked[0])
            self._read("GC 2m", 0, asked)
            self.assertEqual(len(asked), 2)
            self._read("ES 2m", 0, asked)
            self.assertEqual(len(asked), 2)


class EmptyStageTests(unittest.TestCase):
    def test_a_stage_with_no_trades_does_not_use_up_the_review(self):
        from futuresfund import learn
        from futuresfund.board import Board
        from futuresfund.learn import _review_failed_stage

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)), mock.patch.object(learn, "_ensure_worker", lambda: None):
            asked = []

            def execute(params):
                return {}

            execute.source = {"text": "strategy()", "stamp": 0}
            trials = [{"params": {"length": 10}, "net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}]
            with mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: asked.append(1)):
                _review_failed_stage(Board(), Path("s.pine"), trials, 7, execute, {}, "Chart researcher")
                learn.drain_decisions()
            self.assertEqual(asked, [])
            self.assertFalse(diagnosis.load("strategy()").get("reviewed"))

    def test_runs_with_no_trades_do_not_bring_the_researcher_in(self):
        from futuresfund.board import Board
        from futuresfund.learn import _search

        asked = []

        def execute(params):
            return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0, "days": 5}

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "s.pine"
            script.write_text('//@version=5\nstrategy("S")\n', encoding="utf-8")
            with mock.patch("futuresfund.learn.LEARNING", Path(tmp)), mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: asked.append(1)), \
                    mock.patch.object(diagnosis, "HOME", Path(tmp) / "d"):
                _search({"length": 10, "width": 20}, execute, 50000, 8, Board(), script, None, stage_days=7)
                from futuresfund import learn

                learn.drain_decisions()
        self.assertEqual(asked, [])


class PriceJumpTests(unittest.TestCase):
    """The Aug 10 2026 NQZ2026 trade: no trades for 41 minutes, then the first price was 46 points past the stop."""

    def _bars(self):
        from datetime import datetime

        from futuresfund.backtrader_engine import ET, Bar

        start = int(datetime(2026, 8, 10, 6, 30, tzinfo=ET).timestamp())
        return [
            Bar(start, 30240.0, 30240.0, 30233.75, 30233.75, 5),
            Bar(start + 2400, 30164.5, 30178.0, 30164.5, 30175.25, 5),
            Bar(start + 2700, 30175.25, 30176.0, 30120.0, 30125.0, 5),
        ]

    def _trade(self, exit_time, exit_price, pnl):
        return {"side": "long", "exit_time": exit_time, "exit_price": exit_price, "pnl": pnl, "size": 1}

    def test_a_fill_at_the_open_after_a_jump_is_marked(self):
        from futuresfund.tv_broker import mark_price_jumps, tv_metrics

        trade = self._trade("2026-08-10 07:10", 30164.25, -1676.0)
        mark_price_jumps([trade], self._bars(), 20.0, 0.25)
        self.assertEqual(trade["jump"], 1385.0)
        numbers = tv_metrics([trade])
        self.assertEqual(numbers["biggest_loser"], -1676.0)
        self.assertEqual(numbers["held_loser"], -291.0)
        self.assertEqual(numbers["net_profit"], -1676.0)

    def test_a_stop_filled_inside_a_bar_is_not_marked(self):
        from futuresfund.tv_broker import mark_price_jumps

        trade = self._trade("2026-08-10 07:15", 30150.0, -1676.0)
        mark_price_jumps([trade], self._bars(), 20.0, 0.25)
        self.assertNotIn("jump", trade)

    def test_a_jump_does_not_fail_the_loss_rule_or_the_stop_check(self):
        from futuresfund.objective import account_trade_loss_cap, trade_loss_ok
        from futuresfund.script_diagnosis import exit_faults

        huge = -(account_trade_loss_cap() + 500)
        self.assertFalse(trade_loss_ok({"biggest_loser": huge}))
        self.assertTrue(trade_loss_ok({"biggest_loser": huge, "held_loser": -736.0}))
        script = 'stop_dollars = input.int(736, "Stop")\nif strategy.position_size > 0\n    strategy.exit("X", stop=close - stop_dollars)\n'
        base = {"params": {"stop_dollars": 736}, "biggest_loser": -1676.0}
        self.assertTrue(exit_faults(script, {"baseline": base}))
        self.assertFalse(exit_faults(script, {"baseline": {**base, "held_loser": -761.0}}))


class OneAcceptanceCheckTests(unittest.TestCase):
    LIMITS = {
        "min_profit": 6000.0, "profit_factor": 1.1, "win_rate": 40.0, "min_trades": 100, "drawdown_pct": 1.5,
        "use_drawdown": True, "use_trades": True, "use_profit": True, "use_factor": True, "use_win_rate": True,
    }
    # The Micro Nasdaq 15-minute run that reached the Not tested table with these numbers.
    FAILED = {
        "net_profit": 7376.0, "max_drawdown": 14036.0, "trades": 296, "win_rate": 52.36, "profit_factor": 1.112,
        "profit_ratio": 1.011, "biggest_loser": -700.0, "days": 260,
    }
    PASSED = {**FAILED, "net_profit": 50000.0, "max_drawdown": 12000.0, "profit_factor": 1.5, "profit_ratio": 1.3}

    def setUp(self):
        patcher = mock.patch("futuresfund.book.strategy_acceptance", lambda *args: dict(self.LIMITS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_tables_read_the_same_answer(self):
        from futuresfund import library
        from futuresfund.objective import acceptance_misses

        self.assertEqual(set(acceptance_misses(self.FAILED)), {"drawdown", "under_drawdown"})
        self.assertFalse(library.library_ready(self.FAILED))
        self.assertFalse(library._accepted_untested(self.FAILED))
        self.assertTrue(library.library_ready(self.PASSED))
        self.assertTrue(library._accepted_untested(self.PASSED))

    def test_the_profit_must_beat_the_drawdown_with_every_box_off(self):
        from futuresfund.objective import acceptance_misses

        off = {key: value for key, value in self.LIMITS.items() if not key.startswith("use_")}
        row = {"net_profit": 500.0, "max_drawdown": 700.0, "trades": 10, "profit_ratio": 1.3}
        self.assertIn("under_drawdown", acceptance_misses(row, limits=off))
        self.assertNotIn("under_drawdown", acceptance_misses({**row, "net_profit": 800.0}, limits=off))

    def test_a_stage_leaves_out_what_a_few_trades_cannot_show(self):
        from futuresfund.objective import stage_passed

        week = {"net_profit": 300.0, "max_drawdown": 700.0, "trades": 5, "win_rate": 50.0, "profit_factor": 1.2, "profit_ratio": 0.9}
        self.assertTrue(stage_passed(week, 7))

    def test_a_run_that_passes_replaces_a_richer_run_that_fails(self):
        from futuresfund import library

        rule = {**self.PASSED, "title": "A", "script": "A", "contract": "NQ1!", "timeframe": "5m", "net_profit": 8000.0, "max_drawdown": 700.0}
        broken = {**rule, "net_profit": 90000.0, "max_drawdown": 80000.0}
        self.assertEqual([row["net_profit"] for row in library._pick_best([broken, rule])], [8000.0])
        self.assertEqual([row["net_profit"] for row in library._pick_best([rule, broken])], [8000.0])

    def test_a_row_filed_before_the_check_stays_and_says_what_it_fails(self):
        from futuresfund import library

        shown = library._with_verdict([self.FAILED, self.PASSED, {"kind": "Added"}])
        self.assertIn("drawdown", shown[0]["fails"])
        self.assertEqual(shown[1]["fails"], "")
        self.assertEqual(shown[2]["fails"], "")


class ScriptFaultTests(unittest.TestCase):
    SCRIPT = 'strategy("S")\nstop_dollars = input.int(750, "Stop")\ntp_dollars = input.int(500, "TP")\nplot(stop_dollars + tp_dollars)\n'

    def _record(self, flat=(), **base):
        params = {"stop_dollars": 750, "tp_dollars": 500, "use_trail": False}
        return {"flat": list(flat), "baseline": {"params": params, "trades": 40, **base}}

    def test_a_take_profit_that_changes_no_trade_is_a_fault(self):
        found = diagnosis.exit_faults(self.SCRIPT, self._record(["tp_dollars"]))
        self.assertEqual(len(found), 1)
        self.assertIn("tp_dollars", found[0])

    def test_a_loss_far_past_the_stop_is_a_fault(self):
        self.assertEqual(diagnosis.exit_faults(self.SCRIPT, self._record(biggest_loser=-826.0)), [])
        self.assertIn("stop_dollars", diagnosis.exit_faults(self.SCRIPT, self._record(biggest_loser=-1576.0))[0])

    def test_a_win_far_past_the_take_profit_is_not_a_fault(self):
        self.assertEqual(diagnosis.exit_faults(self.SCRIPT, self._record(biggest_winner=3072.5)), [])

    def test_the_stop_holds_at_entry_and_the_take_profit_stays_on_the_close(self):
        from futuresfund.strategy_team import _apply_shared_exit

        script = (
            'strategy("S")\npv = input.float(20, "PV")\nstop_dollars = input.int(500, "Stop")\ntp_dollars = input.int(750, "TP")\n'
            'long_stop = close - stop_dollars / pv\nlong_limit = close + tp_dollars / pv\n'
            'short_stop = close + stop_dollars / pv\nshort_limit = close - tp_dollars / pv\n'
            'if long_sig\n    strategy.entry("Long", strategy.long)\n'
            'if strategy.position_size > 0\n    strategy.exit("XL", "Long", stop=long_stop, limit=long_limit)\n'
        )
        fixed = _apply_shared_exit(script)
        self.assertIn("long_stop = strategy.position_size > 0 ? strategy.position_avg_price - stop_dollars / pv : close - stop_dollars / pv", fixed)
        self.assertIn("short_stop = strategy.position_size < 0 ? strategy.position_avg_price + stop_dollars / pv : close + stop_dollars / pv", fixed)
        self.assertIn("long_limit = close + tp_dollars / pv", fixed)
        self.assertIn("short_limit = close - tp_dollars / pv", fixed)
        self.assertIn('strategy.entry("Long", strategy.long)\n    strategy.exit("XL", "Long", stop=long_stop, limit=long_limit)\n', fixed)
        self.assertEqual(_apply_shared_exit(fixed), fixed)

    def test_an_exit_only_in_the_trail_block_does_not_become_a_take_profit(self):
        from futuresfund.strategy_team import _exit_on_entry_bar

        script = (
            'if long_sig\n    strategy.entry("Long", strategy.long)\n'
            'if use_trail and strategy.position_size != 0\n    if strategy.position_size > 0\n        strategy.exit("XL", "Long", stop=long_stop, limit=long_limit)\n'
        )
        self.assertEqual(_exit_on_entry_bar(script), script)

    def test_an_unread_filter_is_a_fault_and_an_unread_trail_is_not(self):
        script = 'strategy("S")\nmin_delta = input.float(5, "Delta")\ntrail_pct = input.float(5, "Trail")\nplot(close)\n'
        found = diagnosis.input_faults(script)
        self.assertEqual(len(found), 1)
        self.assertIn("min_delta", found[0])
        self.assertNotIn("trail_pct", found[0])


class RepairTests(unittest.TestCase):
    SCRIPT = ScriptFaultTests.SCRIPT

    def setUp(self):
        from futuresfund import learn

        patcher = mock.patch.object(learn, "_ensure_worker", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(learn.drain_decisions)

    def _board(self):
        from futuresfund.board import Board

        return Board()

    def _read(self, record):
        from futuresfund import learn

        def execute(params):
            return {}

        execute.source = {"text": self.SCRIPT, "root": "NQ", "timeframe": "15m"}
        diagnosis.save(self.SCRIPT, record)
        asked = []
        with mock.patch.object(learn, "ask_researcher", lambda *args, **kwargs: asked.append((args[8], kwargs.get("urgent")))):
            learn._read_before_testing(self._board(), Path("s.txt"), execute, {}, {}, "Chart researcher", None)
        return asked

    def test_a_script_already_read_is_sent_back_while_an_exit_is_broken(self):
        record = {"read": True, "flat": ["tp_dollars"], "baseline": {"params": {"tp_dollars": 500}, "trades": 40}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            self.assertEqual(self._read(record), [("repair", True)])

    def test_the_researcher_stops_after_the_last_try_and_the_script_keeps_running(self):
        from futuresfund.learn import REPAIR_TRIES

        record = {"read": True, "repairs": REPAIR_TRIES, "flat": ["tp_dollars"], "baseline": {"params": {"tp_dollars": 500}, "trades": 40}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)):
            self.assertEqual(self._read(record), [])

    def test_a_broken_exit_goes_ahead_of_other_questions(self):
        from futuresfund import learn

        order = []
        trials = [{"params": {}}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)), \
                mock.patch.object(learn, "_decide", lambda job: order.append(job["kind"])):
            learn.ask_researcher(self._board(), Path("a.txt"), "strategy(1)", trials, {}, "why", None, "R", "read")
            learn.ask_researcher(self._board(), Path("b.txt"), "strategy(2)", trials, {}, "why", None, "R", "repair", urgent=True)
            learn.drain_decisions()
        self.assertEqual(order, ["repair", "read"])

    def test_a_stall_is_only_marked_once_the_researcher_ran(self):
        from futuresfund import learn

        board = self._board()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diagnosis, "HOME", Path(tmp)), \
                mock.patch.object(learn, "_reasoned_next", lambda *args: None):
            learn._ask_about_stall(board, Path("s.txt"), self.SCRIPT, [{"params": {}}], {}, "R", None, "why")
            # A restart here loses the queue, so nothing may say the question was answered.
            self.assertFalse(diagnosis.load(self.SCRIPT).get("stalled"))
            learn.drain_decisions()
            self.assertTrue(diagnosis.load(self.SCRIPT).get("stalled"))

    def test_a_finished_script_runs_again_for_an_answer_and_keeps_its_old_results(self):
        from futuresfund import learn

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(learn, "NOTES", Path(tmp)):
            notes = Path(tmp) / "s.json"
            trials = [{"net_profit": 5.0, "trades": 9, "params": {"a": 1}}]
            notes.write_text(json.dumps({"file": "s.txt", "instruments": {"ES": {"charts": {"2m": {"trials": trials, "best": trials[0]}}}}}), encoding="utf-8")
            board = self._board()
            self.assertTrue(learn._reopen_for_answer(board, Path(tmp) / "s.txt", "R"))
            fresh = json.loads(notes.read_text(encoding="utf-8"))
            self.assertEqual(fresh["reopened"], 1)
            self.assertNotIn("instruments", fresh)
            self.assertIn("instruments", json.loads((Path(tmp) / "before_repair" / "s_1.json").read_text(encoding="utf-8")))
            self.assertEqual(learn.pending_for_chart(fresh, "2m", ["ES"]), ["ES"])

            notes.write_text(json.dumps({"instruments": {"ES": {}}, "reopened": learn.REOPEN_LIMIT}), encoding="utf-8")
            self.assertFalse(learn._reopen_for_answer(board, Path(tmp) / "s.txt", "R"))
            self.assertIn("instruments", json.loads(notes.read_text(encoding="utf-8")))


class LibraryKeepsItsRowsTests(unittest.TestCase):
    def test_a_row_stays_when_its_script_leaves_the_folders(self):
        from futuresfund import library

        with mock.patch.object(library, "_measured_script", lambda name: "strategy()" if name == "abc" else ""), \
                mock.patch("futuresfund.strategy.pine_text", lambda name: ""):
            self.assertTrue(library._has_script({"file": "gone.pine", "source_sha": "abc"}))
            self.assertFalse(library._has_script({"file": "gone.pine", "source_sha": ""}))


class ModelPriorityTests(unittest.TestCase):
    def test_other_thinking_passes_wait_for_a_researcher_decision(self):
        from futuresfund import llm

        order = []
        inside = threading.Event()
        finish = threading.Event()

        def fake_request(model, messages, **kwargs):
            if model == "researcher":
                inside.set()
                finish.wait(5)
            order.append(model)
            return "ok"

        with mock.patch.object(llm, "_request", fake_request):
            first = threading.Thread(target=llm._post, args=("researcher", []), kwargs={"think": True, "urgent": True})
            first.start()
            inside.wait(5)
            second = threading.Thread(target=llm._post, args=("developer", []), kwargs={"think": True})
            second.start()
            time.sleep(0.1)
            self.assertEqual(order, [])
            finish.set()
            first.join(5)
            second.join(5)
        self.assertEqual(order, ["researcher", "developer"])

    def test_a_desk_chat_does_not_wait(self):
        from futuresfund import llm

        with mock.patch.object(llm, "_request", lambda *args, **kwargs: "ok"), mock.patch.object(llm, "_urgent_waiting", 1):
            self.assertEqual(llm._post("chat", []), "ok")


class BriefTests(unittest.TestCase):
    def test_the_brief_carries_the_reason_the_findings_and_the_current_rules(self):
        from futuresfund.attempt_reason import _ask, _brief

        row = {"params": {"length": 10}, "net_profit": -100.0, "max_drawdown": 80.0, "trades": 5, "exits": [{"exit": "SL", "trades": 5, "pnl": -100.0}]}
        record = {"sessions": 21, "baseline": row, "unread": ["tp_dollars"], "flat": [], "moved": {}}
        text = _brief('strategy("S")', [row], {"length": 10}, "This script failed the 7-day stage.", record)
        self.assertIn("This script failed the 7-day stage.", text)
        self.assertIn("reads these inputs: tp_dollars", text)
        self.assertIn("Closed by: SL 5 trades", text)
        self.assertIn("The stop starts at $250", text)
        self.assertNotIn("$2,000", text)
        self.assertIn("KEEP", text)
        sent = {}

        def fake_reply(model, messages, **kwargs):
            sent.update(system=messages[0]["content"])
            return ""

        with mock.patch("futuresfund.llm.decision_reply", fake_reply):
            _ask("brief")
        self.assertIn("You do not tune blindly", sent["system"])
        self.assertNotIn("$50 ", sent["system"])

    def test_a_rewrite_is_written_in_a_second_pass(self):
        from futuresfund import attempt_reason

        script = 'strategy("S")\ni_tp = input.float(500, "Target")\nplot(i_tp)\n'
        revised = 'strategy("S")\nplot(close)\n'
        row = {"params": {"i_tp": 500.0}, "net_profit": 1.0, "max_drawdown": 1.0, "trades": 1}
        calls = {}

        def second(model, messages, **kwargs):
            calls.update(prompt=messages[-1]["content"], **kwargs)
            return f"SCRIPT:\n{revised}\nEND"

        with mock.patch.object(attempt_reason, "_ask", lambda prompt: "BECAUSE: The exit reads i_tp.\nREWRITE:\nMake tp_pts read tp_dollars.\nEND"), \
                mock.patch("futuresfund.llm._post", second), \
                mock.patch("futuresfund.script_diagnosis.note_decision"):
            found = attempt_reason.reasoned_change(script, [row])
        self.assertEqual(found["script"].strip(), revised.strip())
        self.assertIn("Make tp_pts read tp_dollars.", calls["prompt"])
        self.assertFalse(calls["think"])
        self.assertTrue(calls["urgent"])

    def test_a_rewrite_answers_only_for_problems_it_brought_in(self):
        from futuresfund.attempt_reason import accept_script, script_refusal

        # This script closes with strategy.close, and the desk's stop check wants strategy.exit.
        original = 'strategy("S")\nif close > open\n    strategy.entry("L", strategy.long)\nif close < open\n    strategy.close("L")\n'
        revised = original.replace("close < open", "close < low")
        self.assertIn("protective stop", script_refusal(revised))
        self.assertEqual(script_refusal(revised, original), "")
        self.assertIsNotNone(accept_script(revised, original))

    def test_a_refused_script_gets_one_more_pass_with_the_reason(self):
        from futuresfund import attempt_reason

        script = 'strategy("S")\nlength = input.int(14, "Length")\nplot(length)\n'
        good = 'strategy("S")\nlength = input.int(14, "Length")\nplot(close + length)\n'
        bad = good + 'extra = input.int(1, "Extra")\n'
        asked = []
        replies = [f"SCRIPT:\n{bad}\nEND", f"SCRIPT:\n{good}\nEND"]

        def fake(model, messages, **kwargs):
            asked.append(messages)
            return replies.pop(0)

        with mock.patch("futuresfund.llm._post", fake):
            self.assertEqual(attempt_reason._write_script(script, "Use close.", "Why").strip(), good.strip())
        self.assertIn("added an input", asked[1][-1]["content"])

    def test_a_placeholder_is_not_a_reason(self):
        from futuresfund.attempt_reason import parse_reasoned_reply

        self.assertIsNone(parse_reasoned_reply("BECAUSE: ...\nKEEP", {"length": 10}))

    def test_keep_is_a_decision_with_its_reason(self):
        from futuresfund.attempt_reason import parse_reasoned_reply

        found = parse_reasoned_reply("BECAUSE: The exits are connected.\nKEEP", {"length": 10})
        self.assertTrue(found["keep"])
        self.assertEqual(found["because"], "The exits are connected.")


if __name__ == "__main__":
    unittest.main()
