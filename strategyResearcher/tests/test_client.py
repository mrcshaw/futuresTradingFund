import sys
import unittest
from pathlib import Path
from urllib.request import urlopen

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from research_department.client import ResearchClient
from research_department.department import Department
from research_department.http_server import DepartmentServer


FEED = {
    "proposals": [
        {"justification": "watch_bars keeps the break inside the trend, so more days finish at $500 and more of those trades win."},
        {"justification": "A smaller doji cut keeps real signal candles, which steadies the day and lifts the win percentage."},
        {"justification": "A steeper EMA angle skips flat breaks, so the remaining trades win more often and the days repeat."},
    ],
    "manager": "watch_bars 6. The days get more consistent and the win percentage rises.",
}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.department = Department()
        self.server = DepartmentServer(self.department)
        self.client = ResearchClient(self.server.url)

    def tearDown(self):
        self.server.close()

    def test_the_feed_shows_the_three_justifications_and_the_manager_line(self):
        self.department.prepare(["meeting"], FEED)
        self.department.post_script("//@version=5\nstrategy(\"Five\")\n", "MNQ")
        feed = self.client.feed("job-1")
        self.assertEqual([item["justification"] for item in feed["proposals"]], [item["justification"] for item in FEED["proposals"]])
        self.assertEqual(feed["manager"], FEED["manager"])

    def test_the_engine_card_fills_in_when_the_backtest_completes(self):
        added = self.department.engines.add("5m")
        self.department.engines.start(added.id, "five.pine")
        cards = self.client.engines()["engines"]
        self.assertEqual(len(cards), 4)
        self.assertIsNone(cards[-1]["result"])
        self.department.engines.complete(added.id, {"days": 7, "success_rate": 1.0, "win_percentage": 0.6})
        filled = self.client.engines()["engines"][-1]
        self.assertEqual(filled["script"], "five.pine")
        self.assertEqual(filled["result"]["success_rate"], 1.0)
        self.assertTrue(filled["free"])

    def test_saved_settings_come_back_from_the_page(self):
        self.assertEqual(self.client.settings()["success_rate"], 0.60)
        self.assertEqual(self.client.settings()["attempts"], 20)
        saved = self.client.save_settings(success_rate=0.7, attempts=12, baseline_risk=250)
        self.assertEqual(saved["success_rate"], 0.7)
        self.assertEqual(saved["attempts"], 12)
        self.assertEqual(self.client.settings()["daily_target"], 500)
        self.assertEqual(self.client.settings()["daily_loss"], 400)

    def test_the_page_does_not_load_a_script_or_take_the_result(self):
        root = Path(__file__).resolve().parents[1] / "research_department" / "static"
        page = "".join(
            (root / name).read_text(encoding="utf-8")
            for name in ("index.html", "researcher.html", "library.html", "app.js")
        )
        self.assertNotIn("/scripts", page)
        self.assertNotIn("/result", page)

    def test_researcher_and_library_are_their_own_pages(self):
        for path in ("/library", "/researcher?who=Researcher%201"):
            with urlopen(self.server.url + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                body = response.read().decode("utf-8")
                self.assertIn("<!DOCTYPE html>", body)
                self.assertNotIn("<dialog", body)

    def test_the_client_does_not_load_a_script_or_take_the_result(self):
        self.department.prepare(["passed_all"], FEED, "strategy(\"Passed\")")
        self.department.post_script("//@version=5\nstrategy(\"Five\")\n", "ES")
        self.client.feed("job-1")
        self.client.engines()
        self.client.settings()
        for method, path in self.client.calls:
            self.assertNotEqual((method, path), ("POST", "/scripts"))
            self.assertFalse(path.endswith("/result"))
