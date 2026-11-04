"""A separate command. The ordinary run does not call PineTS or open chartData."""

import os
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]


@unittest.skipUnless(os.environ.get("RESEARCH_LIVE_PINETS") == "1", "live PineTS check is a separate command")
class LiveEngineTests(unittest.TestCase):
    def test_a_finished_backtest_arrives_on_the_engine_card(self):
        from research_department.client import ResearchClient
        from research_department.http_server import DepartmentServer
        from research_department.service import ResearchDesk

        desk = ResearchDesk()
        server = DepartmentServer(desk)
        try:
            finished = desk.run_sample()
            seen = ResearchClient(server.url).engines()["engines"]
        finally:
            server.close()
        card = next(item for item in seen if item["id"] == finished["id"])
        self.assertEqual(card["timeframe"], "5m")
        self.assertEqual(card["script"], "Research sample")
        self.assertTrue(card["free"])
        self.assertEqual(card["result"]["days"], 3)
        self.assertGreaterEqual(card["result"]["trade_count"], 1)
        self.assertIn("success_rate", card["result"])
