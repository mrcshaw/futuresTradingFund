"""Each desk has its own model. A log written for one desk stays off the others."""

import json
import unittest
from unittest.mock import patch

from futuresfund.board import Board
from futuresfund.crew import Crew
from futuresfund.llm import client_for, complete, model_name
from futuresfund.roster import ROSTER


class AgentModelTests(unittest.TestCase):
    def test_every_desk_has_a_different_model(self):
        names = [model_name(agent["name"]) for agent in ROSTER]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 17)
        self.assertEqual(model_name("Portfolio Manager"), "desk-portfolio")
        self.assertEqual(model_name("2min chart developer"), "desk-developer")
        self.assertEqual(model_name("15 min chart developer"), "desk-developer-15")
        self.assertEqual(model_name("Chart researcher"), "desk-researcher")
        self.assertEqual(model_name("Strategy developer"), "desk-strategy-developer")
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

        def fake_post(model, messages):
            sent.append((model, [item["content"] for item in messages if item["role"] == "user"]))
            return f"reply from {model}"

        with patch("futuresfund.llm._post", fake_post):
            first = complete("researcher question", agent="Chart researcher")
            second = complete("script question", agent="Strategy developer")
        self.assertEqual(first, "reply from desk-researcher")
        self.assertEqual(second, "reply from desk-strategy-developer")
        self.assertEqual(sent[0][0], "desk-researcher")
        self.assertEqual(sent[1][0], "desk-strategy-developer")
        researcher = client_for("Chart researcher")
        developer = client_for("Strategy developer")
        self.assertIn("researcher question", json.dumps(researcher.history))
        self.assertNotIn("script question", json.dumps(researcher.history))
        self.assertIn("script question", json.dumps(developer.history))
        self.assertNotIn("researcher question", json.dumps(developer.history))


if __name__ == "__main__":
    unittest.main()
