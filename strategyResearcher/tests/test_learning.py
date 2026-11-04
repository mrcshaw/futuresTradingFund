import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from research_department.http_server import DepartmentServer
from research_department.loader import LEARNING, learning_files
from research_department.service import ResearchDesk
from research_department.stages import Day


def pine(title: str) -> str:
    return (
        "//@version=5\n"
        f'strategy("{title}", overlay=true)\n'
        'watch_bars = input.int(4, "Max Bars")\n'
    )


def fake_ask(prompt: str) -> str:
    if prompt.startswith("Pick the one"):
        return '{"choice": 0}'
    default = 4
    for line in prompt.splitlines():
        if line.startswith("watch_bars"):
            default = int(float(line.split()[-1]))
    return json.dumps({
        "variable": "watch_bars",
        "value": default + 1,
        "direction": "up",
        "consistent_day": True,
        "higher_win_rate": True,
        "justification": "A longer watch makes the $500 day more repeatable and raises the win percentage.",
    })


def good_days(count: int) -> list[Day]:
    return [Day(500, 100, 1, 1) for _ in range(count)]


def desk_body(url: str) -> dict:
    with urlopen(url + "/desk", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


class LearningWorkerTests(unittest.TestCase):
    def test_the_default_folder_is_the_learning_folder(self):
        desk = ResearchDesk()
        files = learning_files(desk.folder)
        self.assertEqual(desk.folder, LEARNING)
        self.assertTrue(files)
        self.assertTrue(all(path.suffix.lower() in {".pine", ".txt"} for path in files))
        self.assertFalse(any(path.parent.name == "nppBackup" for path in files))

    def test_gold_is_refused_before_a_backtest(self):
        desk = ResearchDesk()
        status, body = desk.post_script(pine("Gold"), "GC")
        self.assertEqual(status, 400)
        self.assertTrue(body["refused"])
        self.assertEqual(desk.jobs, {})

    def test_the_page_starts_and_stops_without_loading_a_script_itself(self):
        root = Path(__file__).resolve().parents[1] / "research_department" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "styles.css").read_text(encoding="utf-8")
        library = (root / "library.html").read_text(encoding="utf-8")
        researcher = (root / "researcher.html").read_text(encoding="utf-8")
        self.assertIn('id="run"', html)
        self.assertIn(">Start<", html)
        self.assertIn("Scripts library", html)
        self.assertIn('href="/library"', html)
        self.assertIn('href="/researcher?who=Researcher%201"', html)
        self.assertIn('class="person-line"', html)
        self.assertNotIn("<dialog", html)
        self.assertNotIn("chat-modal", html)
        self.assertIn(">Delete<", library)
        self.assertIn('id="rows"', library)
        self.assertIn('id="sort-profit"', library)
        self.assertIn("Highest profit is first.", library)
        self.assertIn('id="script-modal"', library)
        self.assertIn('id="copy-script"', library)
        self.assertIn(">Copy<", library)
        self.assertIn("/library/text", js)
        self.assertNotIn("<dialog", library)
        self.assertIn('id="thread"', researcher)
        self.assertNotIn("<dialog", researcher)
        self.assertIn("color-scheme: dark", css)
        self.assertIn('"/worker/start"', js)
        self.assertIn('"/worker/stop"', js)
        self.assertNotIn("/scripts", js + html + library + researcher)
        self.assertNotIn("/result", js + html + library + researcher)
        self.assertIn("Highest profit is first.", js)
        self.assertIn("/catalog/", js)

    def test_the_worker_loads_every_file_and_the_department_finishes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            learning = root / "learning"
            learning.mkdir()
            (learning / "gold_GC.pine").write_text(pine("Gold"), encoding="utf-8")
            (learning / "one_ES.pine").write_text(pine("One"), encoding="utf-8")
            (learning / "two_NQ.pine").write_text(pine("Two"), encoding="utf-8")
            runs = {"n": 0}
            markets = []

            def backtest(source, instrument, timeframe, count):
                runs["n"] += 1
                markets.append((instrument, timeframe))
                if runs["n"] > 72:
                    desk._worker_stop.set()
                return good_days(count)

            desk = ResearchDesk(folder=learning)
            desk.ask = fake_ask
            desk.backtest = backtest
            server = DepartmentServer(desk)
            try:
                self.assertEqual(self._send(server, "POST", "/worker/start")["running"], True)
                deadline = time.time() + 20
                body = {}
                while time.time() < deadline:
                    body = desk_body(server.url)
                    titles = {item["script"] for item in body["strategies"]}
                    if {"One", "Two"} <= titles:
                        break
                    time.sleep(0.02)
                self._send(server, "POST", "/worker/stop")
                desk._worker_thread.join(timeout=5)
                body = desk_body(server.url)
                stored = {item["script"]: item for item in body["strategies"]}
                self.assertIn("One", stored)
                self.assertIn("Two", stored)
                self.assertEqual(stored["One"]["instrument"], "ES, NQ, MNQ")
                self.assertEqual(stored["Two"]["instrument"], "ES, NQ, MNQ")
                self.assertEqual(stored["One"]["days"], 365)
                self.assertEqual(stored["One"]["success_rate"], 1.0)
                self.assertIn("input.int(4", stored["One"]["source"])
                self.assertNotIn("input.int(5", stored["One"]["source"])
                self.assertIn('strategy("One"', stored["One"]["source"])
                passed = {item["file"] for item in desk.accepted if item["state"] == "passed_all"}
                self.assertTrue({"one_ES.pine", "two_NQ.pine"} <= passed)
                self.assertNotIn("gold_GC.pine", passed)
                self.assertEqual({item[0] for item in markets}, {"ES", "NQ", "MNQ"})
                self.assertNotIn("15m", {item[1] for item in markets})
                self.assertIn("1m", {item[1] for item in markets})
                completed = root / "completed strategies"
                self.assertTrue((completed / "one_ES.pine").is_file())
                self.assertTrue((completed / "two_NQ.pine").is_file())
                self.assertTrue((learning / "gold_GC.pine").is_file())
                self.assertFalse((learning / "one_ES.pine").exists())
                self.assertTrue(self._posts_are_serial(desk.loader_calls))
                removed = stored["Two"]["id"]
                self._send(server, "DELETE", f"/strategies/{removed}")
                left = desk_body(server.url)["strategies"]
                self.assertEqual([item["script"] for item in left if item["script"] == "Two"], [])
                self.assertTrue(any(item["script"] == "One" for item in left))
            finally:
                desk.stop_worker()
                if desk._worker_thread is not None:
                    desk._worker_thread.join(timeout=5)
                server.close()

    def test_stop_keeps_the_worker_from_loading_the_next_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one_ES.pine").write_text(pine("One"), encoding="utf-8")
            (root / "two_NQ.pine").write_text(pine("Two"), encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()

            def backtest(source, instrument, timeframe, count):
                if not entered.is_set():
                    entered.set()
                    release.wait(5)
                return good_days(count)

            desk = ResearchDesk(folder=root)
            desk.ask = fake_ask
            desk.backtest = backtest
            server = DepartmentServer(desk)
            try:
                self._send(server, "POST", "/worker/start")
                self.assertTrue(entered.wait(5))
                self._send(server, "POST", "/worker/stop")
                release.set()
                desk._worker_thread.join(timeout=5)
                posts = [call for call in desk.loader_calls if call == ("POST", "/scripts")]
                self.assertEqual(posts, [("POST", "/scripts")])
                self.assertFalse(desk.worker_running)
                self.assertTrue(desk._worker_stop.is_set())
            finally:
                release.set()
                desk.stop_worker()
                if desk._worker_thread is not None:
                    desk._worker_thread.join(timeout=5)
                server.close()

    def _send(self, server, method: str, path: str) -> dict:
        request = Request(server.url + path, data=b"{}", method=method, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _posts_are_serial(self, calls) -> bool:
        waiting = False
        for method, path in calls:
            if (method, path) == ("POST", "/scripts"):
                if waiting:
                    return False
                waiting = True
            elif path.endswith("/result"):
                waiting = False
        return True


class CatalogTests(unittest.TestCase):
    def test_a_finished_backtest_stays_in_the_library_ranked_by_profit(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "strategies.json"
            desk = ResearchDesk(library_path=library)
            desk.current_file = "one.pine"
            weak = {
                "script": "One",
                "instrument": "MNQ",
                "timeframe": "2m",
                "change": "watch_bars 5",
                "days": 7,
                "win_percentage": 0.4,
                "profit": 341,
                "trade_count": 45,
                "worst_day": -479,
                "success_rate": 0,
                "attempt": 1,
            }
            strong = dict(weak)
            strong.update(instrument="NQ", win_percentage=0.22, profit=16015, trade_count=40, worst_day=-1381)
            desk._remember_result(weak)
            desk._remember_result(strong)
            ranked = desk.get_desk()[1]["results"]
            self.assertEqual([row["profit"] for row in ranked], [16015, 341])
            self.assertEqual(ranked[0]["instrument"], "NQ")
            self.assertTrue(ranked[0]["id"].startswith("result-"))
            saved = json.loads((library.parent / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 2)
            again = ResearchDesk(library_path=library)
            self.assertEqual(again.get_desk()[1]["results"][0]["profit"], 16015)
            status, body = again.delete_result(ranked[1]["id"])
            self.assertEqual(status, 200)
            self.assertEqual(len(again.get_desk()[1]["results"]), 1)
            self.assertEqual(body["removed"], ranked[1]["id"])

    def test_clicking_a_row_can_read_the_script_that_was_run(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            script = folder / "one.pine"
            script.write_text(
                '//@version=5\nstrategy("One")\nwatch_bars = input.int(4, "Max Bars")\n',
                encoding="utf-8",
            )
            desk = ResearchDesk(folder=folder, library_path=folder / "strategies.json")
            status, body = desk.script_text("one.pine", "watch_bars 5")
            self.assertEqual(status, 200)
            self.assertIn("input.int(5", body["source"])
            self.assertIn('strategy("One")', body["source"])
            plain_status, plain = desk.script_text("one.pine", "script unchanged")
            self.assertEqual(plain_status, 200)
            self.assertIn("input.int(4", plain["source"])
            self.assertEqual(desk.script_text("../secrets.pine")[0], 404)
