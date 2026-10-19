"""Start and stop the researchers, including a start that arrives while a stop is still finishing."""

import threading
import time
import unittest
from unittest.mock import patch

from futuresfund.board import Board
from futuresfund.learn import begin_research, end_research


class DeskStartTests(unittest.TestCase):
    def setUp(self):
        self.board = Board()
        self.entered = threading.Event()
        self.saw_cancel = threading.Event()
        self.done = threading.Event()
        self.calls = 0

    def tearDown(self):
        self.done.set()
        end_research(self.board)
        from futuresfund import learn

        thread = learn._learning_thread
        if thread is not None:
            thread.join(timeout=2)
        if thread is None or not thread.is_alive():
            learn._learning_thread = None
            learn._leaving = False

    def _until_stopped(self, board):
        self.calls += 1
        self.entered.set()
        while not board.cancel.is_set() and not self.done.is_set():
            time.sleep(0.01)

    def _stay_through_stop(self, board):
        """Remain inside the run after Stop, the way a backtest does before it notices."""
        self.calls += 1
        self.entered.set()
        while not board.cancel.is_set() and not self.done.is_set():
            time.sleep(0.005)
        self.saw_cancel.set()
        while board.cancel.is_set() and not self.done.is_set():
            time.sleep(0.005)
        while not board.cancel.is_set() and not self.done.is_set():
            time.sleep(0.005)

    def _wait_until(self, state: str):
        for _ in range(100):
            if self.board.snapshot()["research"] == state:
                return
            time.sleep(0.02)
        self.fail(f"research stayed {self.board.snapshot()['research']!r}, expected {state}")

    def test_stop_then_start_runs_the_desk_again(self):
        with patch("futuresfund.learn._run_learning", self._until_stopped):
            started = begin_research(self.board)
            self.assertTrue(self.entered.wait(2))
            self.assertTrue(started["started"])
            self.assertEqual(self.board.snapshot()["research"], "running")

            end_research(self.board)
            self._wait_until("stopped")
            self.entered.clear()
            again = begin_research(self.board)
            self.assertTrue(again["started"])
            self.assertTrue(self.entered.wait(2))
            self.assertEqual(self.board.snapshot()["research"], "running")
            self.assertGreaterEqual(self.calls, 2)
            self.assertFalse(self.board.cancel.is_set())

    def test_start_while_stopping_keeps_the_desk_running(self):
        with patch("futuresfund.learn._run_learning", self._stay_through_stop):
            begin_research(self.board)
            self.assertTrue(self.entered.wait(2))
            end_research(self.board)
            self.assertTrue(self.saw_cancel.wait(2))
            self.assertEqual(self.board.snapshot()["research"], "stopping")
            started = begin_research(self.board)
            self.assertTrue(started["started"])
            self.assertEqual(self.board.snapshot()["research"], "running")
            self.assertFalse(self.board.cancel.is_set())
            self.assertEqual(self.calls, 1)

    def test_the_start_and_stop_routes_use_the_same_switch(self):
        with patch("futuresfund.learn._run_learning", self._until_stopped), patch("futuresfund.server.board", self.board), patch(
            "futuresfund.crew.get_crew"
        ) as crew:
            crew.return_value.application.side_effect = lambda _label, work: work()
            from futuresfund.server import start_run, stop_run

            started = start_run()
            self.assertTrue(self.entered.wait(2))
            self.assertTrue(started["started"])
            self.assertEqual(self.board.snapshot()["research"], "running")
            stop_run()
            self._wait_until("stopped")
            self.entered.clear()
            start_run()
            self.assertTrue(self.entered.wait(2))
            self.assertEqual(self.board.snapshot()["research"], "running")
            self.assertGreaterEqual(self.calls, 2)
