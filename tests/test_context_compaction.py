"""Research context stays inside the fast window, and a desk compacts onto the lessons file."""

import unittest
from unittest.mock import patch

from futuresfund import learn, llm
from futuresfund.llm import (
    COMPACT_AT,
    CONTEXT_CAP,
    answer_chat,
    client_for,
    complete,
    message_tokens,
    note_result,
)


MEASURED = {
    "script": "Pivot Hold",
    "chart": "2-minute ES",
    "change": "stop_mult 1 to 2",
    "net_profit": 2825,
    "max_drawdown": 3775,
    "trades": 463,
    "impact": "Move stop_mult further in this direction.",
}


def _desk(name):
    client = client_for(name)
    client.history.clear()
    client.latest_result = None
    llm._chat_history.pop(name, None)
    return client


class ContextCompactionTests(unittest.TestCase):
    def setUp(self):
        self.folder = __import__("tempfile").TemporaryDirectory()
        self.notes = patch.object(learn, "NOTES", __import__("pathlib").Path(self.folder.name))
        self.notes.start()
        self.system = patch("futuresfund.llm._system", lambda agent: "Desk system.")
        self.system.start()
        _desk("Chart researcher")
        _desk("15 min strategy developer")

    def tearDown(self):
        self.system.stop()
        self.notes.stop()
        self.folder.cleanup()

    def test_research_context_uses_the_fast_window_and_not_the_native_window(self):
        seen = {}

        def fake_post(model, messages, **kwargs):
            seen["num_ctx"] = kwargs.get("num_ctx")
            return "kept"

        with patch("futuresfund.llm._post", fake_post):
            complete("short research question", agent="Chart researcher", num_ctx=262144)
        self.assertEqual(seen["num_ctx"], CONTEXT_CAP)
        self.assertEqual(CONTEXT_CAP, 40960)
        self.assertLess(CONTEXT_CAP, 262144)

        with patch("futuresfund.llm._post", fake_post):
            complete("another short question", agent="Chart researcher", num_ctx=30720)
        self.assertEqual(seen["num_ctx"], CONTEXT_CAP)

    def test_chat_stays_on_the_small_window(self):
        seen = {}

        def fake_post(model, messages, **kwargs):
            seen["num_ctx"] = kwargs.get("num_ctx")
            return "hello"

        with patch("futuresfund.llm._post", fake_post):
            answer_chat("Chart researcher", "What is the desk doing?")
        self.assertEqual(seen["num_ctx"], 4096)

    def test_history_stays_clipped_until_the_prompt_crosses_20000_tokens(self):
        client = _desk("Chart researcher")
        client.history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn-{i}-" + ("x" * 3000)}
            for i in range(12)
        ]
        messages = client._messages("next question")
        priors = [item for item in messages if item["content"].startswith("turn-")]
        self.assertEqual([item["content"][:7] for item in priors], ["turn-8-", "turn-9-", "turn-10", "turn-11"])
        self.assertTrue(all(len(item["content"]) <= 1500 for item in priors))

        sent = []

        def fake_post(model, messages, **kwargs):
            sent.append("\n".join(item["content"] for item in messages))
            return "short reply"

        with patch("futuresfund.llm._post", fake_post):
            complete("a short follow up", agent="Chart researcher")
        self.assertIn("turn-11", sent[0])
        self.assertLessEqual(len(client.history), 8)
        self.assertIn("a short follow up", client.history[-2]["content"])

    def test_a_long_prompt_appends_one_measured_lesson_and_clears_that_desk(self):
        researcher = _desk("Chart researcher")
        developer = _desk("15 min strategy developer")
        researcher.history = [
            {"role": "user", "content": "OLD-TURN-MARKER"},
            {"role": "assistant", "content": "The profit is 999999 from memory."},
        ]
        developer.history = [{"role": "user", "content": "DEVELOPER-STAYS"}]
        llm._chat_history["Chart researcher"] = [{"role": "user", "content": "CHAT-OLD"}]
        llm._chat_history["15 min strategy developer"] = [{"role": "user", "content": "CHAT-STAYS"}]
        guide = learn.NOTES / "how_to_research.md"
        guide.write_text("guide stays", encoding="utf-8")
        note = learn.NOTES / "pivot_hold.json"
        note.write_text('{"trials": []}', encoding="utf-8")
        sent = []

        def fake_post(model, messages, **kwargs):
            sent.append(messages)
            return "next script"

        prompt = "NEW-MEASUREMENT " + ("bar " * 30000)
        with patch("futuresfund.llm._post", fake_post):
            complete(prompt, agent="Chart researcher", result=MEASURED)
        self.assertGreater(message_tokens([{"content": prompt}]), COMPACT_AT)
        body = "\n".join(item["content"] for item in sent[0])
        self.assertNotIn("OLD-TURN-MARKER", body)
        self.assertNotIn("999999", body)
        self.assertLessEqual(message_tokens(sent[0]), COMPACT_AT)
        self.assertEqual(sent[0][0]["role"], "system")
        self.assertEqual(researcher.history[0]["content"][:15], "NEW-MEASUREMENT")
        self.assertEqual(developer.history[0]["content"], "DEVELOPER-STAYS")
        self.assertNotIn("Chart researcher", llm._chat_history)
        self.assertEqual(llm._chat_history["15 min strategy developer"][0]["content"], "CHAT-STAYS")

        lessons = (learn.NOTES / "LESSONS.md").read_text(encoding="utf-8")
        self.assertEqual(lessons.count("Profit 2825"), 1)
        self.assertIn("Pivot Hold on the 2-minute ES: stop_mult 1 to 2.", lessons)
        self.assertIn("Drawdown 3775. Trades 463.", lessons)
        self.assertIn("Move stop_mult further in this direction.", lessons)
        self.assertNotIn("999999", lessons)
        self.assertNotIn("OLD-TURN-MARKER", lessons)
        self.assertEqual(guide.read_text(encoding="utf-8"), "guide stays")
        self.assertEqual(note.read_text(encoding="utf-8"), '{"trials": []}')
        self.assertIn("Profit 2825", learn.notes_digest())

        with patch("futuresfund.llm._post", fake_post):
            complete(prompt, agent="Chart researcher", result=dict(MEASURED))
        self.assertEqual((learn.NOTES / "LESSONS.md").read_text(encoding="utf-8").count("Profit 2825"), 1)

    def test_the_same_result_already_in_the_notes_is_not_appended(self):
        learn.NOTES.mkdir(parents=True, exist_ok=True)
        (learn.NOTES / "LESSONS.md").write_text(
            "Pivot Hold changed stop_mult 1 to 2 with profit 2825 drawdown 3775 trades 463.\n",
            encoding="utf-8",
        )
        self.assertIsNone(learn.append_window_lesson(MEASURED))
        self.assertEqual(
            (learn.NOTES / "LESSONS.md").read_text(encoding="utf-8").count("\n"),
            1,
        )

    def test_a_window_without_a_measurement_appends_nothing(self):
        researcher = _desk("Chart researcher")
        researcher.history = [{"role": "user", "content": "OLD-TURN-MARKER"}]

        def fake_post(model, messages, **kwargs):
            return "nothing measured"

        prompt = "only words " + ("bar " * 30000)
        with patch("futuresfund.llm._post", fake_post):
            complete(prompt, agent="Chart researcher")
        self.assertFalse((learn.NOTES / "LESSONS.md").exists())
        self.assertEqual(researcher.history[0]["content"][:10], "only words")
        self.assertIsNone(learn.append_window_lesson({"script": "Pivot Hold", "change": "stop 1 to 2"}))

    def test_the_engine_result_is_remembered_for_the_desk_that_ran_it(self):
        learn._remember_measured_lesson(
            {"title": "Pivot Hold"},
            learn.NOTES / "pivot.pine",
            {
                "change": "stop_mult 1 to 2",
                "net_profit": 2825,
                "max_drawdown": 3775,
                "trades": 463,
                "timeframe": "2m",
                "instrument": "ES",
                "impact": "Move stop_mult further in this direction.",
            },
            "Chart researcher",
            "2min chart developer",
        )
        stored = client_for("Chart researcher").latest_result
        self.assertEqual(stored["net_profit"], 2825)
        self.assertEqual(stored["trades"], 463)
        self.assertEqual(client_for("2min chart developer").latest_result["change"], "stop_mult 1 to 2")
        note_result("Chart researcher", {"script": "Other", "net_profit": 1})
        self.assertEqual(client_for("Chart researcher").latest_result["script"], "Other")


