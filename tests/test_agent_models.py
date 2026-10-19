"""Each desk has its own model. A log written for one desk stays off the others."""

import json
import threading
import unittest
from unittest.mock import patch

from futuresfund.board import Board
from futuresfund.crew import Crew
from futuresfund.llm import MOE_BASE, answer_chat, client_for, complete, model_name, runner_model
from futuresfund.roster import ROSTER


class AgentModelTests(unittest.TestCase):
    def setUp(self):
        self._maintain = patch("futuresfund.llm.maintain_lessons", lambda: None)
        self._maintain.start()

    def tearDown(self):
        self._maintain.stop()

    def test_every_desk_has_a_different_model(self):
        names = [model_name(agent["name"]) for agent in ROSTER]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 19)
        self.assertEqual(model_name("Portfolio Manager"), "desk-portfolio")
        self.assertEqual(model_name("2min chart developer"), "desk-developer")
        self.assertEqual(model_name("15 min chart developer"), "desk-developer-15")
        self.assertEqual(model_name("Chart researcher"), "desk-researcher")
        self.assertEqual(model_name("2min strategy developer"), "desk-strategy-developer-2")
        self.assertEqual(model_name("5 min strategy developer"), "desk-strategy-developer-5")
        self.assertEqual(model_name("15 min strategy developer"), "desk-strategy-developer-15")
        self.assertEqual(model_name("Creation tester"), "desk-creation-tester")

    def test_a_desk_log_is_not_copied_onto_the_others(self):
        board = Board()
        crew = Crew(board)
        line = "Starting the engine on the 2-minute chart for EMA Pullback"
        crew.run("2min chart developer", line, lambda: None)
        logs = board.snapshot()["agent_logs"]
        self.assertEqual(logs["2min chart developer"][-1], line)
        for name, rows in logs.items():
            if name != "2min chart developer":
                self.assertNotIn(line, rows)

    def test_two_desks_keep_separate_histories_and_models(self):
        sent = []

        def fake_post(model, messages, **kwargs):
            sent.append((model, [item["content"] for item in messages if item["role"] == "user"], kwargs.get("num_ctx")))
            return f"reply from {model}"

        with patch("futuresfund.llm._post", fake_post):
            first = complete("researcher question", agent="Chart researcher")
            second = complete("script question", agent="15 min strategy developer")
        self.assertEqual(first, "reply from desk-researcher")
        self.assertEqual(second, "reply from desk-strategy-developer-15")
        self.assertEqual(sent[0][0], "desk-researcher")
        self.assertEqual(sent[1][0], "desk-strategy-developer-15")
        researcher = client_for("Chart researcher")
        developer = client_for("15 min strategy developer")
        self.assertIn("researcher question", json.dumps(researcher.history))
        self.assertNotIn("script question", json.dumps(researcher.history))
        self.assertIn("script question", json.dumps(developer.history))
        self.assertNotIn("researcher question", json.dumps(developer.history))

    def test_every_desk_runs_on_the_qwen38_dense_model(self):
        from futuresfund.llm import _BASES, _post

        self.assertTrue(all(base == MOE_BASE for base in _BASES.values()))
        self.assertIn("qwen3.8", MOE_BASE)
        self.assertIn("27b", MOE_BASE)
        self.assertEqual(runner_model("desk-portfolio"), runner_model("desk-strategy-developer-15"))
        self.assertIn("qwen38", runner_model("desk-researcher"))
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"message": {"content": "kept the lower drawdown"}}).encode()

        def fake_open(request, timeout=0):
            captured["body"] = json.loads(request.data.decode())
            return Response()

        with patch("urllib.request.urlopen", fake_open):
            reply = _post("desk-researcher", [{"role": "user", "content": "Which result ranks first?"}])
        self.assertEqual(reply, "kept the lower drawdown")
        self.assertEqual(captured["body"]["model"], runner_model("desk-researcher"))
        self.assertFalse(captured["body"]["think"])
        with patch("urllib.request.urlopen", fake_open):
            _post("desk-researcher", [{"role": "user", "content": "Which result ranks first?"}], think=True)
        self.assertEqual(captured["body"]["think"], "max")

    def test_the_systems_administrator_chat_uses_thinking(self):
        captured = {}

        def fake_post(model, messages, **kwargs):
            captured["think"] = kwargs.get("think")
            captured["model"] = model
            return "Engine 3 is waiting for the next script."

        from futuresfund import llm

        llm._chat_history.pop("Systems Administrator", None)
        with patch("futuresfund.llm._post", fake_post):
            reply = answer_chat("Systems Administrator", "What is the desk doing?")
        self.assertEqual(reply, "Engine 3 is waiting for the next script.")
        self.assertTrue(captured["think"])
        self.assertEqual(captured["model"], model_name("Systems Administrator"))

    def test_a_strategy_developer_is_told_to_keep_drawdown_first(self):
        messages = client_for("15 min strategy developer")._messages("Write the next script", fresh=True)
        system = messages[0]["content"]
        self.assertIn("Qwen3.8 27B", system)
        self.assertIn("dense model", system)
        self.assertIn("The trade count is on as well.", system)
        self.assertIn("strategy library page", system)
        self.assertIn("their boxes are checked", system)
        self.assertIn("Do not invent a profit", system)
        self.assertIn("larger than the stop", system)
        self.assertIn("more than 3 percent of the account", system)

    def test_two_desks_do_not_wait_on_each_other(self):
        barrier = threading.Barrier(2)
        errors = []

        def fake_post(model, messages, **kwargs):
            barrier.wait(timeout=2)
            return f"done {model}"

        def run(agent, prompt):
            try:
                complete(prompt, agent=agent)
            except Exception as exc:
                errors.append(exc)

        with patch("futuresfund.llm._post", fake_post):
            first = threading.Thread(target=run, args=("Chart researcher", "rank the drawdown"))
            second = threading.Thread(target=run, args=("15 min strategy developer", "write the exit"))
            first.start()
            second.start()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])

    def test_the_harness_reads_the_objective_before_it_answers(self):
        calls = {"n": 0}

        def fake_post(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return "TOOL objective"
            self.assertEqual(model, "desk-strategy-developer-15")
            self.assertIn("meet the trade count", messages[-1]["content"])
            return "The scale goal is a win rate of 60 percent and a profit factor above 1.1."

        from futuresfund import llm

        llm._chat_history.pop("15 min strategy developer", None)
        with patch("futuresfund.llm._post", fake_post):
            reply = answer_chat("15 min strategy developer", "Which stored result should we keep?")
        self.assertEqual(calls["n"], 2)
        self.assertIn("win rate of 60 percent", reply)
        self.assertNotIn("$", reply)

    def test_a_reasoned_answer_cannot_introduce_a_profit(self):
        calls = {"n": 0}

        def fake_post(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return "TOOL objective"
            return "Keep the lower drawdown. The profit is $999,999."

        from futuresfund import llm

        llm._chat_history.pop("Chart researcher", None)
        with patch("futuresfund.llm._post", fake_post):
            reply = answer_chat("Chart researcher", "What should the next script optimize?")
        self.assertNotIn("999,999", reply)
        self.assertIn("not in the desk records", reply)

    def test_the_portfolio_manager_harness_can_carry_out_a_task(self):
        calls = {"n": 0}
        seen = {}

        def fake_post(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return "TOOL task: start the researchers"
            seen["result"] = messages[-1]["content"]
            return "Started the researchers."

        def fake_task(board, asked):
            seen["asked"] = asked
            return "Started the researchers."

        from futuresfund import llm

        llm._chat_history.pop("Portfolio Manager", None)
        with patch("futuresfund.llm._post", fake_post), patch("futuresfund.desk_chat._task", fake_task):
            reply = answer_chat("Portfolio Manager", "Start research.", board=Board())
        self.assertEqual(seen["asked"], "start the researchers")
        self.assertIn("Started the researchers", seen["result"])
        self.assertEqual(reply, "Started the researchers.")

    def test_another_desk_cannot_place_a_task_or_an_order(self):
        calls = {"n": 0}

        def fake_post(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return "TOOL task: start the researchers"
            self.assertIn("does not place orders", messages[-1]["content"])
            return "I cannot start that from this desk."

        from futuresfund import llm

        llm._chat_history.pop("Floor Trader", None)
        with patch("futuresfund.llm._post", fake_post):
            reply = answer_chat("Floor Trader", "Start research and buy ES.")
        self.assertEqual(reply, "I cannot start that from this desk.")
        self.assertNotIn("buy", reply.lower())


if __name__ == "__main__":
    unittest.main()
