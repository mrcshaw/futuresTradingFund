import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from harness import PINE, proposal, three
from research_department.department import Job
from research_department.inputs import ScriptInput, script_inputs
from research_department.meeting import (
    Proposal,
    apply_change,
    change_kept,
    hold_meeting,
    priced_source,
    proposal_fault,
    result_note,
)
from research_department.model import manager_prompt, parse_proposal, researcher_prompt
from research_department.service import ResearchDesk, choose_result
from research_department.stages import Day


class MeetingTests(unittest.TestCase):
    def test_a_proposal_missing_either_justification_stays_in_the_same_attempt(self):
        attempt = 1
        missing_consistency = three(consistent=False, win=True)
        missing_win_rate = three(consistent=True, win=False)
        for proposals in (missing_consistency, missing_win_rate):
            result = hold_meeting(proposals, set(), 0)
            self.assertTrue(result.resubmit)
            self.assertIsNone(result.order)
            self.assertFalse(result.consumes_attempt)
            self.assertEqual(attempt, 1)

    def test_the_manager_cannot_invent_a_fourth_change(self):
        extra = Proposal("stop_dollars", 300, "up", True, True, "A wider stop.")
        result = hold_meeting(three(), set(), extra)
        self.assertEqual(result.refused, "fourth change")
        self.assertIsNone(result.order)
        self.assertFalse(result.consumes_attempt)

    def test_the_manager_picks_one_of_the_three(self):
        proposals = three()
        result = hold_meeting(proposals, set(), 1)
        self.assertIs(result.order, proposals[1])
        self.assertTrue(result.consumes_attempt)
        self.assertFalse(result.resubmit)

    def test_one_input_changes_and_the_other_stays(self):
        result = hold_meeting(three(), set(), 0)
        updated = apply_change(PINE, result.order.variable, result.order.value)
        self.assertIn("watch_bars = input.int(6,", updated)
        self.assertIn("doji_threshold = input.float(0.25,", updated)

    def test_a_closed_direction_is_refused(self):
        closed = {("watch_bars", "up")}
        result = hold_meeting(three(), closed, 0)
        self.assertEqual(result.refused, "closed")
        self.assertIsNone(result.order)
        again = proposal("watch_bars", 8, "up")
        self.assertEqual(hold_meeting([again, *three()[1:]], closed, 0).refused, "closed")

    def test_a_plain_input_and_a_spoken_justification_can_be_read(self):
        source = 'Stochlength = input(14, title="lookback")\n'
        found = script_inputs(source)
        self.assertEqual(found[0].name, "Stochlength")
        self.assertEqual(found[0].default, 14)
        self.assertEqual(found[0].title, "lookback")
        updated = apply_change(source, "Stochlength", 20)
        self.assertIn("input(20,", updated)
        spoken = parse_proposal(
            "Set Stochlength to 20 so the $500 day is more consistent and the win percentage is higher.",
            found,
        )
        self.assertEqual(spoken.variable, "Stochlength")
        self.assertEqual(spoken.value, 20)
        self.assertTrue(spoken.consistent_day)
        self.assertTrue(spoken.higher_win_rate)
        self.assertIn("consistent", spoken.justification)

    def test_the_next_researcher_is_prompted_with_the_previous_reply(self):
        found = script_inputs(PINE)
        first = researcher_prompt("5-Candle Breakout", found, set(), 0)
        self.assertIn("You speak first", first)
        self.assertIn("at most two losing trades", first)
        self.assertIn("moves the scale toward the goal", first)
        self.assertIn("profit scale starts at $0", first)
        self.assertIn("50% of that total", first)
        self.assertIn("50% of $500 times the days tested", first)
        self.assertIn("Do not change the point value", first)
        self.assertIn("$1,000 of profit for every 7 days", first)
        self.assertIn("that change is reverted", first)
        self.assertNotIn("Answer alone", first)
        second = researcher_prompt(
            "5-Candle Breakout",
            found,
            set(),
            1,
            prior=["Lower watch_bars to 3. That keeps a trade in every session and raises the win percentage."],
        )
        self.assertIn("Researcher 1's reply is your prompt", second)
        self.assertIn("Lower watch_bars to 3", second)
        self.assertIn("consistent $500 day", second)
        boss = manager_prompt(three(), spoken=["Researcher 1 keeps the $500 day.", "Researcher 2 raises the win percentage."])
        self.assertTrue(boss.startswith("Pick the one"))
        self.assertIn("moves the scale away from the goal", boss)
        self.assertIn("Researcher 1 keeps the $500 day.", boss)
        self.assertIn("Researcher 2 raises the win percentage.", boss)

        desk = ResearchDesk()
        desk.jobs["job-1"] = Job("job-1", PINE, "ES", ["meeting"])
        desk.shown = "job-1"
        prompts = []

        def ask(prompt, listener=None):
            prompts.append(prompt)
            if prompt.startswith("Pick the one"):
                return '{"choice": 1}'
            seat = 1
            if "You speak first" in prompt:
                seat = 1
            elif "Researcher 1's reply is your prompt" in prompt:
                seat = 2
            else:
                seat = 3
            return (
                '{"variable":"watch_bars","value":5,"direction":"up","consistent_day":true,'
                '"higher_win_rate":true,"justification":"Researcher '
                + str(seat)
                + ' answers the last point, keeps the $500 day consistent, and raises the win percentage."}'
            )

        desk.ask = ask
        order, _proposals = desk._meet("5-Candle Breakout", found, set(), None)
        self.assertEqual(order.variable, "watch_bars")
        self.assertEqual(order.value, 5)
        self.assertEqual(len(prompts), 4)
        self.assertIn("Researcher 1 answers the last point", prompts[1])
        self.assertIn("Researcher 2 answers the last point", prompts[2])
        self.assertIn("Researcher 1 answers the last point", prompts[3])
        self.assertIn("Researcher 3 answers the last point", prompts[3])
        self.assertTrue(prompts[3].startswith("Pick the one"))
        self.assertIn("Do not raise a stop", prompts[3])


