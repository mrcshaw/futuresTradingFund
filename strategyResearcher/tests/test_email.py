import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from test_learning import fake_ask, good_days, pine

from research_department.department import Job
from research_department.http_server import DepartmentServer
from research_department.inputs import ScriptInput
from research_department.meeting import Proposal
from research_department.service import raises_delta_while_a_session_is_empty, trade_note
from research_department.mail import ADDRESS, MailService, smtp_send
from research_department.service import ResearchDesk
from research_department.stages import Day


PASSED = {
    "script": "One",
    "instrument": "ES",
    "timeframe": "5m",
    "change": "watch_bars 8",
    "justification": "A longer watch makes the $500 day more consistent and raises the win percentage.",
    "days": 365,
    "success_rate": 1,
    "win_percentage": 0.8,
    "worst_day": 100,
    "trade_count": 40,
    "attempt": 1,
}


class EmailServiceTests(unittest.TestCase):
    def test_a_passed_strategy_is_addressed_to_the_futures_gmail_with_the_phone_url(self):
        sent = []

        def transport(message):
            sent.append(message)
            return {"sent": True, "to": message["to"], "reason": "sent"}

        mail = MailService(transport=transport, phone=lambda: "https://research.example.trycloudflare.com")
        result = mail.send_success(PASSED)
        self.assertTrue(result["sent"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], ADDRESS)
        self.assertEqual(sent[0]["to"], "thefutureoffuturestrading@gmail.com")
        self.assertIn("One", sent[0]["body"])
        self.assertIn("watch_bars 8", sent[0]["body"])
        self.assertIn("100%", sent[0]["body"])
        self.assertIn("https://research.example.trycloudflare.com", sent[0]["body"])
        self.assertIn("Open the research department on your phone", sent[0]["body"])

    def test_the_research_manager_sends_when_a_strategy_is_kept(self):
        sent = []
        desk = ResearchDesk()
        desk.mail = MailService(
            transport=lambda message: sent.append(message) or {"sent": True, "to": message["to"], "reason": "sent"},
            phone=lambda: "https://phone.example/research",
        )
        result = desk.deliver(dict(PASSED), None)
        self.assertTrue(result["sent"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], "thefutureoffuturestrading@gmail.com")
        self.assertIn("https://phone.example/research", sent[0]["body"])

    def test_an_empty_password_does_not_open_a_connection(self):
        previous = os.environ.get("SMTP_PASSWORD")
        os.environ["SMTP_PASSWORD"] = ""
        os.environ.setdefault("SMTP_USER", "desk@gmail.com")
        os.environ.setdefault("REPORT_FROM", "desk@gmail.com")

        def connect(*_args, **_kwargs):
            raise AssertionError("SMTP was opened")

        try:
            result = smtp_send(
                {"to": ADDRESS, "subject": "Research manager: One passed", "body": "kept"},
                connect=connect,
                load=False,
            )
        finally:
            if previous is None:
                os.environ.pop("SMTP_PASSWORD", None)
            else:
                os.environ["SMTP_PASSWORD"] = previous
        self.assertFalse(result["sent"])
        self.assertIn("thefutureoffuturestrading@gmail.com", result["reason"])
        self.assertIn("SMTP_PASSWORD is empty", result["reason"])


