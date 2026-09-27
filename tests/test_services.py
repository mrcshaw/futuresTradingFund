"""Ingestion, NinjaTrader confirmation, and the 5:00pm prop-account record."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from futuresfund.alerts import parse_alert


class ConfirmationTests(unittest.TestCase):
    def test_a_fill_confirms_and_a_reject_does_not(self):
        from futuresfund.trade_confirmation import judge

        signal = {"account": "PA-APEX-1", "instrument": "ES1!"}
        sent = {"sent": True, "body": ""}
        fill = {
            "account": "PA-APEX-1",
            "instrument": "ES 12-26",
            "isExit": False,
            "price": 5800.25,
            "quantity": 1,
            "epoch": 10,
        }
        confirmed = judge(signal, sent, [fill], closing=False)
        self.assertTrue(confirmed["confirmed"])
        self.assertIn("5800.25", confirmed["reason"])
        refused = judge(signal, sent, [], "Rejected", closing=False)
        self.assertFalse(refused["confirmed"])
        self.assertEqual(refused["status"], "rejected")
        dry = judge(signal, {"sent": False, "dry_run": True}, [fill], closing=False)
        self.assertEqual(dry["status"], "not_sent")

    def test_a_streamed_position_is_what_the_floor_manages(self):
        from futuresfund import ninjatrader
        from futuresfund.trade_manager import liquidation_reason, reconcile_position

        ninjatrader.apply_frame({
            "type": "positionUpdate",
            "account": "PA-APEX-1",
            "instrument": "ES 12-26",
            "marketPosition": "Long",
            "quantity": 1,
            "averagePrice": 5000,
            "unrealizedProfitLoss": -2000,
        })
        held, entry = reconcile_position({"account": "PA-APEX-1", "instrument": "ES1!"}, 0, None)
        self.assertEqual(held, 1)
        self.assertEqual(entry, 5000)
        reason = liquidation_reason(50000, held, entry, 5000, 50, -2000)
        self.assertIn("liquidate", reason)
        self.assertIsNone(liquidation_reason(50000, 1, 5000, 4998, 50, None))
        ninjatrader.reset_cache()


class DeskFlowTests(unittest.TestCase):
    def setUp(self):
        os.environ["DRY_RUN"] = "true"
        os.environ["PROP_ACCOUNTS"] = "PA-APEX-1"
        os.environ["FUTURES_MAX_QTY"] = "5"
        from futuresfund.ninjatrader import reset_cache

        reset_cache()

    def test_ingestion_adds_the_bar_and_the_floor_does_not(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "PA-APEX-1",
                "account_size": 50000,
                "strategies": [],
            })
            board = Board()
            refused = handle_interval(board, parse_alert(
                "account=PA-OTHER;instrument=ES1!;qty=1;timeframe=5;price=5000;time=2024-01-02T14:00:00Z;"
            ))
            self.assertFalse(refused["sent"])
            self.assertTrue(any(message["author"] == "Ingestion" for message in board.messages))
            self.assertFalse(any(message["author"] == "Floor Trader" for message in board.messages))

            added = handle_interval(board, parse_alert(
                "account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=5;price=5001;"
                "open=5000;high=5002;low=4999;time=2024-01-02T14:05:00Z;"
            ))
            self.assertEqual(added["action"], "bar")
            self.assertEqual(len(research.load_bars()), 1)
            added_notes = [message for message in board.messages if "Added the closed" in message["text"]]
            self.assertEqual(added_notes[0]["author"], "Ingestion")

    def test_the_drawdown_tells_the_floor_to_close(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "PA-APEX-1",
                "account_size": 50000,
                "qty": 1,
                "working": {"contracts": 1, "entry": 5000},
                "strategies": [],
            })
            board = Board()
            closed = handle_interval(board, parse_alert(
                "account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=1;price=4960;"
                "open=5000;high=5000;low=4960;time=2024-01-02T14:03:00Z;"
            ))
            self.assertEqual(closed["action"], "close")
            self.assertIn("liquidate", closed["reason"])
            self.assertTrue(any(
                message["author"] == "Trading Analyst" and "Floor trader, close this trade" in message["text"]
                for message in board.messages
            ))

    def test_prop_mail_is_recorded_at_the_five_pm_meeting(self):
        from futuresfund import book as bookmod
        from futuresfund.account_mail import accept_mail, brief_prop_accounts, parse_account_update
        from futuresfund.board import Board

        parsed = parse_account_update("Daily loss limit is $1,500. Trailing drawdown is $2,000. Max contracts is 4.")
        self.assertEqual(parsed["daily_loss_limit"], 1500)
        self.assertEqual(parsed["max_drawdown"], 2000)
        self.assertEqual(parsed["max_contracts"], 4)
        self.assertEqual(parse_account_update("The balance is 25000."), {})

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            board = Board()
            accepted = accept_mail(board, {
                "from": "notices@apex.com",
                "subject": "Account update",
                "text": "Daily loss limit is $1,500. Trailing drawdown is $2,000. Max contracts is 4.",
            })
            self.assertTrue(accepted["ok"])
            self.assertTrue(any(message["author"] == "Systems Administrator" for message in board.messages))
            mention = brief_prop_accounts(board)
            saved = bookmod.load()["headquarters_rules"]["text"]
            self.assertIn("5:00pm", saved)
            self.assertIn("1,500", saved)
            self.assertEqual(saved, mention)
            self.assertTrue(any(
                message["author"] == "Portfolio Manager" and message["channel"] == "headquarters"
                for message in board.messages
            ))


if __name__ == "__main__":
    unittest.main()
