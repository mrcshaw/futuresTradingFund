"""Chat with the Portfolio Manager about the application: direct answers and tasks."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from futuresfund import desk_chat
from futuresfund.board import Board

SCRIPT = """//@version=5
strategy("Two EMA Cross [Test]", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1)
fast = ta.ema(close, 9)
slow = ta.ema(close, 21)
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("Long")
"""


class ScriptFilingTests(unittest.TestCase):
    def test_a_pasted_strategy_is_filed_in_the_learning_folder_under_its_title(self):
        board = Board()
        with TemporaryDirectory() as tmp:
            learning = Path(tmp) / "learningStrategies"
            learned = Path(tmp) / "learnedStrategies"
            with mock.patch.object(desk_chat, "LEARNING", learning), mock.patch.object(desk_chat, "LEARNED", learned), \
                    mock.patch("futuresfund.indicators.record_script", lambda *args, **kwargs: []):
                reply = desk_chat.answer(board, "Portfolio Manager", "can you add this script to the learningStrategies folder\n" + SCRIPT)
                self.assertIn("Filed Two EMA Cross [Test] as learningStrategies/two_ema_cross_test.pine", reply)
                saved = (learning / "two_ema_cross_test.pine").read_text(encoding="utf-8")
                self.assertTrue(saved.startswith("//@version=5"))
                self.assertNotIn("can you add this script", saved)
                # A second copy does not overwrite the first.
                again = desk_chat.answer(board, "Portfolio Manager", SCRIPT)
                self.assertIn("two_ema_cross_test_2.pine", again)
                self.assertIn("Press Start", reply)
        developer_posts = [m for m in board.snapshot()["messages"] if m["channel"] == "2min chart developer"]
        self.assertTrue(developer_posts)

    def test_an_indicator_is_not_filed_and_another_desk_points_to_the_portfolio_manager(self):
        board = Board()
        indicator = SCRIPT.replace('strategy("Two EMA Cross [Test]", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1)', 'indicator("Only lines")')
        indicator = "\n".join(line for line in indicator.splitlines() if "strategy." not in line and not line.startswith("if "))
        with TemporaryDirectory() as tmp:
            with mock.patch.object(desk_chat, "LEARNING", Path(tmp)), mock.patch.object(desk_chat, "LEARNED", Path(tmp) / "x"):
                self.assertIn("indicator, not a strategy", desk_chat.answer(board, "Portfolio Manager", indicator))
                self.assertIn("Ask the Portfolio Manager", desk_chat.answer(board, "Floor Trader", SCRIPT))
                self.assertFalse(list(Path(tmp).glob("*.pine")))

    def test_asking_to_add_a_script_without_one_asks_for_the_paste(self):
        reply = desk_chat.answer(Board(), "Portfolio Manager", "can you add this script to the learningStrategies folder")
        self.assertIn("Paste the whole Pine strategy", reply)
        with mock.patch.object(desk_chat, "research_lines", lambda board: ["Research is idle.", "Learning folder: 3 Pine scripts waiting or in progress."]):
            asked = desk_chat.answer(Board(), "Portfolio Manager", "how many scripts are waiting in the learning folder and what are the developers doing?")
        self.assertIn("Learning folder: 3", asked)
        self.assertNotIn("Paste", asked)


class QuestionTests(unittest.TestCase):
    def test_the_active_strategy_question_is_answered_from_the_execution_record(self):
        lines = ["Trading mode for new entries: paper.", "Floor Trader is executing POC Base on the 5m ES1! chart for account paper since 3:20 PM: flat right now. 0 closed trades, realized 0.0."]
        with mock.patch.object(desk_chat, "executing_lines", lambda: lines):
            reply = desk_chat.answer(Board(), "Portfolio Manager", "what is the active strategies we are currently running?")
        self.assertIn("Floor Trader is executing POC Base", reply)
        self.assertIn("paper", reply)

    def test_the_email_question_is_answered_with_the_mail_record_and_the_research_state(self):
        board = Board()
        facts = ["Report email is configured. Reports go from a@x to b@y.", "No email attempt has been logged yet."]
        with mock.patch.object(desk_chat, "mail_lines", lambda probe=False: facts):
            reply = desk_chat.answer(board, "Portfolio Manager", "why are you not emailing the reports on completed strategies")
        self.assertIn("Report email is configured", reply)
        self.assertIn("Research is idle", reply)
        self.assertIn("Press Start", reply)

    def test_a_plain_question_goes_to_the_model_with_facts_chosen_by_its_words(self):
        board = Board()
        self.assertIsNone(desk_chat.answer(board, "Portfolio Manager", "how does the drawdown rule work on Apex?"))
        with mock.patch.object(desk_chat, "mail_lines", lambda probe=False: ["MAIL FACT"]), \
                mock.patch.object(desk_chat, "executing_lines", lambda: ["EXEC FACT"]):
            facts = desk_chat.facts_for(board, "tell me about the email and what is executing")
        self.assertIn("MAIL FACT", facts)
        self.assertIn("EXEC FACT", facts)
        self.assertIn("What the Portfolio Manager can do", facts)


class TaskTests(unittest.TestCase):
    def test_a_question_about_the_report_does_not_send_it_but_an_instruction_does(self):
        board = Board()
        with mock.patch.object(desk_chat, "_send_report_now", lambda b: "SENT") as _, \
                mock.patch.object(desk_chat, "mail_lines", lambda probe=False: ["MAIL"]):
            self.assertEqual(desk_chat.answer(board, "Portfolio Manager", "please send the report now"), "SENT")
            self.assertNotEqual(desk_chat.answer(board, "Portfolio Manager", "did you send the report?"), "SENT")
            # Another desk cannot send it.
            self.assertNotEqual(desk_chat.answer(board, "Floor Trader", "send the report"), "SENT")

    def test_research_can_be_started_and_stopped_by_name(self):
        board = Board()
        with mock.patch.object(desk_chat, "_start_research", lambda: "STARTED"), mock.patch.object(desk_chat, "_stop_research", lambda: "STOPPED"):
            self.assertEqual(desk_chat.answer(board, "Portfolio Manager", "start the researchers"), "STARTED")
            self.assertEqual(desk_chat.answer(board, "Portfolio Manager", "stop research"), "STOPPED")
            self.assertNotEqual(desk_chat.answer(board, "Portfolio Manager", "is research running?"), "STARTED")

    def test_a_card_is_matched_by_its_title_and_live_mode_is_not_switched_from_chat(self):
        cards = [{"id": "a", "title": "POC Base Color + Pullback"}, {"id": "b", "title": "Overnight Drift Capture [Optimized]"}]
        self.assertEqual(desk_chat._match_card("start executing overnight drift capture", cards)["id"], "b")
        self.assertEqual(desk_chat._match_card("start the poc base pullback card", cards)["id"], "a")
        self.assertIsNone(desk_chat._match_card("start executing", cards))
        reply = desk_chat.answer(Board(), "Portfolio Manager", "switch to live")
        self.assertIn("not from chat", reply)

    def test_the_portfolio_manager_resets_a_liquidated_paper_account_without_the_model(self):
        book = {
            "positions": {
                "paper|NQ|ema": {
                    "account": "paper",
                    "contracts": -1,
                    "entry_ms": 99,
                    "strategy_id": "ema-bb-nq",
                    "mode": "paper",
                },
                "live|ES|x": {"account": "live1", "contracts": 1, "mode": "live"},
            },
            "trades": [{"account": "paper", "pnl": -10}, {"account": "live1", "pnl": 20}],
            "account_books": {
                "paper": {"size": 50000, "realized": -1434.0, "peak": 50000},
                "live1": {"size": 50000, "realized": 40.0, "peak": 50000},
            },
            "paper": {"realized": -1434.0, "equity": 47686.0},
            "executing": [{
                "id": "ema-bb-nq",
                "account": "paper",
                "handled": [],
                "trades": 4,
                "wins": 0,
                "realized": -1434.0,
            }],
            "strategy_acceptance": {"min_profit": 15000},
        }
        saved = {}

        def fake_save(value):
            saved.clear()
            saved.update(value)

        with mock.patch("futuresfund.desk_services.load", lambda: book), mock.patch("futuresfund.desk_services.save", fake_save):
            reply = desk_chat.answer(Board(), "Portfolio Manager", "the account has been liquidated. reset the paper account")
        self.assertIn("Paper account reset", reply)
        self.assertNotIn("timed out", reply)
        self.assertEqual(list(saved["positions"]), ["live|ES|x"])
        self.assertEqual(saved["account_books"]["paper"]["realized"], 0.0)
        self.assertEqual(saved["account_books"]["live1"]["realized"], 40.0)
        self.assertEqual(saved["executing"][0]["handled"], [99])
        self.assertEqual(saved["executing"][0]["realized"], 0.0)
        self.assertEqual(saved["strategy_acceptance"]["min_profit"], 15000)
        self.assertEqual([row["account"] for row in saved["trades"]], ["live1"])

    def test_a_question_about_resetting_does_not_reset_the_account(self):
        with mock.patch("futuresfund.desk_services.reset_paper_account", return_value="RESET") as reset:
            reply = desk_chat.answer(Board(), "Portfolio Manager", "did you reset the paper account?")
        reset.assert_not_called()
        self.assertNotEqual(reply, "RESET")


if __name__ == "__main__":
    unittest.main()
