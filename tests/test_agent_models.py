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
        self.assertEqual(len(names), 11)
        self.assertEqual(model_name("Portfolio Manager"), "desk-portfolio")
        self.assertEqual(model_name("Quantitative Developer"), "desk-developer")
        self.assertNotEqual(model_name("Quantitative Researcher"), model_name("Indicator Researcher"))

    def test_a_desk_log_is_not_copied_onto_the_others(self):
        board = Board()
        crew = Crew(board)
        line = "Starting the engine on the 2-minute chart for EMA Pullback"
        crew.run("Quantitative Developer", line, lambda: None)
        logs = board.snapshot()["agent_logs"]
        self.assertEqual(logs["Quantitative Developer"][-1], line)
        for name, rows in logs.items():
            if name != "Quantitative Developer":
                self.assertNotIn(line, rows)

    def test_two_desks_keep_separate_histories_and_models(self):
        sent = []

        def fake_post(model, messages):
            sent.append((model, [item["content"] for item in messages if item["role"] == "user"]))
            return f"reply from {model}"

        with patch("futuresfund.llm._post", fake_post):
            first = complete("researcher question", agent="Quantitative Researcher")
            second = complete("indicator question", agent="Indicator Researcher")
        self.assertEqual(first, "reply from desk-researcher")
        self.assertEqual(second, "reply from desk-indicator")
        self.assertEqual(sent[0][0], "desk-researcher")
        self.assertEqual(sent[1][0], "desk-indicator")
        researcher = client_for("Quantitative Researcher")
        indicator = client_for("Indicator Researcher")
        self.assertIn("researcher question", json.dumps(researcher.history))
        self.assertNotIn("indicator question", json.dumps(researcher.history))
        self.assertIn("indicator question", json.dumps(indicator.history))
        self.assertNotIn("researcher question", json.dumps(indicator.history))


if __name__ == "__main__":
    unittest.main()