class SearchRuleTests(unittest.TestCase):
    def test_one_step_is_legal_and_a_jump_or_a_wider_stop_is_not(self):
        bars = ScriptInput("watch_bars", "int", 4)
        point = ScriptInput("pv", "float", 20, "Point Value ($)")
        stop = ScriptInput("stop_dollars", "int", 250, "Stop ($)")
        target = ScriptInput("tp_dollars", "int", 500, "Take Profit ($)")
        delta = ScriptInput("min_delta_pct", "float", 10, "Min Delta Imbalance (%)")
        inputs = [bars, point, stop, target, delta]
        last = {"profit": 200, "win_percentage": 0.3, "trade_count": 20, "quiet_sessions": 0, "worst_day": -800}
        self.assertEqual(proposal_fault(proposal("watch_bars", 5, "up"), inputs, None, [], 0.5, 1750, 400), "")
        self.assertIn("larger than one step", proposal_fault(proposal("watch_bars", 8, "up"), inputs, None, [], 0.5, 1750, 400))
        self.assertIn("point value", proposal_fault(proposal("pv", 21, "up"), inputs, last, [], 0.5, 1750, 400))
        self.assertIn("Do not raise", proposal_fault(proposal("stop_dollars", 300, "up"), inputs, last, [], 0.5, 1750, 400))
        self.assertIn("Do not lower", proposal_fault(proposal("tp_dollars", 400, "down"), inputs, last, [], 0.5, 1750, 400))
        self.assertIn("already trading", proposal_fault(proposal("min_delta_pct", 8, "down"), inputs, last, [], 0.5, 1750, 400))
        self.assertIn("already tried", proposal_fault(proposal("watch_bars", 5, "up"), inputs, None, [("watch_bars", 5)], 0.5, 1750, 400))

    def test_a_change_is_kept_only_when_profit_and_win_percentage_both_hold(self):
        before = {"best_win": 0.4, "best_profit": 700}
        self.assertTrue(change_kept(None, before))
        self.assertTrue(change_kept(before, {"best_win": 0.45, "best_profit": 700}))
        self.assertTrue(change_kept(before, {"best_win": 0.4, "best_profit": 900}))
        self.assertFalse(change_kept(before, {"best_win": 0.5, "best_profit": 100}))
        self.assertFalse(change_kept(before, {"best_win": 0.2, "best_profit": 1200}))
        self.assertFalse(change_kept(before, {"best_win": 0.4, "best_profit": 700}))

    def test_the_backtest_uses_each_markets_point_value(self):
        source = 'pv = input.float(20, "Point Value ($)")\nstop_dollars = input.int(250, "Stop ($)")\n'
        self.assertIn("input.float(50", priced_source(source, "ES"))
        self.assertIn("input.int(250", priced_source(source, "ES"))
        self.assertIn("input.float(20", priced_source(source, "NQ"))
        self.assertIn("input.float(2", priced_source(source, "MNQ"))
        self.assertEqual(priced_source("watch_bars = input.int(4)\n", "ES"), "watch_bars = input.int(4)\n")

    def test_a_passing_result_is_chosen_ahead_of_a_higher_win_percentage(self):
        weak = {"win_percentage": 0.8, "profit": 100, "trade_count": 10}
        strong = {"win_percentage": 0.5, "profit": 2000, "trade_count": 10}
        chosen = choose_result([(None, weak, None, "MNQ"), (None, strong, None, "NQ")], 7, 0.5, 500)
        self.assertEqual(chosen[1], strong)

    def test_the_note_shows_the_best_profit_and_the_best_win_percentage(self):
        board = [
            {"instrument": "MNQ", "timeframe": "2m", "profit": 341, "win_percentage": 0.4, "trade_count": 45, "worst_day": -479},
            {"instrument": "NQ", "timeframe": "2m", "profit": 16015, "win_percentage": 0.22, "trade_count": 40, "worst_day": -1381},
        ]
        from research_department.meeting import board_marks

        note = result_note(board, board_marks(board), 30, 500, 0.5)
        self.assertIn("$4,286", note)
        self.assertIn("$7,500", note)
        self.assertIn("NQ 2m", note)
        self.assertIn("16,015", note)
        self.assertIn("MNQ 2m", note)
        self.assertIn("clears the profit goal", note)

    def test_a_change_that_lowers_the_result_is_reverted(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "strategies.json"
            desk = ResearchDesk(library_path=library)
            desk.current_file = "one.pine"
            desk.jobs["job-1"] = Job("job-1", PINE, "ES", ["meeting"])
            desk.shown = "job-1"

            def backtest(source, _instrument, _timeframe, count):
                if "input.int(5" in source:
                    return [Day(-50, 300, 0, 4) for _ in range(count)]
                return [Day(100, 200, 1, 4) for _ in range(count)]

            def ask(prompt, listener=None):
                if "Already tried" in prompt:
                    desk._worker_stop.set()
                    return ""
                if prompt.startswith("Pick the one"):
                    return '{"choice": 0}'
                return (
                    '{"variable":"watch_bars","value":5,"direction":"up","consistent_day":true,'
                    '"higher_win_rate":true,"justification":"One step keeps the $500 day consistent and raises the win percentage."}'
                )

            desk.backtest = backtest
            desk.ask = ask
            desk._research("job-1")
            saved = json.loads(library.parent.joinpath("progress.json").read_text(encoding="utf-8"))
            self.assertIn("input.int(4", saved["source"])
            self.assertNotIn("input.int(5", saved["source"])
            self.assertIn(["watch_bars", 5], saved["tried"])
            self.assertTrue(any("Reverted watch_bars" in line for line in desk.jobs["job-1"].feed["lines"]))


class ReplyLimitTests(unittest.TestCase):
    def test_a_reply_stops_when_the_ten_minutes_are_already_gone(self):
        from research_department.model import _stream

        class Response:
            def readline(self):
                raise AssertionError("a finished deadline must not keep reading")

        text = _stream(Response(), None, time.monotonic() - 1)
        self.assertIn("longer than 10 minutes", text)

    def test_text_already_received_is_kept_when_the_clock_runs_out(self):
        import socket
        from research_department.model import _stream

        class Response:
            def __init__(self):
                self.sent = False

            def readline(self):
                if not self.sent:
                    self.sent = True
                    return b'{"message":{"content":"{\\"variable\\":\\"watch_bars\\",\\"value\\":5}"}}\n'
                raise socket.timeout()

        text = _stream(Response(), None, time.monotonic() + 1)
        self.assertIn("watch_bars", text)
        self.assertIn("5", text)

    def test_the_next_open_step_skips_a_tried_value_and_the_point_value(self):
        from research_department.meeting import next_allowed_change

        inputs = [
            ScriptInput("pv", "float", 20, "Point Value ($)"),
            ScriptInput("min_delta_pct", "float", 8),
            ScriptInput("watch_bars", "int", 4, "Max Bars"),
        ]
        last = {"quiet_sessions": 2, "profit": 536, "win_percentage": 0.83, "trade_count": 6, "worst_day": -45}
        chosen = next_allowed_change(inputs, last, [("min_delta_pct", 6.4)], 0.5, 1750, 400, "min_delta_pct")
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.variable, "watch_bars")
        self.assertEqual(chosen.value, 3)
        self.assertEqual(
            proposal_fault(chosen, inputs, last, [("min_delta_pct", 6.4)], 0.5, 1750, 400),
            "",
        )