class AttemptTests(unittest.TestCase):
    def test_an_empty_session_asks_for_a_lower_volume_delta(self):
        inputs = [ScriptInput("min_delta_pct", "float", 7.0), ScriptInput("stop_dollars", "int", 250.0)]
        last = {"success_rate": 0.0, "win_percentage": 0.0, "worst_day": 0, "trade_count": 2, "quiet_sessions": 5, "days": 7}
        note = trade_note(inputs, last)
        self.assertIn("Lower min_delta_pct from 7", note)
        self.assertIn("5 of 7 sessions had no trade", note)
        self.assertIn("profit 0", note)
        order = Proposal("min_delta_pct", 9, "up", True, True, "Raising the volume delta filters trades.")
        self.assertTrue(raises_delta_while_a_session_is_empty(order, inputs, last))
        lower = Proposal("min_delta_pct", 4, "down", True, True, "A lower volume delta gives every session a trade.")
        self.assertFalse(raises_delta_while_a_session_is_empty(lower, inputs, last))

    def test_the_attempt_limit_moves_the_file_and_a_pass_keeps_the_other_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            learning = root / "learning"
            learning.mkdir()
            source = learning / "flat_ES.pine"
            source.write_text(pine("Flat"), encoding="utf-8")

            def backtest(_source, _instrument, _timeframe, count):
                return [Day(0, 500, 0, 0) for _ in range(count)]

            desk = ResearchDesk(folder=learning)
            desk.settings["attempts"] = 1
            desk.settings["success_rate"] = 0.5
            desk.ask = fake_ask
            desk.backtest = backtest
            server = DepartmentServer(desk)
            try:
                self._send(server, "POST", "/worker/start")
                moved = root / "did not produce profits" / "flat_ES.pine"
                deadline = time.time() + 8
                while time.time() < deadline and not moved.is_file():
                    time.sleep(0.05)
                self.assertTrue(moved.is_file())
                self.assertFalse(source.exists())
                self.assertFalse((root / "completed strategies").exists())
            finally:
                desk.stop_worker()
                if desk._worker_thread is not None:
                    desk._worker_thread.join(timeout=5)
                server.close()

    def _send(self, server, method: str, path: str) -> dict:
        request = Request(server.url + path, data=b"{}", method=method, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


class MeetingFeedTests(unittest.TestCase):
    def test_a_missing_justification_keeps_the_meeting_open_until_a_choice(self):
        desk = ResearchDesk()
        desk.jobs["job-1"] = Job("job-1", "source", "ES", ["meeting"])
        desk.shown = "job-1"
        calls = {"n": 0}

        def ask(prompt):
            calls["n"] += 1
            if calls["n"] > 12:
                raise AssertionError("the meeting did not decide")
            if prompt.startswith("Pick the one"):
                return '{"choice": 0}'
            if calls["n"] <= 3:
                return "I have not justified it yet."
            return json.dumps({
                "variable": "watch_bars",
                "value": 5,
                "direction": "up",
                "consistent_day": True,
                "higher_win_rate": True,
                "justification": "A longer watch makes the $500 day more consistent and raises the win percentage.",
            })

        desk.ask = ask
        order, _proposals = desk._meet("Sample", [ScriptInput("watch_bars", "int", 4.0)], set())
        self.assertIsNotNone(order)
        self.assertEqual(order.variable, "watch_bars")
        self.assertGreater(calls["n"], 4)
        self.assertEqual(desk.jobs["job-1"].states, ["meeting"])
        feed = desk.jobs["job-1"].feed
        self.assertIn("win percentage", feed["proposals"][0]["spoken"])
        self.assertTrue(any(item["who"].startswith("Researcher") for item in feed["activity"]))
        self.assertIn("picked", feed["manager"])

    def test_a_running_engine_shows_the_work_and_then_the_result(self):
        desk = ResearchDesk()
        desk.engines.start(2, "Sample")
        card = next(item for item in desk.engines.cards() if item["id"] == 2)
        self.assertFalse(card["free"])
        self.assertEqual(card["status"], "Working")
        self.assertEqual(card["script"], "Sample")
        desk.engines.complete(2, {
            "days": 7,
            "success_rate": 1,
            "win_percentage": 1,
            "worst_day": 100,
            "trade_count": 3,
        })
        finished = next(item for item in desk.engines.cards() if item["id"] == 2)
        self.assertEqual(finished["status"], "Finished")
        self.assertEqual(finished["result"]["trade_count"], 3)
        self.assertEqual(finished["result"]["days"], 7)

    def test_one_decision_is_run_on_all_three_engines(self):
        seen = []
        desk = ResearchDesk()

        def backtest(_source, instrument, timeframe, count):
            seen.append((instrument, timeframe))
            return good_days(count)

        desk.backtest = backtest
        desk.ask = fake_ask
        _status, body = desk.post_script(pine("One"), "ES")
        job_id = body["job_id"]
        deadline = time.time() + 5
        state = ""
        while time.time() < deadline:
            state = desk.jobs[job_id].states[-1]
            if state == "passed_all":
                break
            time.sleep(0.02)
        self.assertEqual(state, "passed_all")
        self.assertEqual(seen[:9], [
            ("ES", "2m"), ("NQ", "2m"), ("MNQ", "2m"),
            ("ES", "5m"), ("NQ", "5m"), ("MNQ", "5m"),
            ("ES", "1m"), ("NQ", "1m"), ("MNQ", "1m"),
        ])
        self.assertNotIn("GC", {item[0] for item in seen})
        self.assertEqual(len(desk.engine_runs[1]), 12)
        self.assertLessEqual(len(desk.engine_runs[1]), 20)
        self.assertEqual(len(desk.successes[7]), 9)
        self.assertEqual(len(desk.successes[30]), 9)
        self.assertEqual(len(desk.successes[90]), 9)
        self.assertEqual(len(desk.successes[365]), 9)
        self.assertEqual({run["profit"] for run in desk.successes[7]}, {3500})
        self.assertEqual({run["profit"] for run in desk.successes[30]}, {15000})
        self.assertEqual({run["profit"] for run in desk.successes[90]}, {45000})
        self.assertEqual({run["profit"] for run in desk.successes[365]}, {182500})
        self.assertTrue(all(run["profit"] in {3500, 15000, 45000, 182500} for run in desk.engine_runs[1]))
        self.assertIn("Measuring the script before any change.", desk.jobs[job_id].feed["lines"])
        self.assertNotIn("Researcher 1", desk.jobs[job_id].feed.get("chats", {}))

    def test_a_researcher_page_keeps_the_thought_in_paragraphs(self):
        desk = ResearchDesk()
        desk.jobs["job-1"] = Job("job-1", "source", "ES", ["meeting"])
        desk.shown = "job-1"
        desk._working("Researcher 1", "First thought about the day.\n\nSecond thought about the wins.", commit=False)
        desk._working("Researcher 1", '{"variable":"pv","value":25}', stream_kind="content")
        desk._working(
            "Researcher 1",
            "The $500 day is more consistent.\n\nThe win percentage is higher.",
            commit=True,
        )
        messages = desk.jobs["job-1"].feed["chats"]["Researcher 1"]
        self.assertEqual(messages[0]["kind"], "thinking")
        self.assertIn("\n\n", messages[0]["text"])
        self.assertEqual(messages[-1]["kind"], "reply")
        self.assertNotIn("pv", messages[0]["text"])
        activity = desk.jobs["job-1"].feed["activity"][0]["text"]
        self.assertIn("First thought", activity)
        self.assertNotIn("\n", activity)

    def test_the_page_shows_the_working_feed(self):
        root = Path(__file__).resolve().parents[1] / "research_department" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        researcher = (root / "researcher.html").read_text(encoding="utf-8")
        library = (root / "library.html").read_text(encoding="utf-8")
        js = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="person-line"', html)
        self.assertIn("Scripts library", html)
        self.assertIn('href="/library"', html)
        self.assertIn('id="thread"', researcher)
        self.assertIn(">Delete<", library)
        self.assertNotIn("<dialog", html + researcher + library)
        self.assertNotIn("chat-modal", html + js)
        self.assertIn("ES, NQ, MNQ", html)
        self.assertIn(">1m<", html)
        self.assertNotIn(">15m<", html)
        self.assertIn("last 20", js)
        self.assertIn("365 days", js)
        self.assertIn("Working", js)
        self.assertNotIn("/scripts", js + html + researcher + library)
        self.assertNotIn("/result", js + html + researcher + library)