class LessonSummaryTests(unittest.TestCase):
    def setUp(self):
        self.folder = __import__("tempfile").TemporaryDirectory()
        self.notes = patch.object(learn, "NOTES", __import__("pathlib").Path(self.folder.name))
        self.notes.start()
        learn.NOTES.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.notes.stop()
        self.folder.cleanup()

    def _fill(self, name, profit):
        path = learn.NOTES / "LESSONS.md"
        line = (
            f"{name} on the 2-minute ES: stop 1 to 2. "
            f"Profit {profit}. Drawdown 50. Trades 140. "
            "Next: Try one different input on this chart.\n"
        )
        with path.open("a", encoding="utf-8") as handle:
            while path.stat().st_size <= llm.CONTEXT_CAP * 4:
                handle.write(line)
        return path

    def test_a_small_lesson_file_is_left_alone_and_not_emailed(self):
        path = learn.NOTES / "LESSONS.md"
        path.write_text("Pivot Hold on the 2-minute ES: stop 1 to 2. Profit 10. Drawdown 4. Trades 130. Next: wait.\n", encoding="utf-8")
        sent = []
        with patch("futuresfund.mailer.send_report", lambda body, subject="Futures desk report": sent.append(subject) or {"sent": True}):
            self.assertFalse(learn.maintain_lessons())
        self.assertEqual(sent, [])
        self.assertFalse((learn.NOTES / "LESSONS_SUMMARY.md").exists())

    def test_a_script_keeps_its_best_measured_lesson_and_drops_the_attempt_table(self):
        path = learn.NOTES / "LESSONS.md"
        weaker = (
            "Pivot Hold: attempts 12, best profit 400, drawdown 900, trades 80. "
            "Single changes that beat the original on profit and drawdown: stop 2 to 1.\n"
        )
        stronger = (
            "Pivot Hold: attempts 40, best profit 2800, drawdown 700, trades 160. "
            "Single changes that beat the original on profit and drawdown: stop 1 to 2.\n"
        )
        table = "stop 1 to 2                                        $400.00     $900.00          80     no\n"
        path.write_text((weaker + table) * 4000 + stronger, encoding="utf-8")
        before = path.stat().st_size
        sent = []
        with patch("futuresfund.mailer.send_report", lambda body, subject="Futures desk report": sent.append(body) or {"sent": True}):
            self.assertTrue(learn.maintain_lessons())
        kept = path.read_text(encoding="utf-8")
        self.assertLess(path.stat().st_size, before)
        self.assertIn("best profit 2800", kept)
        self.assertIn("stop 1 to 2", kept)
        self.assertNotIn("best profit 400", kept)
        self.assertNotIn("$400.00", kept)
        self.assertEqual(len(sent), 1)
        self.assertIn("best measured lesson", sent[0])

    def test_oversized_lessons_are_summarized_and_the_portfolio_manager_emails(self):
        path = self._fill("Sample Script", 100)
        original = path.read_bytes()
        guide = learn.NOTES / "how_to_research.md"
        guide.write_text("do not touch", encoding="utf-8")
        sent = []

        def fake_send(body, subject="Futures desk report"):
            sent.append((subject, body))
            return {"sent": True, "reason": "sent"}

        with patch("futuresfund.mailer.send_report", fake_send):
            self.assertTrue(learn.maintain_lessons())
            self.assertFalse(learn.maintain_lessons())
        self.assertLess(path.stat().st_size, len(original))
        self.assertEqual(path.read_text(encoding="utf-8").count("Profit 100"), 1)
        self.assertEqual(guide.read_text(encoding="utf-8"), "do not touch")
        summary = (learn.NOTES / "LESSONS_SUMMARY.md").read_text(encoding="utf-8")
        self.assertLess(len(summary), len(original))
        self.assertIn("Profit 100", summary)
        self.assertNotIn("Profit 999999", summary)
        self.assertEqual(len(sent), 1)
        subject, body = sent[0]
        self.assertIn("too large", subject.lower())
        self.assertIn("Portfolio Manager", body)
        self.assertIn("performance", body.lower())
        self.assertIn("summar", body.lower())
        self.assertIn("28672", body)
        self.assertNotIn("999999", body)

        learn.append_window_lesson({
            "script": "Fresh Script",
            "chart": "5-minute NQ",
            "change": "cooldown 3 to 8",
            "net_profit": 4400,
            "max_drawdown": 900,
            "trades": 210,
            "impact": "Change a different input.",
        })
        digest = learn.notes_digest()
        self.assertIn("Profit 100", digest)
        self.assertIn("Fresh Script on the 5-minute NQ: cooldown 3 to 8.", digest)
        self.assertIn("Profit 4400. Drawdown 900. Trades 210.", digest)

        extra = "Grown Script on the 2-minute ES: stop 2 to 3. Profit 15. Drawdown 6. Trades 150. Next: wait.\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(extra * ((llm.CONTEXT_CAP * 4 // len(extra)) + 2))
        swollen = path.stat().st_size
        with patch("futuresfund.mailer.send_report", fake_send):
            self.assertTrue(learn.maintain_lessons())
        self.assertEqual(len(sent), 2)
        self.assertLess(path.stat().st_size, swollen)
        kept = path.read_text(encoding="utf-8")
        self.assertIn("Profit 100", kept)
        self.assertIn("Profit 15", kept)
        self.assertEqual(kept.count("Profit 15"), 1)


if __name__ == "__main__":
    unittest.main()
