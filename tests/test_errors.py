"""The error journal: errors in the workflow are written down, the Systems Administrator reports them, the Portfolio Manager emails."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from futuresfund import errors
from futuresfund.board import Board


class JournalTests(unittest.TestCase):
    def test_an_error_posted_on_the_board_is_written_in_the_journal_and_repeats_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"):
            board = Board()
            board.on_error = errors.journal_post
            board.post("2min chart developer", "orb.pine stopped on an engine error: boom. It was not marked finished.", kind="error")
            board.post("2min chart developer", "orb.pine stopped on an engine error: boom. It was not marked finished.", kind="error")
            board.post("Chart researcher", "A report line, not an error.", kind="report")
            board.post("Portfolio Manager", "Something else broke.", kind="error", channel="headquarters")
            rows = errors.entries()
            data = json.loads(errors.JOURNAL.read_text(encoding="utf-8"))
        self.assertEqual(data["kind"], "error_journal")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["desk"], "2min chart developer")
        self.assertEqual(rows[0]["repeats"], 2)
        self.assertEqual(rows[0]["source"], "board")
        self.assertEqual(rows[1]["desk"], "Portfolio Manager")

    def test_a_board_without_the_hook_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"):
            Board().post("Floor Trader", "An error in a test board.", kind="error")
            self.assertFalse(errors.JOURNAL.exists())

    def test_entries_since_a_time_and_the_report_text(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"):
            errors.record("5 min chart developer", "poc.pine attempt 3 on 5-minute: TypeError: '<' not supported", source="engine")
            errors.record("5 min chart developer", "poc.pine attempt 4 on 5-minute: TypeError: '<' not supported", source="engine")
            errors.record("Portfolio Manager", "Email not sent: Futures desk report. The report was not emailed: timed out", source="mail")
            rows = errors.entries()
            old = (datetime.now(errors.ET) - timedelta(hours=3)).isoformat(timespec="seconds")
            rows[0]["time"] = rows[0]["last"] = old
            data = json.loads(errors.JOURNAL.read_text(encoding="utf-8"))
            data["errors"][0]["time"] = data["errors"][0]["last"] = old
            errors.JOURNAL.write_text(json.dumps(data), encoding="utf-8")
            since = (datetime.now(errors.ET) - timedelta(hours=1)).isoformat(timespec="seconds")
            recent = errors.entries(since)
            text = errors.report(errors.entries(), None)
            empty = errors.report([], since)
        self.assertEqual(len(recent), 2)
        self.assertIn("Workflow error report", text)
        self.assertIn("Compiled by the Systems Administrator", text)
        self.assertIn("3 errors on 2 desks, 2 distinct messages", text)
        self.assertIn("5 min chart developer: 2 errors", text)
        self.assertIn("Portfolio Manager: 1 error", text)
        self.assertIn("Recurring errors", text)
        self.assertIn("2 times on 5 min chart developer", text)
        self.assertIn("; engine]", text)
        self.assertIn("; mail]", text)
        self.assertIn("No errors were recorded", empty)

    def test_the_journal_keeps_its_cap(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"), mock.patch.object(errors, "LIMIT", 5):
            for index in range(8):
                errors.record("Desk", f"error number {index} with a distinct text {index * 7}", source="board")
            rows = errors.entries()
        self.assertEqual(len(rows), 5)
        self.assertIn("error number 7", rows[-1]["text"])


class DeliveryTests(unittest.TestCase):
    def test_the_systems_administrator_compiles_and_the_portfolio_manager_emails_to_the_fund_address(self):
        sent = []

        def fake_send(body, subject="Futures desk report"):
            sent.append((subject, body))
            return {"sent": True, "reason": "sent", "to": "thefutureoffuturestrading@gmail.com"}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"), \
                mock.patch("futuresfund.mailer.send_report", fake_send):
            board = Board()
            board.on_error = errors.journal_post
            board.post("15 min chart developer", "ema.pine stopped on an engine error: KeyError 'x'.", kind="error")
            result = errors.deliver(board)
            snapshot = board.snapshot()
            last = errors.last_report()
            again = errors.deliver(board)
        self.assertTrue(result["sent"])
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].startswith("Futures desk error report"))
        self.assertIn("15 min chart developer: 1 error", sent[0][1])
        self.assertEqual(snapshot["reports"][errors.REPORT_KEY], result["report"])
        systems = [m for m in snapshot["messages"] if m["author"] == errors.SYSTEMS]
        self.assertTrue(any(m["kind"] == "report" and "Workflow error report" in m["text"] for m in systems))
        self.assertTrue(any(m["channel"] == errors.PORTFOLIO_MANAGER and "Please email it" in m["text"] for m in systems))
        manager = [m for m in snapshot["messages"] if m["author"] == errors.PORTFOLIO_MANAGER]
        self.assertTrue(any("Emailed the workflow error report" in m["text"] and "thefutureoffuturestrading@gmail.com" in m["text"] for m in manager))
        self.assertTrue(last["sent"])
        self.assertEqual(last["errors"], 1)
        # Nothing new since the report: nothing is emailed the second time.
        self.assertFalse(again["sent"])
        self.assertEqual(again["errors"], 0)
        self.assertEqual(len(sent), 1)

    def test_a_failed_email_is_said_on_the_board_and_the_report_stays_on_the_desk(self):
        def fake_send(body, subject="Futures desk report"):
            return {"sent": False, "reason": "The report was not emailed: timed out."}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"), \
                mock.patch("futuresfund.mailer.send_report", fake_send):
            errors.record("Systems Administrator", "Request GET /api/book failed: ValueError: bad", source="request")
            board = Board()
            result = errors.deliver(board)
            manager = [m["text"] for m in board.snapshot()["messages"] if m["author"] == errors.PORTFOLIO_MANAGER]
            last = errors.last_report()
        self.assertFalse(result["sent"])
        self.assertTrue(any("was not emailed" in text and "timed out" in text for text in manager))
        self.assertFalse(last["sent"])
        self.assertEqual(last["errors"], 1)

    def test_a_failed_email_from_the_mailer_is_journaled_but_a_missing_password_is_not(self):
        from futuresfund import mailer

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"), \
                mock.patch.object(mailer, "MAIL_LOG", Path(tmp) / "_sent.json"):
            mailer._record("Futures desk report", {"sent": False, "reason": "SMTP_PASSWORD is empty, so the report was kept on the desk."})
            self.assertEqual(errors.entries(), [])
            mailer._record("Futures desk report", {"sent": False, "reason": "The report was not emailed: connection refused"})
            rows = errors.entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "mail")
        self.assertIn("connection refused", rows[0]["text"])

    def test_the_daily_report_is_due_after_five_past_five_once_a_day(self):
        monday = datetime(2026, 10, 5, 17, 4, tzinfo=errors.ET)
        self.assertFalse(errors.due(monday, {}))
        self.assertTrue(errors.due(monday.replace(minute=5), {}))
        self.assertTrue(errors.due(monday.replace(minute=40), {}))
        self.assertFalse(errors.due(monday.replace(hour=18, minute=1), {}))
        self.assertFalse(errors.due(monday.replace(minute=6), {errors.BOOK_KEY: "2026-10-05 05:05 PM ET"}))
        self.assertTrue(errors.due(monday.replace(minute=6), {errors.BOOK_KEY: "2026-10-04 05:05 PM ET"}))

    def test_the_scheduler_hands_the_report_over_once_and_marks_the_book(self):
        book = {}
        delivered = []

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"), \
                mock.patch("futuresfund.book.load", lambda: dict(book)), \
                mock.patch("futuresfund.book.save", lambda data: book.update(data)), \
                mock.patch.object(errors, "deliver", lambda board, force=False: delivered.append(board) or {"sent": True}):
            when = datetime(2026, 10, 5, 17, 6, tzinfo=errors.ET)
            first = errors.scheduled(None, when)
            second = errors.scheduled(None, when.replace(minute=7))
            early = errors.scheduled(None, datetime(2026, 10, 6, 9, 0, tzinfo=errors.ET))
        self.assertEqual(first, {"sent": True})
        self.assertIsNone(second)
        self.assertIsNone(early)
        self.assertEqual(len(delivered), 1)
        self.assertTrue(str(book[errors.BOOK_KEY]).startswith("2026-10-05"))


class ChatTests(unittest.TestCase):
    def test_the_portfolio_manager_sends_the_error_report_on_request_and_answers_questions_about_errors(self):
        from futuresfund import desk_chat

        calls = []

        def fake_deliver(board, force=False):
            calls.append(force)
            return {"sent": True, "errors": 2, "to": "thefutureoffuturestrading@gmail.com", "reason": "sent", "report": "x"}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(errors, "JOURNAL", Path(tmp) / "errors.json"), \
                mock.patch.object(errors, "deliver", fake_deliver):
            errors.record("Chart researcher", "The finished studies could not be ranked again: boom", source="board")
            board = Board()
            reply = desk_chat.answer(board, "Portfolio Manager", "send the error report")
            asked = desk_chat.answer(board, "Portfolio Manager", "what errors have we had?")
            other = desk_chat.answer(board, "Risk Manager", "email the errors")
        self.assertEqual(calls, [True])
        self.assertIn("compiled the workflow error report (2 errors)", reply)
        self.assertIn("thefutureoffuturestrading@gmail.com", reply)
        self.assertIn("No error report has been sent yet.", asked)
        self.assertIn("could not be ranked again: boom", asked)
        self.assertIsNone(other)

    def test_sending_the_fund_report_is_still_the_fund_report(self):
        from futuresfund import desk_chat

        with mock.patch.object(desk_chat, "_send_report_now", lambda board: "FUND"), \
                mock.patch.object(desk_chat, "_send_error_report_now", lambda board: "ERRORS"):
            self.assertEqual(desk_chat.answer(Board(), "Portfolio Manager", "send the report"), "FUND")
            self.assertEqual(desk_chat.answer(Board(), "Portfolio Manager", "email the error report please"), "ERRORS")


if __name__ == "__main__":
    unittest.main()
