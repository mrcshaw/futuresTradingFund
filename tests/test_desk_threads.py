"""Agents, engines, and desk requests stay on separate threads."""

import threading
import time
import unittest
from pathlib import Path


class AgentThreadTests(unittest.TestCase):
    def test_a_desk_request_does_not_wait_on_an_agent(self):
        from futuresfund.board import Board
        from futuresfund.crew import Crew

        board = Board()
        crew = Crew(board)
        started = threading.Event()
        release = threading.Event()

        def backtest():
            started.set()
            release.wait(3)
            return "finished"

        worker = threading.Thread(
            target=lambda: crew.run("2min chart developer", "Running a backtest", backtest),
            daemon=True,
        )
        worker.start()
        self.assertTrue(started.wait(2))
        began = time.perf_counter()
        opened = crew.application("Opening a library script", lambda: "script")
        elapsed = time.perf_counter() - began
        release.set()
        worker.join(3)
        self.assertEqual(opened, "script")
        self.assertLess(elapsed, 0.4)
        names = {thread.name for thread in threading.enumerate()}
        self.assertIn("agent-developer", names)
        self.assertIn("systems-administrator", names)


class EngineProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from futuresfund.engine_workers import start_engines

        cls.desk = start_engines()
        cls.desk.hold(0, 0)

    @classmethod
    def tearDownClass(cls):
        from futuresfund.engine_workers import stop_engines

        stop_engines()

    def test_a_busy_engine_does_not_stall_a_library_request(self):
        from futuresfund.backtrader_engine import engine_status
        from futuresfund.board import Board
        from futuresfund.crew import Crew

        occupier = threading.Thread(target=lambda: self.desk.burn(0, 1.2), daemon=True)
        occupier.start()
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline and not engine_status()[0]["busy"]:
            time.sleep(0.02)
        self.assertTrue(engine_status()[0]["busy"])
        crew = Crew(Board())
        began = time.perf_counter()
        opened = crew.application("Opening the strategy library", lambda: "ready")
        elapsed = time.perf_counter() - began
        self.assertEqual(opened, "ready")
        self.assertLess(elapsed, 0.4)
        occupier.join(3)

    def test_the_engine_process_matches_a_local_run(self):
        from futuresfund.backtrader_engine import execute_script

        source = (Path(__file__).resolve().parents[1] / "futuresfund" / "learningStrategies" / "outside_bar_reversal.pine").read_text(encoding="utf-8")
        bars = []
        price = 100.0
        for index in range(80):
            price += 1 if index % 5 else -2
            bars.append({"t": 1_700_000_000 + index * 900, "o": price - 1, "h": price + 2, "l": price - 3, "c": price, "v": 20})
        local = execute_script(source, bars, "15m", {"Stop ($)": 400}, "ES", "Outside bar", 2)
        local.pop("logs", None)
        remote = self.desk.run(2, source, bars, "15m", {"Stop ($)": 400}, "ES", "Outside bar", None, "START  Backtrader  Outside bar")
        self.assertEqual(remote.get("trades"), local.get("trades"))
        self.assertEqual(remote.get("net_profit"), local.get("net_profit"))
        self.assertFalse(remote.get("fatal"))
