"""Questions by email (questions.py): the researcher asks, the Portfolio Manager carries it, the researcher applies the answer.

Nothing here can send email or open a mailbox: the send path and imaplib.IMAP4_SSL are stubbed, and the
question store, the strategy folders and the notes live in a temp folder.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from futuresfund import learn, mailer, questions

PINE = (
    "//@version=6\n"
    'strategy("Owner Test", overlay=true)\n'
    'stop_dollars = input.int(250, "Stop ($)", minval=50, maxval=700, step=10)\n'
    'tp_dollars = input.int(500, "Take Profit ($)", minval=50, step=10)\n'
    'length = input.int(14, "Length", minval=2, maxval=200)\n'
    'daily_loss_limit = input.float(0.0, "Daily loss limit", minval=0)\n'
    "if ta.crossover(close, ta.sma(close, length))\n"
    '    strategy.entry("L", strategy.long)\n'
)


class _Board:
    def __init__(self):
        self.posts: list[tuple[str, str, str]] = []
        self.cancel = threading.Event()

    def post(self, author, text, kind="log", channel=None):
        self.posts.append((author, str(text), channel or author))

    def log(self, *args, **kwargs):
        pass

    def set_task(self, *args, **kwargs):
        pass

    def set_activity(self, *args, **kwargs):
        pass

    def set_status(self, *args, **kwargs):
        pass

    def texts(self, author=None):
        return [text for who, text, _ in self.posts if author is None or who == author]


class _Desk(unittest.TestCase):
    """A temp desk: learning, cannotRun, awaiting, notes and the question store."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.learning = self.root / "futuresfund" / "learningStrategies"
        self.learned = self.root / "futuresfund" / "learnedStrategies"
        self.notes = self.root / "futuresfund" / "researchNotes"
        self.cannot = self.root / "cannotRunStrategies"
        self.waiting = self.root / "awaitingAnswerStrategies"
        for folder in (self.learning, self.learned, self.notes):
            folder.mkdir(parents=True)
        self.script = self.learning / "owner_test.pine"
        self.script.write_text(PINE, encoding="utf-8")
        for patch in (
            mock.patch.object(learn, "LEARNING", self.learning),
            mock.patch.object(learn, "LEARNED", self.learned),
            mock.patch.object(learn, "CANNOT_RUN", self.cannot),
            mock.patch.object(learn, "NOTES", self.notes),
            mock.patch.object(questions, "STORE", self.notes / "questions"),
            mock.patch.object(questions, "EMAIL_QUESTIONS", True),
            # The researcher's jobs run inline here, and the model is never reached.
            mock.patch.object(learn, "researcher_job", lambda key, work, urgent=False: (work(), True)[1]),
            mock.patch.object(questions, "_model_reply", lambda *args, **kwargs: ""),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        questions.clear_memory()
        self.addCleanup(questions.clear_memory)
        questions._owner_memory.clear()
        self.board = _Board()

    def ask(self, kind="no_trades", reason="It took no trades and made no profit 20 times in a row.", path=None):
        return questions.ask_owner(
            self.board, path or self.script, "Chart researcher", reason, kind,
            chart={"root": "ES", "timeframe": "5m", "engine": 2, "developer": "5 min chart developer"},
        )


class AskTests(_Desk):
    def test_a_blocking_question_moves_the_script_and_its_twin_and_the_engine_moves_on(self):
        twin = self.learning / "owner_test.py"
        twin.write_text("# twin", encoding="utf-8")
        outcome = self.ask()
        self.assertEqual(outcome, "awaiting")
        self.assertFalse(self.script.exists())
        self.assertFalse(twin.exists())
        self.assertTrue((self.waiting / "owner_test.pine").is_file())
        self.assertTrue((self.waiting / "owner_test.py").is_file())
        note = (self.waiting / "owner_test.question.txt").read_text(encoding="utf-8")
        record = questions.open_questions()[0]
        self.assertRegex(record["qid"], r"^FFQ-[0-9a-f]{6}$")
        self.assertIn(record["qid"], note)
        self.assertIn("1. Put it back in the queue as it is", note)
        self.assertEqual(record["asked_by"], "Chart researcher")
        self.assertEqual(record["chart"], {"root": "ES", "timeframe": "5m", "engine": 2})
        self.assertEqual(record["origin_folder"], str(self.learning))
        self.assertTrue(record["blocking"])
        # The researcher posts the question on its channel; the engine says it moved on.
        self.assertTrue(any(record["qid"] in text and "Portfolio Manager emails it" in text for text in self.board.texts("Chart researcher")))
        moved = [text for text in self.board.texts("5 min chart developer") if "waits in awaitingAnswerStrategies" in text]
        self.assertEqual(moved, [f"owner_test.pine waits in awaitingAnswerStrategies for your emailed answer {record['qid']}. The engine moved on to the next script."])
        # The record is on disk, in the questions subfolder, not at the top of researchNotes.
        saved = json.loads((self.notes / "questions" / f"{record['qid']}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kind"], "no_trades")
        self.assertEqual(list(self.notes.glob("*.json")), [])
        # The template stands in when the researcher's model gives nothing.
        self.assertEqual(saved["worded_by"], "template")
        self.assertIn("I am the Chart researcher", saved["explanation"])

    def test_a_newer_copy_in_the_origin_folder_is_not_overwritten_on_resume(self):
        self.ask()
        record = questions.open_questions()[0]
        newer = PINE + "// the owner's newer copy\n"
        self.script.write_text(newer, encoding="utf-8")
        path = questions._resume(record, self.board)
        self.assertEqual(path, self.script)
        self.assertEqual(self.script.read_text(encoding="utf-8"), newer)
        self.assertFalse((self.waiting / "owner_test.pine").exists())
        self.assertTrue((self.waiting / "replaced" / "owner_test.pine").is_file())

    def test_a_file_another_engine_already_moved_is_not_an_error(self):
        record = {"qid": "FFQ-eeeeee", "file": self.script.name, "title": "t", "asked_by": "Chart researcher",
                  "asked_at": "now", "reason": "r", "applied_default": "", "options": []}
        with mock.patch.object(learn, "_place_aside", mock.Mock(side_effect=FileNotFoundError)):
            questions._move_to_awaiting(self.script, record)
        self.assertNotIn("held_name", record)

    def test_an_open_question_on_the_same_file_and_kind_is_not_asked_twice(self):
        self.ask()
        self.script.write_text(PINE, encoding="utf-8")
        self.ask()
        self.assertEqual(len(questions.open_questions()), 1)
        self.ask(kind="unrunnable", reason="Invalid session specification.")
        self.assertEqual(sorted(row["kind"] for row in questions.open_questions()), ["no_trades", "unrunnable"])

    def test_a_repair_question_does_not_move_the_script(self):
        outcome = self.ask(kind="repair", reason="It did not compile: Undeclared identifier 'foo'.")
        self.assertEqual(outcome, "")
        self.assertTrue(self.script.is_file())
        record = questions.open_questions()[0]
        self.assertFalse(record["blocking"])
        self.assertIn("repair is running", record["applied_default"])

    def test_email_questions_off_parks_the_script_the_old_way(self):
        with mock.patch.object(questions, "EMAIL_QUESTIONS", False):
            outcome = self.ask()
        self.assertEqual(outcome, "cannot_run")
        self.assertTrue((self.cannot / "owner_test.pine").is_file())
        self.assertEqual(questions.open_questions(), [])

    def test_the_researcher_model_words_the_question(self):
        words = "I ran owner_test.pine on the ES 5m chart and it never entered a trade in 20 runs. The script waits for you."
        with mock.patch.object(questions, "_model_reply", lambda *args, **kwargs: words):
            self.ask()
        record = questions.open_questions()[0]
        self.assertEqual(record["worded_by"], "model")
        self.assertEqual(record["explanation"], words)


class OutboxTests(_Desk):
    def test_the_outbox_never_sends_in_a_test_run(self):
        self.ask()
        send = mock.Mock(side_effect=AssertionError("a test tried to email a question"))
        with mock.patch.object(mailer, "send_question", send), mock.patch.object(questions, "WORDING_WAIT_SECONDS", 0):
            self.assertFalse(questions.start(self.board))
            self.assertEqual([thread for thread in questions._state["threads"] if thread.is_alive()], [])
            self.assertFalse(questions.mail_allowed())
            self.assertEqual(questions.send_pending(self.board), [])
        send.assert_not_called()
        self.assertTrue(mailer.test_run())

    def test_one_question_is_emailed_with_its_id_in_the_subject(self):
        self.ask()
        record = questions.open_questions()[0]
        sent = mock.Mock(return_value={"sent": True, "reason": "sent", "message_id": "<ffq.1@example.com>"})
        with mock.patch.object(mailer, "send_question", sent), mock.patch.object(questions, "mail_allowed", lambda: True):
            self.assertEqual(questions.send_pending(self.board), [record["qid"]])
            self.assertEqual(questions.send_pending(self.board), [])
        subject, body = sent.call_args.args[:2]
        self.assertEqual(subject, f"[{record['qid']}] owner_test.pine: it never takes a trade")
        self.assertIn("1. Put it back in the queue as it is", body)
        self.assertIn("reply with a number, or lines like stop_dollars = 300", body)
        self.assertIn("Meanwhile: The script waits in awaitingAnswerStrategies", body)
        again = questions.get(record["qid"])
        self.assertTrue(again["sent_at"])
        self.assertEqual(again["message_id"], "<ffq.1@example.com>")
        self.assertIn(f"Emailed {record['qid']} for the Chart researcher (owner_test.pine).", self.board.texts("Portfolio Manager"))

    def test_a_failed_send_waits_longer_each_time_and_is_posted_once(self):
        self.ask()
        record = questions.open_questions()[0]
        failed = mock.Mock(return_value={"sent": False, "reason": "SMTP_PASSWORD is not set."})
        with mock.patch.object(mailer, "send_question", failed), mock.patch.object(questions, "mail_allowed", lambda: True), \
                mock.patch.object(questions, "WORDING_WAIT_SECONDS", 0):
            start = 1_000_000.0
            self.assertEqual(questions.send_pending(self.board, now=start), [])
            # A minute later the next try still waits (two minutes after the first failure).
            self.assertEqual(questions.send_pending(self.board, now=start + 60), [])
            self.assertEqual(failed.call_count, 1)
            self.assertEqual(questions.send_pending(self.board, now=start + 121), [])
            self.assertEqual(failed.call_count, 2)
            # The second failure waits four minutes.
            self.assertEqual(questions.send_pending(self.board, now=start + 121 + 200), [])
            self.assertEqual(failed.call_count, 2)
        posts = [text for text in self.board.texts("Portfolio Manager") if "were not emailed" in text]
        self.assertEqual(len(posts), 1)
        self.assertEqual(questions.get(record["qid"])["send_failures"], 2)

    def test_several_waiting_questions_go_out_as_one_digest(self):
        self.ask()
        other = self.learning / "second.pine"
        other.write_text(PINE, encoding="utf-8")
        self.ask(kind="unrunnable", reason="The script stopped in PineTS.", path=other)
        sent = mock.Mock(return_value={"sent": True, "message_id": "<d@x>"})
        with mock.patch.object(mailer, "send_question", sent), mock.patch.object(questions, "mail_allowed", lambda: True):
            ids = questions.send_pending(self.board)
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(len(ids), 2)
        subject, body = sent.call_args.args[:2]
        for qid in ids:
            self.assertIn(qid, subject)
            self.assertIn(qid, body)
        self.assertIn("2 questions", subject)

    def test_send_question_sets_the_message_id_and_reply_to(self):
        env = {"SMTP_USER": "desk@gmail.com", "REPORT_FROM": "Futures Desk <reports@fund.example>", "SMTP_PASSWORD": "x", "FUTURES_MAIL_OFF": "1"}
        with mock.patch.dict(os.environ, env), TemporaryDirectory() as tmp, mock.patch.object(mailer, "MAIL_LOG", Path(tmp) / "_sent.json"):
            headers = mailer.question_headers()
            result = mailer.send_question("[FFQ-abcdef] x: y", "body")
        self.assertTrue(headers["Message-ID"].startswith("<"))
        self.assertEqual(headers["Reply-To"], "desk@gmail.com")
        # FUTURES_MAIL_OFF: nothing left the desk, and the attempt is logged.
        self.assertFalse(result["sent"])
        self.assertTrue(result["message_id"])


class _FakeImap:
    def __init__(self, messages: dict[bytes, bytes]):
        self.messages = messages
        self.seen: list[bytes] = []
        self.logged_in = False

    def __call__(self, host, port):
        self.host, self.port = host, port
        return self

    def login(self, user, password):
        self.logged_in = True
        return "OK", [b"Logged in"]

    def select(self, folder):
        self.folder = folder
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        if command == "FETCH":
            return "OK", [(b"1 (RFC822)", self.messages[args[0]])]
        if command == "STORE":
            self.seen.append(args[0])
            return "OK", []
        return "NO", []

    def logout(self):
        return "BYE", []


def _reply(qid: str, text: str, sender="owner@fund.example", auth="mx.google.com; dkim=pass header.i=@fund.example; spf=pass; dmarc=pass") -> bytes:
    message = EmailMessage()
    message["From"] = f"Owner <{sender}>"
    message["To"] = "desk@gmail.com"
    message["Subject"] = f"Re: [{qid}] owner_test.pine: it never takes a trade"
    message["Message-ID"] = f"<reply-{qid}@fund.example>"
    if auth:
        message["Authentication-Results"] = auth
    message.set_content(text + "\n\nOn Fri, Nov 6, 2026 at 9:00 PM Futures Desk <desk@gmail.com> wrote:\n> The Chart researcher has a question\n> 1. Put it back\n")
    return message.as_bytes()


class InboxTests(_Desk):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"REPORT_TO": "owner@fund.example", "SMTP_USER": "desk@gmail.com", "SMTP_PASSWORD": "app-password", "QUESTION_FROM": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_a_reply_is_read_from_the_mailbox_marked_seen_and_applied(self):
        self.ask()
        qid = questions.open_questions()[0]["qid"]
        fake = _FakeImap({b"7": _reply(qid, "retry")})
        with mock.patch("imaplib.IMAP4_SSL", fake):
            self.assertEqual(questions.poll_inbox(self.board), 1)
            # The same message is never used twice.
            self.assertEqual(questions.poll_inbox(self.board), 0)
        self.assertEqual((fake.host, fake.port, fake.folder), ("imap.gmail.com", 993, "INBOX"))
        self.assertEqual(fake.seen, [b"7", b"7"])
        record = questions.get(qid)
        self.assertEqual(record["status"], "applied")
        self.assertEqual(record["answer_text"], "retry")
        self.assertEqual(record["answer_from"], "owner@fund.example")
        self.assertTrue((self.learning / "owner_test.pine").is_file())
        self.assertFalse((self.waiting / "owner_test.pine").exists())
        self.assertIn(f"{qid} answered — passing it to the Chart researcher.", self.board.texts("Portfolio Manager"))

    def test_a_sender_off_the_allow_list_is_refused(self):
        self.ask()
        qid = questions.open_questions()[0]["qid"]
        with mock.patch("imaplib.IMAP4_SSL", _FakeImap({b"1": _reply(qid, "park", sender="stranger@evil.example")})):
            self.assertEqual(questions.poll_inbox(self.board), 0)
        self.assertEqual(questions.get(qid)["status"], "open")
        self.assertTrue((self.waiting / "owner_test.pine").is_file())

    def test_question_from_widens_the_allow_list(self):
        self.ask()
        qid = questions.open_questions()[0]["qid"]
        with mock.patch.dict(os.environ, {"QUESTION_FROM": "phone@fund.example"}):
            result = questions.accept_answer({"from": "phone@fund.example", "subject": f"Re: [{qid}]", "text": "retry"}, self.board)
        self.assertEqual(result["answered"], [qid])

    def test_a_reply_that_fails_dkim_is_refused(self):
        self.ask()
        qid = questions.open_questions()[0]["qid"]
        bad = "mx.google.com; dkim=fail header.i=@fund.example; spf=fail; dmarc=fail"
        with mock.patch("imaplib.IMAP4_SSL", _FakeImap({b"1": _reply(qid, "park", auth=bad)})):
            self.assertEqual(questions.poll_inbox(self.board), 0)
        self.assertEqual(questions.get(qid)["status"], "open")
        self.assertFalse(questions.authenticated("mx; dkim=fail; spf=softfail"))
        self.assertTrue(questions.authenticated("mx; dkim=pass"))
        self.assertTrue(questions.authenticated(None))

    def test_only_the_top_header_counts_and_a_pass_must_be_for_the_senders_domain(self):
        owner = "owner@fund.example"
        # The sender's own server passing SPF for its own domain, with no DMARC, is not the owner.
        self.assertFalse(questions.authenticated("mx.google.com; spf=pass smtp.mailfrom=bounce@forger.example; dmarc=none", owner))
        self.assertTrue(questions.authenticated("mx.google.com; dkim=pass header.i=@fund.example; dmarc=none", owner))
        self.assertTrue(questions.authenticated("mx.google.com; spf=pass smtp.mailfrom=bounce@mail.fund.example", owner))
        self.assertTrue(questions.authenticated("mx.google.com; dkim=fail; dmarc=pass", owner))
        # A dkim=pass header the sender added below the receiving server's header is ignored.
        forged = ["mx.google.com; dkim=none; spf=softfail; dmarc=none", "fake; dkim=pass header.d=fund.example"]
        self.assertFalse(questions.authenticated(forged, owner))

    def test_a_login_failure_is_told_once_to_the_systems_administrator(self):
        self.ask()
        questions.open_questions()[0]["sent_at"] = "now"

        class Refuses(_FakeImap):
            def login(self, user, password):
                import imaplib

                raise imaplib.IMAP4.error("Application-specific password required")

        with mock.patch("imaplib.IMAP4_SSL", Refuses({})):
            questions.poll_inbox(self.board)
            questions.poll_inbox(self.board)
        told = self.board.texts("Systems Administrator")
        self.assertEqual(len(told), 1)
        self.assertIn("Turn IMAP on in Gmail", told[0])

    def test_the_real_mailbox_is_never_opened_from_a_test_run(self):
        self.ask()
        self.assertIs(questions.imaplib.IMAP4_SSL, questions._REAL_IMAP)
        self.assertEqual(questions.poll_inbox(self.board), 0)

    def test_the_quoted_email_is_stripped(self):
        text = "stop_dollars = 300\n\nOn Fri, Nov 6, 2026 at 9:00 PM Futures Desk <desk@gmail.com>\nwrote:\n> 1. Put it back\n> https://desk.trycloudflare.com"
        self.assertEqual(questions.strip_quoted(text), "stop_dollars = 300")
        self.assertEqual(questions.strip_quoted("park\n> quoted line\n-----Original Message-----\nFrom: desk"), "park")

    def test_a_digest_reply_answers_each_question_by_its_id(self):
        self.ask()
        other = self.learning / "second.pine"
        other.write_text(PINE, encoding="utf-8")
        self.ask(kind="unrunnable", reason="The script stopped in PineTS.", path=other)
        first, second = [row["qid"] for row in questions.open_questions()]
        result = questions.accept_answer({
            "from": "owner@fund.example",
            "subject": f"Re: [{first} {second}] 2 questions from the research desk",
            "text": f"{first}: retry\n{second.upper()}: park this one is broken\n\nOn Fri wrote:\n> old",
        }, self.board)
        self.assertEqual(sorted(result["answered"]), sorted([first, second]))
        self.assertEqual(questions.get(first)["status"], "applied")
        self.assertEqual(questions.get(second)["status"], "parked")
        self.assertTrue((self.cannot / "second.pine").is_file())
        self.assertIn("this one is broken", (self.cannot / "second.reason.txt").read_text(encoding="utf-8"))

    def test_in_reply_to_finds_the_question_without_an_id_in_the_subject(self):
        self.ask()
        record = questions.open_questions()[0]
        record["message_id"] = "<ffq.42@gmail.com>"
        result = questions.accept_answer({"from": "owner@fund.example", "subject": "Re: your question", "text": "1", "in_reply_to": "<ffq.42@gmail.com>"}, self.board)
        self.assertEqual(result["answered"], [record["qid"]])


class ParseTests(_Desk):
    def setUp(self):
        super().setUp()
        self.ask(kind="over_trades", reason="It took 1,400 trades in the year.")
        self.record = questions.open_questions()[0]

    def test_numbers_park_retry_params_scripts_and_free_text(self):
        parse = lambda text: questions.parse_answer(self.record, text)
        self.assertEqual(parse("2"), {"action": "params", "option": 2, "raw": {"desk_max_trades_day": "1"}})
        self.assertEqual(parse("3")["action"], "retry")
        self.assertEqual(parse("4")["action"], "park")
        self.assertEqual(parse("9")["action"], "unclear")
        self.assertEqual(parse("Park it, it overtrades"), {"action": "park", "reason": "it overtrades"})
        self.assertEqual(parse("retry"), {"action": "retry"})
        self.assertEqual(parse("stop_dollars = 300\ndesk_daily_loss: 400"), {"action": "params", "raw": {"stop_dollars": "300", "desk_daily_loss": "400"}})
        self.assertEqual(parse("SETTING: length = 21")["raw"], {"length": "21"})
        pasted = parse("Here you go\n//@version=6\nstrategy(\"Fixed\")\nplot(close)")
        self.assertEqual(pasted["action"], "script")
        self.assertTrue(pasted["script"].startswith("//@version=6"))
        self.assertEqual(parse("please make it trade less on the open")["action"], "free")
        self.assertEqual(parse(""), {"action": "unclear"})

    def test_values_are_checked_against_the_script_inputs(self):
        params, rejected = questions.validate(self.record, {"stop_dollars": "300", "length": "500", "nope": "1", "desk_max_trades_day": "2", "pv": "5"})
        self.assertEqual(params, {"stop_dollars": 300, "desk_max_trades_day": 2})
        self.assertEqual(len(rejected), 3)
        self.assertTrue(any("above its maximum" in line for line in rejected))
        self.assertTrue(any("no input named nope" in line for line in rejected))


class ApplyTests(_Desk):
    def test_params_are_kept_in_the_notes_and_the_script_goes_back_to_its_folder(self):
        self.ask()
        qid = questions.open_questions()[0]["qid"]
        result = questions.answer(qid, "stop_dollars = 300\nlength = 21\nlength2 = 5", "owner@fund.example", board=self.board, run_now=True)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(self.script.is_file())
        self.assertFalse((self.waiting / "owner_test.question.txt").exists())
        notes = json.loads((self.notes / "owner_test.json").read_text(encoding="utf-8"))
        self.assertEqual(notes["owner_answers"]["params"], {"stop_dollars": 300, "length": 21})
        self.assertEqual(questions.owner_settings(self.script), {"stop_dollars": 300, "length": 21})
        said = " ".join(self.board.texts("Chart researcher"))
        self.assertIn("I set stop_dollars = 300, length = 21", said)
        self.assertIn("Not used: the script has no input named length2", said)
        self.assertIn(f"{qid} is applied", " ".join(self.board.texts("Portfolio Manager")))

    def test_a_resume_clears_the_holds_and_counts_for_that_file(self):
        self.ask()
        qid = questions.open_questions()[0]["qid"]
        learn.hold_script(self.script, "5m")
        learn.park_engine_script(self.script, "2m")
        learn._repair_tries[str(self.script)] = 3
        learn._flat_runs[learn._flat_key(self.script, "ES|5m")] = 19
        self.addCleanup(learn.clear_holds)
        questions.answer(qid, "1", "owner@fund.example", board=self.board, run_now=True)
        with learn._held_lock:
            self.assertFalse(any(key[0] == str(self.script) for key in learn._held_back | learn._engine_parked))
        self.assertNotIn(str(self.script), learn._repair_tries)
        self.assertNotIn(learn._flat_key(self.script, "ES|5m"), learn._flat_runs)

    def test_park_sends_the_script_to_cannot_run(self):
        self.ask()
        qid = questions.open_questions()[0]["qid"]
        questions.answer(qid, "park", "owner@fund.example", board=self.board, run_now=True)
        self.assertEqual(questions.get(qid)["status"], "parked")
        self.assertTrue((self.cannot / "owner_test.pine").is_file())
        self.assertFalse((self.waiting / "owner_test.pine").exists())
        self.assertFalse((self.waiting / "owner_test.question.txt").exists())

    def test_a_pasted_script_replaces_the_file_and_the_original_is_kept(self):
        self.ask(kind="unrunnable", reason="Invalid session specification: 180.")
        qid = questions.open_questions()[0]["qid"]
        fixed = PINE.replace("Owner Test", "Owner Test Fixed")
        questions.answer(qid, "Fixed it:\n" + fixed, "owner@fund.example", board=self.board, run_now=True)
        self.assertEqual(questions.get(qid)["status"], "applied")
        self.assertIn("Owner Test Fixed", self.script.read_text(encoding="utf-8"))
        original = self.root / "futuresfund" / "researchNotes" / "script_originals" / "owner_test.pine"
        self.assertIn('strategy("Owner Test"', original.read_text(encoding="utf-8"))

    def test_free_text_goes_to_the_researcher_model_then_one_follow_up(self):
        self.ask(kind="unrunnable", reason="The script stopped in PineTS.")
        qid = questions.open_questions()[0]["qid"]
        with mock.patch.object(questions, "_model_reply", lambda *args, **kwargs: "SETTING: length = 30"):
            questions.answer(qid, "Use a slower average, maybe thirty bars.", "owner@fund.example", board=self.board, run_now=True)
        self.assertEqual(questions.get(qid)["status"], "applied")
        self.assertEqual(questions.owner_settings(self.script), {"length": 30})

        self.script.write_text(PINE, encoding="utf-8")
        self.ask(kind="no_trades")
        other = next(row["qid"] for row in questions.open_questions() if row["kind"] == "no_trades")
        questions.answer(other, "I am not sure, what do you think?", "owner@fund.example", board=self.board, run_now=True)
        record = questions.get(other)
        self.assertEqual(record["status"], "open")
        self.assertEqual(record["followups"], 1)
        self.assertIn("reply with a number", record["followup_pending"])
        sent = mock.Mock(return_value={"sent": True, "message_id": "<f@x>"})
        with mock.patch.object(mailer, "send_question", sent), mock.patch.object(questions, "mail_allowed", lambda: True):
            questions.send_pending(self.board)
        self.assertIn("Follow-up", sent.call_args.args[0])
        questions.answer(other, "still not sure", "owner@fund.example", board=self.board, run_now=True)
        record = questions.get(other)
        self.assertEqual(record["followups"], 1)
        self.assertEqual(record["followup_pending"], "")

    def test_words_for_a_compile_repair_become_the_developers_hint(self):
        self.ask(kind="repair", reason="It did not compile: Undeclared identifier 'foo'.")
        qid = questions.open_questions()[0]["qid"]
        questions.answer(qid, "foo should be the fast EMA, declare it as ta.ema(close, 9).", "owner@fund.example", board=self.board, run_now=True)
        self.assertEqual(questions.get(qid)["status"], "applied")
        self.assertIn("ta.ema(close, 9)", questions.repair_hint(self.script))
        self.assertEqual(questions.repair_hint(self.script), "")

    def test_an_answered_script_reopens_its_finished_charts(self):
        (self.notes / "owner_test.json").write_text(json.dumps({"file": "owner_test.pine", "instruments": {"ES": {"charts": {"5m": {"trials": [{"trades": 3}]}}}}}), encoding="utf-8")
        self.ask(kind="repair", reason="It did not compile.")
        qid = questions.open_questions()[0]["qid"]
        with mock.patch.object(learn, "REOPEN_FOR_ANSWERS", False), mock.patch.object(learn, "READ_BEFORE_TESTING", False):
            questions.answer(qid, "tp_dollars = 600", "owner@fund.example", board=self.board, run_now=True)
        notes = json.loads((self.notes / "owner_test.json").read_text(encoding="utf-8"))
        self.assertNotIn("instruments", notes)
        self.assertEqual(notes["owner_answers"]["params"], {"tp_dollars": 600})
        self.assertTrue(list((self.notes / "before_repair").glob("owner_test_owner_*.json")))


class SearchTests(_Desk):
    def _first_params(self, base):
        seen = []

        def execute(params):
            seen.append(dict(params))
            return {"net_profit": 100.0, "max_drawdown": 50.0, "trades": 40}

        facts = learn.pine_facts(PINE)
        learn._search(base, execute, 50000, 1, None, self.script, None, facts, extras=False, developer="5 min chart developer")
        return seen[0]

    def test_the_owner_values_start_every_charts_search_after_prepare_research(self):
        questions._store_owner_params(self.script.name, {"stop_dollars": 300, "daily_loss_limit": 400.0}, "FFQ-aaaaaa")
        base = {"stop_dollars": 250, "tp_dollars": 500, "length": 14, "daily_loss_limit": 0.0}
        for _chart in range(2):
            first = self._first_params(dict(base))
            # prepare_research would zero the daily loss limit; the owner's 400 holds on every chart.
            self.assertEqual(first["stop_dollars"], 300)
            self.assertEqual(first["daily_loss_limit"], 400.0)

    def test_the_owner_values_survive_an_attempt_recorded_on_another_chart(self):
        questions._store_owner_params(self.script.name, {"stop_dollars": 300}, "FFQ-cccccc")
        learn._remember_chart_attempt(
            self.script, {}, {"timeframe": "5m", "instrument": "ES", "net_profit": 10.0, "max_drawdown": 5.0, "trades": 3}, 1,
        )
        self.assertEqual(questions.owner_settings(self.script), {"stop_dollars": 300})
        notes = learn._read_notes(self.script)
        self.assertIn("ES", notes["instruments"])
        first = self._first_params({"stop_dollars": 250, "tp_dollars": 500, "length": 14})
        self.assertEqual(first["stop_dollars"], 300)

    def test_a_session_or_plain_text_answer_is_accepted_and_reaches_the_engine(self):
        from futuresfund.backtrader_engine import script_as_run
        from futuresfund.strategy import _apply_inputs

        script = PINE + 'sess = input.session("0930-1600", "Session")\nmode = input.string("abc", "Mode")\n'
        self.script.write_text(script, encoding="utf-8")
        self.ask()
        record = questions.open_questions()[0]
        params, rejected = questions.validate(record, {"sess": "0800-1600", "mode": "xyz", "length": "21"})
        self.assertEqual(params, {"sess": "0800-1600", "mode": "xyz", "length": 21})
        self.assertEqual(rejected, [])
        params, rejected = questions.validate(record, {"sess": "8am"})
        self.assertEqual(params, {})
        self.assertIn("is not a session like 0930-1600", rejected[0])
        applied = _apply_inputs(script, {"sess": "0800-1600", "mode": "xyz"})
        self.assertIn('sess = input.session("0800-1600", "Session")', applied)
        self.assertIn('mode = input.string("xyz", "Mode")', applied)
        self.assertNotIn('"0930-1600"', applied)
        self.assertIn('mode = input.string("xyz"', script_as_run(script, {"mode": "xyz"}, "ES"))

    def test_the_owner_session_value_starts_the_search(self):
        script = PINE + 'sess = input.session("0930-1600", "Session")\n'
        self.script.write_text(script, encoding="utf-8")
        questions._store_owner_params(self.script.name, {"sess": "0800-1600"}, "FFQ-dddddd")
        seen = []

        def execute(params):
            seen.append(dict(params))
            return {"net_profit": 100.0, "max_drawdown": 50.0, "trades": 40}

        execute.source = {"text": script, "stamp": 0}
        learn._search({"stop_dollars": 250, "tp_dollars": 500, "length": 14}, execute, 50000, 1, None, self.script, None,
                      learn.pine_facts(script), extras=False, developer="5 min chart developer")
        self.assertEqual(seen[0].get("sess"), "0800-1600")

    def test_a_desk_limit_answer_installs_the_desk_trade_limits_in_the_run_copy(self):
        from futuresfund.strategy_team import TRADE_LIMITS_MARKER

        questions._store_owner_params(self.script.name, {"desk_max_trades_day": 2}, "FFQ-bbbbbb")
        holder = {"text": PINE, "stamp": 0}
        seen = []

        def execute(params):
            seen.append(dict(params))
            return {"net_profit": 100.0, "max_drawdown": 50.0, "trades": 40}

        execute.source = holder
        learn._search({"stop_dollars": 250, "tp_dollars": 500, "length": 14}, execute, 50000, 1, None, self.script, None,
                      learn.pine_facts(PINE), extras=False, developer="5 min chart developer")
        self.assertIn(TRADE_LIMITS_MARKER, holder["text"])
        self.assertEqual(holder["stamp"], 1)
        self.assertTrue(holder["owner_applied"])
        self.assertEqual(seen[0]["desk_max_trades_day"], 2)
        self.assertTrue(holder["trade_cut"]["touched"])


class LoopTests(_Desk):
    def test_the_engine_loop_takes_the_next_script_after_awaiting(self):
        source = Path(learn.__file__).read_text(encoding="utf-8")
        self.assertIn('if studied in ("cannot_run", "awaiting"):', source)
        self.assertIn("awaiting", learn._END_REASONS)

    def test_a_chart_whose_file_went_to_awaiting_says_so(self):
        self.ask()
        text = learn._left_text(self.script)
        self.assertEqual(text, "owner_test.pine waits in awaitingAnswerStrategies for the owner's answer. This chart left it.")


if __name__ == "__main__":
    unittest.main()
