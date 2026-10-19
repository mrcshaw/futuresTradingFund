"""The chart agents study at the same time, and a script that cannot run leaves the learning folder."""

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class IndependentAgentTests(unittest.TestCase):
    def test_the_cannot_run_folder_is_at_the_project_root(self):
        from futuresfund.learn import CANNOT_RUN, ROOT

        self.assertEqual(CANNOT_RUN, ROOT.parent / "cannotRunStrategies")
        self.assertNotEqual(CANNOT_RUN.parent, ROOT)

    def test_three_charts_write_rules_at_the_same_time(self):
        from futuresfund.board import Board
        from futuresfund.entry_rules import ensure_entry_rule

        board = Board()
        entered = []
        gate = threading.Barrier(3)

        def write(developer, source, bars):
            entered.append(time.perf_counter())
            gate.wait(2)
            return True

        names = (
            ("2min chart developer", 'strategy("One rule")\n'),
            ("5 min chart developer", 'strategy("Two rule")\n'),
            ("15 min chart developer", 'strategy("Three rule")\n'),
        )
        with mock.patch("futuresfund.entry_rules._ready", return_value=False), mock.patch(
            "futuresfund.entry_rules._write_rule", side_effect=write
        ):
            threads = []
            for developer, source in names:
                threads.append(
                    threading.Thread(
                        target=ensure_entry_rule,
                        args=(board, developer, source, []),
                        kwargs={"wait": False},
                        daemon=True,
                    )
                )
            began = time.perf_counter()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            elapsed = time.perf_counter() - began
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(entered), 3)
        self.assertLess(max(entered) - min(entered), 0.4)
        self.assertLess(elapsed, 1.2)

    def test_a_second_chart_does_not_wait_on_the_chart_writing_the_rule(self):
        from futuresfund.board import Board
        from futuresfund.entry_rules import ensure_entry_rule

        board = Board()
        release = threading.Event()
        source = 'strategy("Fair Value Gap (FVG)")\n'

        def write(developer, source, bars):
            release.wait(2)
            return False

        box = {}
        with mock.patch("futuresfund.entry_rules._ready", return_value=False), mock.patch(
            "futuresfund.entry_rules._write_rule", side_effect=write
        ):
            first = threading.Thread(
                target=lambda: box.__setitem__("first", ensure_entry_rule(board, "5 min chart developer", source, [], wait=False)),
                daemon=True,
            )
            first.start()
            time.sleep(0.05)
            began = time.perf_counter()
            box["second"] = ensure_entry_rule(board, "15 min chart developer", source, [], wait=False)
            waited = time.perf_counter() - began
            release.set()
            first.join(2)
        self.assertFalse(first.is_alive())
        self.assertIsNone(box["second"])
        self.assertLess(waited, 0.3)
        self.assertIs(box["first"], False)

    def test_one_agent_sets_a_script_aside_while_another_chart_keeps_studying(self):
        from futuresfund import learn
        from futuresfund.board import Board

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            aside = root / "cannotRunStrategies"
            learning.mkdir()
            bad = learning / "fvg_strategy.pine"
            good = learning / "kept.pine"
            bad.write_text('strategy("Fair Value Gap")\n', encoding="utf-8")
            good.write_text('strategy("Kept")\n', encoding="utf-8")
            board = Board()
            previous_learning, previous_aside = learn.LEARNING, learn.CANNOT_RUN
            learn.LEARNING = learning
            learn.CANNOT_RUN = aside
            finished = threading.Event()
            try:
                def study_good():
                    time.sleep(0.3)
                    self.assertTrue(good.is_file())
                    finished.set()

                worker = threading.Thread(target=study_good, daemon=True)
                worker.start()
                time.sleep(0.05)
                result = learn.leave_unrunnable(
                    board,
                    bad,
                    "5 min chart developer",
                    "The engine could not add an entry rule, so this script cannot be run.",
                )
                self.assertEqual(result, "cannot_run")
                self.assertFalse(finished.is_set())
                self.assertFalse(bad.exists())
                self.assertTrue((aside / "fvg_strategy.pine").is_file())
                self.assertEqual([path.name for path in learn.queue(learning)], ["kept.pine"])
                self.assertIn("cannot be run", board.agent_tasks["5 min chart developer"])
                worker.join(2)
                self.assertTrue(finished.is_set())
            finally:
                learn.LEARNING = previous_learning
                learn.CANNOT_RUN = previous_aside
