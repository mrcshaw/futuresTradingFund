"""An engine with no file open alerts the systems administrator.

Engine 4 studies the learning folder while Hold is on.
"""

from __future__ import annotations

import unittest

from futuresfund.board import Board
from futuresfund.engine_watch import GRACE_SECONDS, check, explain_engines, reset_incidents, supply_check


def _engine(engine_id, developer, *, busy=False, studying=False, script="", study=""):
    return {
        "id": engine_id,
        "developer": developer,
        "busy": busy,
        "studying": studying,
        "script": script,
        "study": study,
    }


class EngineWatchTests(unittest.TestCase):
    def setUp(self):
        reset_incidents()
        self.board = Board()
        self.sent = []
        self.cleared = []
        self.asked = []

    def _check(self, engines, status, *, research=True, hold=False, now=0.0, ask=None, lock_held=None):
        return check(
            self.board,
            engines=engines,
            agent_status=status,
            research_running=research,
            hold_new=hold,
            send=lambda body, subject: self.sent.append((subject, body)),
            ask=ask,
            now=now,
            clear=lambda index: self.cleared.append(index) or True,
            lock_held=lock_held,
        )

    def test_hold_marks_engine_4_idle_and_emails_one_incident(self):
        self.board.agent_status["Creation tester"] = "working"
        engines = [_engine(4, "Creation tester")]
        status = {"Creation tester": "working", "Systems Administrator": "idle"}

        def ask(issue):
            self.asked.append(issue["engine"])
            return "Hold left the working flag on. Clear it."

        first = self._check(engines, status, hold=True, ask=ask)
        second = self._check(engines, status, hold=True, now=10, ask=ask)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["action"], "idle")
        self.assertFalse(first[0]["fault"])
        self.assertEqual(second, [])
        self.assertEqual(self.asked, [4])
        self.assertEqual(self.board.agent_status["Creation tester"], "idle")
        self.assertEqual(self.board.agent_status["Systems Administrator"], "idle")
        self.assertEqual(len(self.sent), 1)
        subject, body = self.sent[0]
        self.assertIn("Engine 4 is not processing a file", subject)
        self.assertIn("Hold is on", body)
        self.assertIn("Clear it.", body)
        self.assertTrue(any(message["kind"] == "alert" for message in self.board.messages))

    def test_a_chart_developer_between_scripts_is_not_an_incident(self):
        found = self._check(
            [_engine(2, "5 min chart developer")],
            {"5 min chart developer": "working"},
            now=GRACE_SECONDS + 30,
        )
        self.assertEqual(found, [])
        self.assertEqual(self.sent, [])

    def test_an_idle_engine_4_on_hold_is_not_an_incident(self):
        found = self._check(
            [_engine(4, "Creation tester")],
            {"Creation tester": "idle"},
            hold=True,
        )
        self.assertEqual(found, [])
        self.assertEqual(self.sent, [])

    def test_a_chart_engine_without_a_file_waits_then_alerts_once(self):
        self.board.agent_status["5 min chart developer"] = "working"
        engines = [_engine(2, "5 min chart developer", busy=True)]
        status = {"5 min chart developer": "working"}
        self.assertEqual(self._check(engines, status, now=0), [])
        self.assertEqual(self._check(engines, status, now=GRACE_SECONDS - 1), [])
        found = self._check(engines, status, now=GRACE_SECONDS, ask=lambda issue: "The flag was left on.")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["fault"])
        self.assertEqual(self.cleared, [1])
        self.assertEqual(self.board.agent_status["5 min chart developer"], "idle")
        self.assertIn("The flag was left on.", self.sent[0][1])
        self.assertEqual(self._check(engines, status, now=GRACE_SECONDS + 30), [])
        self.assertEqual(len(self.sent), 1)

    def test_a_live_backtest_is_not_an_incident(self):
        engines = [_engine(1, "2min chart developer", busy=True)]
        found = self._check(
            engines,
            {"2min chart developer": "working"},
            now=GRACE_SECONDS + 5,
            lock_held=lambda index: True,
        )
        self.assertEqual(found, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.cleared, [])

    def test_an_engine_processing_a_file_is_not_an_incident(self):
        found = self._check(
            [_engine(1, "2min chart developer", busy=True, script="created_breakout.pine")],
            {"2min chart developer": "working"},
            now=GRACE_SECONDS + 5,
        )
        self.assertEqual(found, [])
        self.assertEqual(self.sent, [])

    def test_research_stopped_does_not_alert(self):
        found = self._check(
            [_engine(3, "15 min chart developer", studying=True)],
            {"15 min chart developer": "working"},
            research=False,
            now=GRACE_SECONDS + 5,
        )
        self.assertEqual(found, [])
        self.assertEqual(self.sent, [])

    def test_a_model_failure_still_sends_the_report(self):
        def ask(issue):
            raise RuntimeError("model down")

        found = self._check(
            [_engine(4, "Creation tester")],
            {"Creation tester": "working"},
            hold=True,
            ask=ask,
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Engine 4 incident.", self.sent[0][1])

    def test_the_systems_administrator_thinks(self):
        from futuresfund.llm import _reasons

        self.assertTrue(_reasons("Systems Administrator"))
        self.assertTrue(_reasons("Creation tester"))

    def test_hold_keeps_engine_4_on_the_learning_folder(self):
        from pathlib import Path
        from unittest import mock

        from futuresfund import learn
        from futuresfund.backtrader_engine import _ENGINE_STATUS, finish_study
        from futuresfund.strategy_team import CREATION_ENGINE, CREATION_TESTER, run_creation_engine

        path = Path("learningStrategies") / "created_sample.pine"
        taken = []

        def take(timeframe):
            taken.append(timeframe)
            return path if timeframe == "2m" and path not in taken else None

        def release(file, timeframe):
            return None

        previous = (learn._job_take, learn._job_release)
        learn._job_take = take
        learn._job_release = release
        try:
            with mock.patch.object(learn, "_study", return_value="studied") as study:
                found = learn.study_learning_on_engine_4(self.board, None)
            self.assertEqual(found, "studied")
            self.assertEqual(taken[0], "2m")
            study.assert_called_once()
            self.assertEqual(_ENGINE_STATUS[CREATION_ENGINE]["study"], path.name)
            self.assertEqual(self.board.agent_status[CREATION_TESTER], "working")
            self.assertIn(path.name, self.board.agent_tasks[CREATION_TESTER])
            learn._job_take = lambda timeframe: None
            self.assertEqual(learn.study_learning_on_engine_4(self.board, None), "none")
            self.assertEqual(self.board.agent_status[CREATION_TESTER], "idle")
        finally:
            learn._job_take, learn._job_release = previous
            finish_study(CREATION_ENGINE)

        waits = {"left": 1}

        def wait(board, pause):
            waits["left"] -= 1
            return waits["left"] >= 0

        self.board.set_hold_new(True)
        with mock.patch("futuresfund.learn.engine4_should_study_learning", return_value=False), \
             mock.patch("futuresfund.learn.study_learning_on_engine_4", return_value="studied") as study, \
             mock.patch("futuresfund.learn._wait", side_effect=wait), \
             mock.patch("futuresfund.strategy_team.develop_one") as develop:
            run_creation_engine(self.board, None)
        study.assert_called_once()
        develop.assert_not_called()

    def test_clear_idle_engine_leaves_a_held_lock_alone(self):
        from futuresfund import backtrader_engine as engines

        lock = engines._ENGINE_LOCKS[3]
        self.assertTrue(lock.acquire(blocking=False))
        row = engines._ENGINE_STATUS[3]
        previous = (row.get("busy"), row.get("script"), row.get("studying"), row.get("study"))
        try:
            row["busy"] = True
            row["script"] = ""
            row["studying"] = True
            row["study"] = ""
            self.assertFalse(engines.clear_idle_engine(3))
            self.assertTrue(row["busy"])
        finally:
            row["busy"], row["script"], row["studying"], row["study"] = previous
            lock.release()
        row["busy"] = True
        row["script"] = ""
        row["studying"] = True
        row["study"] = ""
        try:
            self.assertTrue(engines.clear_idle_engine(3))
            self.assertFalse(row["busy"])
            self.assertFalse(row["studying"])
        finally:
            row["busy"] = False
            row["script"] = ""
            row["studying"] = False
            row["study"] = ""

    def _idle_engines(self):
        return [
            _engine(1, "2min chart developer"),
            _engine(2, "5 min chart developer"),
            _engine(3, "15 min chart developer"),
            _engine(4, "Creation tester"),
        ]

    def test_an_empty_learning_folder_moves_created_scripts_and_emails_once(self):
        moved = []

        def move(names):
            moved.extend(names)
            return list(names)

        released = []
        kwargs = dict(
            research_running=True,
            hold_new=True,
            learning_count=0,
            created_names=["created_a.pine"],
            engines=self._idle_engines(),
            send=lambda body, subject: self.sent.append((subject, body)),
            move=move,
            release=lambda: released.append(True),
        )
        found = supply_check(self.board, **kwargs)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["action"], "move")
        self.assertEqual(moved, ["created_a.pine"])
        self.assertEqual(released, [])
        self.assertIn("created_a.pine", self.sent[0][1])
        self.assertEqual(supply_check(self.board, **kwargs), [])
        self.assertEqual(len(self.sent), 1)

    def test_an_empty_created_folder_presses_make_new_strategies(self):
        released = []
        found = supply_check(
            self.board,
            research_running=True,
            hold_new=True,
            learning_count=0,
            created_names=[],
            engines=self._idle_engines(),
            send=lambda body, subject: self.sent.append((subject, body)),
            move=lambda names: [],
            release=lambda: released.append(True),
        )
        self.assertEqual(released, [True])
        self.assertEqual(found[0]["action"], "make")
        self.assertIn("Make new strategies", self.sent[0][1])

    def test_a_busy_engine_is_left_while_a_file_is_open(self):
        found = supply_check(
            self.board,
            research_running=True,
            hold_new=True,
            learning_count=0,
            created_names=["created_a.pine"],
            engines=[_engine(1, "2min chart developer", busy=True, script="created_a.pine")],
            send=lambda body, subject: self.sent.append((subject, body)),
            move=lambda names: names,
            release=lambda: None,
        )
        self.assertEqual(found, [])
        self.assertEqual(self.sent, [])

    def test_restock_moves_a_created_script_into_an_empty_learning_folder(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from futuresfund import learn

        with tempfile.TemporaryDirectory() as tmp:
            learning = Path(tmp) / "learningStrategies"
            created = Path(tmp) / "createdStrategies"
            learning.mkdir()
            created.mkdir()
            (created / "created_band.pine").write_text("//@version=5\nstrategy(\"Band\")\n", encoding="utf-8")
            (created / "created_band.py").write_text("x = 1\n", encoding="utf-8")
            notes = Path(tmp) / "researchNotes"
            notes.mkdir()
            with mock.patch.object(learn, "LEARNING", learning), mock.patch.object(learn, "CREATED", created), mock.patch.object(learn, "NOTES", notes):
                held = learn.restock_learning()
            self.assertEqual(held, [])
            self.assertTrue((created / "created_band.pine").is_file())
            (notes / "created_band.json").write_text(
                '{"best": {"net_profit": 400, "trades": 8, "max_drawdown": 100}}',
                encoding="utf-8",
            )
            with mock.patch.object(learn, "LEARNING", learning), mock.patch.object(learn, "CREATED", created), mock.patch.object(learn, "NOTES", notes):
                moved = learn.restock_learning()
                again = learn.restock_learning()
            self.assertEqual(moved, ["created_band.pine"])
            self.assertEqual(again, [])
            self.assertTrue((learning / "created_band.pine").is_file())
            self.assertTrue((learning / "created_band.py").is_file())
            self.assertFalse((created / "created_band.pine").exists())

    def test_the_systems_administrator_says_why_an_engine_stopped_and_starts_research(self):
        started = []
        text = explain_engines(
            "why is engine 2 not running",
            research_running=False,
            hold_new=True,
            learning_count=3,
            created_count=1,
            engines=[_engine(2, "5 min chart developer")],
            threads_alive={2: True},
            apply=True,
            start=lambda: started.append(True),
            restart=lambda engine_id: None,
            clear=lambda index: None,
            move=lambda: [],
            release=lambda: None,
            send=lambda body, subject: self.sent.append((subject, body)),
            board=self.board,
        )
        self.assertIn("research is stopped", text)
        self.assertIn("started the researchers", text)
        self.assertEqual(started, [True])
        self.assertIn("research is stopped", self.sent[0][1])
        quiet = explain_engines(
            "why is engine 2 not running",
            research_running=False,
            hold_new=True,
            learning_count=3,
            created_count=1,
            engines=[_engine(2, "5 min chart developer")],
            threads_alive={2: True},
            apply=False,
            start=lambda: started.append("again"),
            restart=lambda engine_id: None,
            clear=lambda index: None,
            move=lambda: [],
            release=lambda: None,
            send=lambda body, subject: self.sent.append((subject, body)),
            board=self.board,
        )
        self.assertIn("research is stopped", quiet)
        self.assertNotIn("started the researchers", quiet)
        self.assertEqual(started, [True])
        self.assertEqual(len(self.sent), 1)

    def test_a_dead_engine_worker_is_restarted(self):
        restarted = []
        text = explain_engines(
            "why is engine 1 not running",
            research_running=True,
            hold_new=False,
            learning_count=4,
            created_count=0,
            engines=[_engine(1, "2min chart developer")],
            threads_alive={1: False},
            apply=True,
            start=lambda: None,
            restart=lambda engine_id: restarted.append(engine_id),
            clear=lambda index: None,
            move=lambda: [],
            release=lambda: None,
            send=lambda body, subject: self.sent.append((subject, body)),
            board=self.board,
        )
        self.assertEqual(restarted, [1])
        self.assertIn("worker stopped", text)
        self.assertIn("restarted engine 1", text)

    def test_chat_asks_the_systems_administrator_to_fix_an_engine(self):
        from unittest import mock

        from futuresfund import desk_chat

        with mock.patch("futuresfund.engine_watch.answer_engines", return_value="Engine 2 is idle.") as asked:
            reply = desk_chat.answer(self.board, "Systems Administrator", "why is engine 2 not running?")
            other = desk_chat.answer(self.board, "Portfolio Manager", "why is engine 2 not running?")
        self.assertEqual(reply, "Engine 2 is idle.")
        self.assertEqual(other, "Engine 2 is idle.")
        self.assertTrue(asked.call_args_list[0].kwargs["apply"])
        self.assertFalse(asked.call_args_list[1].kwargs["apply"])
        with mock.patch("futuresfund.engine_watch.answer_engines", return_value="Engine 3 has finished its scripts.") as asked_again:
            reply = desk_chat.answer(self.board, "Systems Administrator", "why isn't engine 3 running")
        self.assertEqual(reply, "Engine 3 has finished its scripts.")
        self.assertTrue(asked_again.call_args.kwargs["apply"])

    def test_an_idle_chart_that_finished_its_scripts_is_explained(self):
        from futuresfund.engine_watch import _engine_ids, engine_issue_asked

        self.assertTrue(engine_issue_asked("why isn't engine 3 running"))
        self.assertTrue(engine_issue_asked("why isn't engine 23 running?"))
        self.assertEqual(_engine_ids("why isn't engine 23 running?"), [2, 3])
        self.board.agent_tasks["15 min chart developer"] = (
            "The 15-minute chart has finished the learning folder and createdStrategies. "
            "The strategy team is not writing a new strategy."
        )
        text = explain_engines(
            "why isn't engine 23 running?",
            research_running=True,
            hold_new=True,
            learning_count=2,
            created_count=1,
            engines=[
                _engine(2, "5 min chart developer", script="night.pine"),
                _engine(3, "15 min chart developer"),
            ],
            threads_alive={2: True, 3: True},
            apply=True,
            start=lambda: None,
            restart=lambda engine_id: None,
            clear=lambda index: None,
            move=lambda: [],
            release=lambda: None,
            send=lambda body, subject: self.sent.append((subject, body)),
            board=self.board,
        )
        self.assertIn("There is no engine 23", text)
        self.assertIn("Engine 2 is running night.pine", text)
        self.assertIn("finished the learning folder", text)
        self.assertIn("not writing a new strategy", text)
        self.assertEqual(self.sent, [])
