"""A proving stage cut off by a restart, the engine's Restart or an error resumes where it stopped.

Skip pressed by the owner stays a skip. A stage resumes at most MAX_RESUMES times for a stop that was
the script's or the engine's (Restart, an engine fault, an error), and MAX_INTERRUPTIONS times for a
desk restart or a research Stop. The resumable ledger row keeps the chart pending even when the
cut-off study already wrote attempts to the notes, and the next study starts at that stage from the
pass before it, keeping the earlier passes and their trade limits.
Every test works in temporary folders: no desk notes, ledger or strategy folder is written.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import book, learn, proving

SCRIPT = """//@version=6
strategy("Resume test")
length = input.int(10, "Length", minval=2, maxval=50)
"""
# A script with daily limits of its own, for the trade-limit carry-over on a resume.
LIMITS_SCRIPT = """//@version=6
strategy("Resume limits")
length = input.int(10, "Length", minval=2, maxval=50)
use_daily_limits = input.bool(false, "Use daily limits")
daily_loss_limit = input.float(0.0, "Daily loss limit", minval=0, maxval=5000)
if close > open
    strategy.entry("L", strategy.long)
"""
# A kept pass the trade cut had reached: its daily limits on, and the desk trade limits set.
LIMITED_PASS = {"length": 16, "use_daily_limits": True, "daily_loss_limit": 600.0, "desk_daily_loss": 500, "desk_max_trades_day": 4}
RESEARCH_START = "Research stopped or the desk restarted before this stage finished."


def _default_book(test):
    """The acceptance boxes at their code defaults, and no model call for an attempt's reason."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    for patcher in (
        mock.patch.object(book, "BOOK_PATH", Path(folder.name) / "book.json"),
        mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args, **kwargs: None),
    ):
        patcher.start()
        test.addCleanup(patcher.stop)


def _executor(results, text=SCRIPT):
    calls = []

    def execute(params):
        calls.append((execute.source["sessions"], dict(params)))
        return results(execute.source["sessions"], params)

    execute.source = {"text": text, "stamp": 0, "sessions": None}
    return execute, calls


def _climbing(sessions, params):
    """Every run passes; length 20 is the best pass. The year search has no sessions (260 trading days)."""
    sessions = sessions or 260
    length = float(params.get("length", 10))
    profit = 300 + 20 * min(length, 20) - 30 * max(0.0, length - 20)
    return {"net_profit": profit, "max_drawdown": 100, "trades": 2 * sessions, "days": sessions}


class _Board:
    def __init__(self):
        self.posts = []
        self.cancel = threading.Event()

    def post(self, author, text, **kwargs):
        self.posts.append((author, text, kwargs.get("channel")))


class _Isolated(unittest.TestCase):
    """learn.NOTES, the folders and the ledger all live in one temporary folder."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.learning = self.home / "learningStrategies"
        self.created = self.home / "createdStrategies"
        self.notes = self.home / "researchNotes"
        for path in (self.learning, self.created, self.notes):
            path.mkdir()
        self.script = self.learning / "resume.pine"
        self.script.write_text(SCRIPT, encoding="utf-8")
        for patcher in (
            mock.patch.object(learn, "LEARNING", self.learning),
            mock.patch.object(learn, "CREATED", self.created),
            mock.patch.object(learn, "NOTES", self.notes),
            mock.patch.object(learn, "research_instruments", lambda: ["ES", "NQ"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        learn.clear_holds()
        self.addCleanup(learn.clear_holds)
        # A resumed stage run straight through _prove_stages (no _study around it) leaves its key behind.
        learn._resuming.clear()
        self.addCleanup(learn._resuming.clear)
        # Every ledger path below is inside the temporary folder.
        self.assertTrue(str(proving._ledger()).startswith(str(self.home)))

    def _entry(self, root="ES", timeframe="5m", file="resume.pine"):
        return proving._load().get(f"{file}|{root}|{timeframe}")

    def _row(self, root="ES", timeframe="5m", file="resume.pine"):
        return next(row for row in proving.rows() if row["id"] == f"{file}|{root}|{timeframe}")

    def _stop_at(self, stage, passes: dict, resume=True, root="ES", timeframe="5m", counted=False):
        """A row that passed `passes` ({days: length, or days: params}) and stopped at `stage`.

        By default it stopped the way research starting stops it: resumable, and not counted against MAX_RESUMES.
        """
        for days, length in passes.items():
            params = dict(length) if isinstance(length, dict) else {"length": length}
            proving.mark(self.script.name, root, timeframe, days, "testing", {"attempt_budget": learn.stage_attempts(days)})
            proving.mark(
                self.script.name, root, timeframe, days, "passed",
                {"net_profit": 500 + days, "trades": 9, "max_drawdown": 100, "params": params,
                 "attempts_used": 30, "attempt_budget": learn.stage_attempts(days), "best_attempt": 4},
                SCRIPT,
            )
        proving.mark(self.script.name, root, timeframe, stage, "testing", {"attempt_budget": learn.stage_attempts(None if stage == 365 else stage)})
        proving.stop_testing(RESEARCH_START, file=self.script.name, timeframe=timeframe, root=root, resume=resume, counted=counted)

    def _finish_notes(self, charts=(("ES", "5m"),)):
        run = {"attempts": 3, "trials": [{"trades": 8, "net_profit": 400}], "best": {"trades": 8, "net_profit": 400}}
        instruments = {}
        for root, timeframe in charts:
            instruments.setdefault(root, {"charts": {}})["charts"][timeframe] = run
        (self.notes / "resume.json").write_text(json.dumps({"instruments": instruments}), encoding="utf-8")


class LedgerResumeTests(_Isolated):
    def test_a_resumable_stop_says_it_resumes_and_a_skip_does_not(self):
        proving.mark("a.pine", "ES", "5m", 30, "testing")
        proving.mark("b.pine", "ES", "5m", 30, "testing")
        proving.stop_testing("The engine was restarted.", file="a.pine", resume=True)
        proving.stop_testing("It was skipped on this chart.", file="b.pine")
        a, b = self._row(file="a.pine"), self._row(file="b.pine")
        self.assertEqual((a["label"], a["resume"]), ("Stopped at 30 days, resumes there", True))
        self.assertEqual((b["label"], b["resume"]), ("Stopped at 30 days", False))
        self.assertFalse(b["resume"])
        self.assertIs(self._entry(file="b.pine")["resume"], False)

    def test_a_stage_resumes_at_most_three_times_after_its_own_faults(self):
        self._stop_at(30, {7: 20})
        proving.mark(self.script.name, "ES", "5m", 30, "testing", resumed=True)
        for turn in range(proving.MAX_RESUMES):
            # Each stop is counted when it is made: an error is the script's or the engine's fault.
            proving.stop_testing("The search stopped on an error.", file=self.script.name, resume=True)
            self.assertTrue(proving.resumable(self._entry()), turn)
            self.assertEqual(self._entry()["resumes"], turn + 1)
            # Starting the resumed stage again does not count it a second time.
            proving.mark(self.script.name, "ES", "5m", 30, "testing", resumed=True)
            self.assertEqual(self._entry()["resumes"], turn + 1)
        proving.stop_testing("The search stopped on an error.", file=self.script.name, resume=True)
        entry = self._entry()
        self.assertEqual(entry["resumes"], proving.MAX_RESUMES)
        self.assertFalse(proving.resumable(entry))
        self.assertIn(f"already resumed {proving.MAX_RESUMES} times", entry["reason"])
        self.assertEqual(self._row()["label"], "Stopped at 30 days")
        # The passes stay with the capped row.
        self.assertIn("7", entry["passed"])

    def test_desk_restarts_and_research_stops_do_not_use_up_the_resumes(self):
        # A row stopped at the year, as the live rows were. Its year search keeps being cut off by deploys.
        self._stop_at(365, {7: 20, 30: 18, 90: 17})
        for turn in range(proving.MAX_RESUMES + 2):
            self.assertTrue(proving.resumable(self._entry()), turn)
            execute, _calls = _executor(_climbing)
            learn._prove_stages({"length": 10}, execute, None, self.script, None, None, "dev", "Chart researcher", None, root="ES", timeframe="5m")
            self.assertEqual(self._entry()["status"], "testing")
            # The desk restarts: research starting stops every testing row.
            proving.stop_testing(RESEARCH_START, resume=True, counted=False)
        entry = self._entry()
        self.assertTrue(proving.resumable(entry))
        self.assertNotIn("resumes", entry)
        self.assertEqual(entry["interruptions"], proving.MAX_RESUMES + 3)
        self.assertEqual(sorted(entry["passed"]), ["30", "7", "90"])
        self.assertEqual(self._row()["label"], "Stopped at 365 days, resumes there")

    def test_a_research_stop_inside_a_study_is_not_counted(self):
        self._stop_at(30, {7: 20})
        board = _Board()

        def studies(board, path, *args, **kwargs):
            proving.mark(path.name, "ES", "5m", 30, "testing", resumed=True)
            board.cancel.set()
            return None

        for _turn in range(proving.MAX_RESUMES + 1):
            board.cancel.clear()
            with mock.patch.object(learn, "_study_chart", side_effect=studies):
                learn._study(board, self.script, {}, None, timeframe="5m", root="ES")
            row = self._row()
            self.assertEqual((row["reason"], row["resume"]), ("Research was stopped before this stage finished.", True))
        self.assertNotIn("resumes", self._entry())
        # An error while research is stopping (a worker killed as the desk goes down) is not counted either.
        board.cancel.clear()

        def dies(board, path, *args, **kwargs):
            proving.mark(path.name, "ES", "5m", 30, "testing", resumed=True)
            board.cancel.set()
            raise RuntimeError("worker died")

        with mock.patch.object(learn, "_study_chart", side_effect=dies), self.assertRaises(RuntimeError):
            learn._study(board, self.script, {}, None, timeframe="5m", root="ES")
        self.assertTrue(self._row()["resume"])
        self.assertNotIn("resumes", self._entry())

    def test_interruptions_have_their_own_larger_cap(self):
        self._stop_at(90, {7: 20, 30: 16})
        for _turn in range(proving.MAX_INTERRUPTIONS - 1):
            proving.mark(self.script.name, "ES", "5m", 90, "testing", resumed=True)
            proving.stop_testing(RESEARCH_START, resume=True, counted=False)
        self.assertTrue(proving.resumable(self._entry()))
        self.assertEqual(self._entry()["interruptions"], proving.MAX_INTERRUPTIONS)
        proving.mark(self.script.name, "ES", "5m", 90, "testing", resumed=True)
        proving.stop_testing(RESEARCH_START, resume=True, counted=False)
        entry = self._entry()
        self.assertFalse(proving.resumable(entry))
        self.assertIn(f"already cut off {proving.MAX_INTERRUPTIONS} times at the 90-day stage", entry["reason"])
        self.assertEqual(self._row()["label"], "Stopped at 90 days")

    def test_a_pass_a_fail_or_a_new_study_resets_the_resume_count(self):
        self._stop_at(30, {7: 20}, counted=True)
        self.assertEqual(self._entry()["resumes"], 1)
        proving.mark(self.script.name, "ES", "5m", 30, "testing", resumed=True)
        self.assertEqual(self._entry()["resumes"], 1)
        self.assertNotIn("resume", self._entry())
        proving.mark(self.script.name, "ES", "5m", 30, "passed", {"params": {"length": 20}}, SCRIPT)
        self.assertNotIn("resumes", self._entry())
        self.assertNotIn("interruptions", self._entry())
        proving.mark(self.script.name, "ES", "5m", 90, "testing")
        proving.stop_testing("The search stopped on an error.", file=self.script.name, resume=True)
        proving.mark(self.script.name, "ES", "5m", 90, "testing", resumed=True)
        proving.mark(self.script.name, "ES", "5m", 90, "failed")
        self.assertNotIn("resumes", self._entry())
        # A resumed first stage keeps its counts; a new study at 7 days starts them over.
        proving.mark(self.script.name, "ES", "5m", 7, "testing")
        proving.stop_testing("The search stopped on an error.", file=self.script.name, resume=True)
        proving.mark(self.script.name, "ES", "5m", 7, "testing", resumed=True)
        self.assertEqual((self._entry()["resumes"], self._entry().get("interruptions")), (1, None))
        proving.stop_testing(RESEARCH_START, resume=True, counted=False)
        proving.mark(self.script.name, "ES", "5m", 7, "testing", resumed=True)
        self.assertEqual((self._entry()["resumes"], self._entry()["interruptions"]), (1, 1))
        proving.mark(self.script.name, "ES", "5m", 7, "testing")
        self.assertNotIn("resumes", self._entry())
        self.assertNotIn("interruptions", self._entry())

    def test_rows_stopped_before_resume_was_recorded_resume_unless_the_stop_was_the_scripts(self):
        legacy = {
            "Research stopped or the desk restarted before this stage finished.": True,
            "The search stopped on an error.": True,
            # The old Restart button ended a study exactly like Skip.
            "It was skipped on this chart.": True,
            "The engine stopped. PineTS did not finish within 10 minutes.": True,
            "It went to the developer for a repair. The script did not compile.": False,
            "It waits in awaitingAnswerStrategies for the owner's emailed answer q12.": False,
        }
        data = {
            f"old{index}.pine|GC|15m": {"file": f"old{index}.pine", "root": "GC", "timeframe": "15m", "stage": 365, "status": "stopped", "reason": reason, "passed": {}}
            for index, reason in enumerate(legacy)
        }
        proving._write(data)
        for index, expected in enumerate(legacy.values()):
            self.assertEqual(proving.resumable(proving._load()[f"old{index}.pine|GC|15m"]), expected, index)
            self.assertEqual(bool(proving.resumable_roots(f"old{index}.pine", "15m")), expected, index)

    def test_research_starting_marks_the_stages_it_stops_resumable(self):
        source = inspect.getsource(learn._run_learning)
        # A desk restart is not the script's fault, so research starting does not count it against MAX_RESUMES.
        self.assertIn(f'stop_testing("{RESEARCH_START}", resume=True, counted=False)', source)
        proving.mark("a.pine", "ES", "5m", 365, "testing")
        proving.mark("b.pine", "YM", "2m", 30, "testing")
        proving.stop_testing(RESEARCH_START, resume=True, counted=False)
        self.assertEqual(
            sorted(row["label"] for row in proving.rows()),
            ["Stopped at 30 days, resumes there", "Stopped at 365 days, resumes there"],
        )

    def test_a_resumable_row_is_not_trimmed(self):
        with mock.patch.object(proving, "LIMIT", 1), mock.patch.object(proving, "_now") as now:
            now.return_value = "2026-11-01 01:00:00"
            proving.mark("stopped.pine", "ES", "5m", 30, "testing")
            proving.stop_testing(RESEARCH_START, resume=True, counted=False)
            now.return_value = "2026-11-01 02:00:00"
            proving.mark("newer.pine", "ES", "5m", 7, "failed")
        self.assertEqual(sorted(row["file"] for row in proving.rows()), ["stopped.pine"])

    def test_a_resumable_row_that_waited_past_the_keep_days_is_trimmed(self):
        with mock.patch.object(proving, "LIMIT", 1), mock.patch.object(proving, "_now") as now:
            now.return_value = "2026-11-01 01:00:00"
            proving.mark("gone.pine", "ES", "5m", 30, "testing")
            proving.stop_testing(RESEARCH_START, resume=True, counted=False)
            # The file was moved by hand, so it never resumes. After RESUME_KEEP_DAYS its row is settled.
            now.return_value = f"2026-11-{1 + proving.RESUME_KEEP_DAYS:02d} 02:00:00"
            proving.mark("newer.pine", "ES", "5m", 7, "failed")
        self.assertEqual(sorted(row["file"] for row in proving.rows()), ["newer.pine"])

    def test_parking_a_script_drops_its_resumes(self):
        self._stop_at(90, {7: 20, 30: 16}, timeframe="15m")
        self.assertEqual(proving.resumable_roots("resume.pine", "15m"), {"ES": 90})
        cannot = self.home / "cannotRunStrategies"
        with mock.patch.object(learn, "CANNOT_RUN", cannot):
            self.assertEqual(learn.leave_unrunnable(None, self.script, "Chart developer", "It never trades."), "cannot_run")
        self.assertTrue((cannot / "resume.pine").is_file())
        self.assertEqual(proving.resumable_roots("resume.pine", "15m"), {})
        self.assertEqual(self._row(timeframe="15m")["label"], "Stopped at 90 days")


class CacheTests(_Isolated):
    def test_the_cache_follows_the_desks_own_writes_at_once(self):
        self.assertEqual(proving.resumable_roots("resume.pine", "5m"), {})
        self._stop_at(30, {7: 20})
        self.assertEqual(proving.resumable_roots("resume.pine", "5m"), {"ES": 30})
        proving.mark("resume.pine", "ES", "5m", 30, "testing", resumed=True)
        self.assertEqual(proving.resumable_roots("resume.pine", "5m"), {})

    def test_a_scan_reads_no_ledger_file_between_checks(self):
        self._stop_at(30, {7: 20})
        proving.resumable_roots("resume.pine", "5m")
        loads, stats = [], []
        real_load, real_stamp = proving._load, proving._stamp
        with mock.patch.object(proving, "_load", lambda: loads.append(1) or real_load()), \
                mock.patch.object(proving, "_stamp", lambda path: stats.append(1) or real_stamp(path)):
            for _turn in range(50):
                learn.jobs_for_chart("5m", root="ES")
        self.assertEqual((loads, stats), ([], []))

    def test_a_change_from_outside_is_read_at_the_next_check(self):
        self.assertEqual(proving.resumable_roots("other.pine", "2m"), {})
        outside = {"other.pine|NQ|2m": {"file": "other.pine", "root": "NQ", "timeframe": "2m", "stage": 90, "status": "stopped", "resume": True, "passed": {}}}
        proving._ledger().parent.mkdir(parents=True, exist_ok=True)
        proving._ledger().write_text(json.dumps(outside), encoding="utf-8")
        # Inside CACHE_CHECK the cache stands.
        self.assertEqual(proving.resumable_roots("other.pine", "2m"), {})
        with mock.patch.object(proving, "CACHE_CHECK", 0.0):
            self.assertEqual(proving.resumable_roots("other.pine", "2m"), {"NQ": 90})

    def test_threads_that_wait_for_the_ledger_lock_stat_it_once(self):
        self._stop_at(30, {7: 20})
        proving.resumable_roots("resume.pine", "5m")
        stats, found = [], []
        real_stamp = proving._stamp
        with mock.patch.object(proving, "_stamp", lambda path: stats.append(1) or real_stamp(path)):
            with proving._CACHE_LOCK:
                # The 30-second check is due.
                proving._cache["checked"] = time.monotonic() - proving.CACHE_CHECK - 1
            threads = [threading.Thread(target=lambda: found.append(proving.resumable_roots("resume.pine", "5m"))) for _ in range(8)]
            with proving._LOCK:
                for thread in threads:
                    thread.start()
                # Every thread passes the fast path and waits here for the ledger lock.
                time.sleep(0.3)
            for thread in threads:
                thread.join(5)
        self.assertEqual(found, [{"ES": 30}] * 8)
        self.assertEqual(len(stats), 1)


class PendingAgainTests(_Isolated):
    def test_an_attempted_chart_with_a_resumable_stage_is_pending_again(self):
        self._finish_notes()
        self.assertEqual(learn.jobs_for_chart("5m", root="ES"), [])
        self._stop_at(365, {7: 20, 30: 20, 90: 20})
        self.assertEqual([path.name for path in learn.jobs_for_chart("5m", root="ES")], ["resume.pine"])
        self.assertIn("ES", learn._open_roots(self.script, "5m"))
        self.assertEqual(learn.JobClaims().take("ES", "5m"), self.script)
        # The progress counts agree with the queue.
        self.assertIn("ES", learn._open_from_notes(self.script, "5m", learn._peek_notes(self.script), {}))

    def test_a_skip_or_a_capped_stop_leaves_the_chart_finished(self):
        self._finish_notes()
        self._stop_at(30, {7: 20}, resume=False)
        self.assertEqual(learn.jobs_for_chart("5m", root="ES"), [])
        self._stop_at(30, {7: 20})
        entry = proving._load()["resume.pine|ES|5m"]
        entry["resumes"] = proving.MAX_RESUMES
        proving._write(proving._load() | {"resume.pine|ES|5m": entry})
        proving.mark("resume.pine", "ES", "5m", 30, "testing")
        proving.stop_testing("The search stopped on an error.", file="resume.pine", resume=True)
        self.assertEqual(learn.jobs_for_chart("5m", root="ES"), [])

    def test_a_script_with_a_resumable_stage_is_not_filed_until_it_finishes(self):
        self._finish_notes([(root, timeframe) for root in ("ES", "NQ") for timeframe in learn.RESEARCH_CHARTS])
        notes = learn._read_notes(self.script)
        self.assertTrue(learn.ready_to_file(notes))
        self._stop_at(90, {7: 20, 30: 20})
        board = _Board()
        with mock.patch.object(learn, "_file_away") as filed:
            learn._file_ready(board, self.script, notes)
            filed.assert_not_called()
            proving.mark("resume.pine", "ES", "5m", 90, "failed")
            learn._file_ready(board, self.script, notes)
            filed.assert_called_once()

    def test_a_resumed_study_is_not_filed_while_it_runs(self):
        _default_book(self)
        self._finish_notes([(root, timeframe) for root in ("ES", "NQ") for timeframe in learn.RESEARCH_CHARTS])
        notes = learn._read_notes(self.script)
        self._stop_at(365, {7: 20, 30: 18, 90: 17})
        board = _Board()
        seen = {}

        def studies(board, path, *args, **kwargs):
            execute, _calls = _executor(_climbing)
            learn._prove_stages({"length": 10}, execute, board, path, None, None, "dev", "Chart researcher", None, root="ES", timeframe="5m")
            # The year's first mark makes the row "testing", so the ledger alone no longer holds the file back.
            seen["status"] = self._entry()["status"]
            # Another engine's file_completed_soon pass, on its own thread, while the resumed year search runs.
            other = threading.Thread(target=learn._file_ready, args=(board, path, notes))
            other.start()
            other.join(10)
            seen["filed_mid_run"] = filed.called
            # The year ends and is marked. This study's own filing check, at its end, then files the script.
            proving.mark(path.name, "ES", "5m", 365, "passed", {"params": {"length": 17}}, SCRIPT)
            learn._file_ready(board, path, notes)
            seen["filed_at_its_end"] = filed.call_count
            return None

        with mock.patch.object(learn, "_file_away") as filed:
            with mock.patch.object(learn, "_study_chart", side_effect=studies):
                learn._study(board, self.script, {}, None, timeframe="5m", root="ES")
        self.assertEqual(seen, {"status": "testing", "filed_mid_run": False, "filed_at_its_end": 1})
        self.assertEqual(learn._resuming, set())

    def test_a_resumed_study_still_holds_back_its_files_other_charts(self):
        self._finish_notes([(root, timeframe) for root in ("ES", "NQ") for timeframe in learn.RESEARCH_CHARTS])
        notes = learn._read_notes(self.script)
        # Another engine's thread is running a resumed NQ 15m stage of this file.
        other = threading.Thread(target=learn._begin_resuming, args=(self.script.name, "NQ", "15m"))
        other.start()
        other.join(10)
        with mock.patch.object(learn, "_file_away") as filed:
            learn._file_ready(_Board(), self.script, notes)
            filed.assert_not_called()
            learn._end_resuming(self.script.name, "NQ", "15m")
            learn._file_ready(_Board(), self.script, notes)
            filed.assert_called_once()

    def test_an_answer_that_reruns_every_chart_drops_the_resume(self):
        self._finish_notes()
        self._stop_at(30, {7: 20})
        self.assertTrue(learn._reopen_for_owner(None, self.script, "Chart researcher"))
        self.assertEqual(proving.resumable_roots("resume.pine", "5m"), {})
        self.assertFalse(self._row()["resume"])


class StudyOutcomeTests(_Isolated):
    def _study(self, outcome):
        def studies(board, path, *args, **kwargs):
            proving.mark(path.name, "ES", "5m", 30, "testing")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(learn, "_study_chart", side_effect=studies):
            return learn._study(None, self.script, {}, None, timeframe="5m", root="ES")

    def test_restart_resumes_and_skip_does_not(self):
        self.assertEqual(self._study("restarted"), "restarted")
        row = self._row()
        self.assertEqual(row["label"], "Stopped at 30 days, resumes there")
        self.assertEqual(row["reason"], "The engine was restarted during this stage. It resumes at this stage on its next turn.")
        self.assertEqual(self._study("skipped"), "skipped")
        row = self._row()
        self.assertEqual((row["label"], row["reason"]), ("Stopped at 30 days", "It was skipped on this chart."))

    def test_an_error_or_an_engine_fault_resumes_and_a_question_does_not(self):
        with self.assertRaises(RuntimeError):
            self._study(RuntimeError("worker died"))
        self.assertEqual((self._row()["reason"], self._row()["resume"]), ("The search stopped on an error.", True))
        with mock.patch("futuresfund.backtrader_engine.set_engine_notice"):
            def faults(board, path, *args, **kwargs):
                proving.mark(path.name, "ES", "5m", 30, "testing")
                learn._tell_administrator(None, 0, path, "PineTS did not finish within 10 minutes.")
                return "engine"

            with mock.patch.object(learn, "_study_chart", side_effect=faults):
                learn._study(None, self.script, {}, None, timeframe="5m", root="ES")
        self.assertTrue(self._row()["resume"])
        self.assertEqual(self._study(None), None)
        self.assertEqual((self._row()["reason"], self._row()["resume"]), ("The search ended before this stage had a result.", True))
        self._study("held")
        self.assertFalse(self._row()["resume"])


class RestartOutcomeTests(unittest.TestCase):
    """The engine's Restart ends the search as restarted; Skip ends it as skipped."""

    INDEX = 9

    def setUp(self):
        _default_book(self)
        from futuresfund import backtrader_engine

        self.engine = backtrader_engine
        self.addCleanup(backtrader_engine.clear_skips)
        self.addCleanup(backtrader_engine.clear_restarts)
        backtrader_engine.clear_skips()
        backtrader_engine.clear_restarts()
        busy = mock.patch.dict(backtrader_engine._ENGINE_STATUS[self.INDEX], {"busy": True})
        busy.start()
        self.addCleanup(busy.stop)
        quiet = mock.patch("futuresfund.engine_workers.active_desk", lambda: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def _search(self, execute):
        return learn._search({"length": 10}, execute, learn.ACCOUNT_SIZE, 5, None, None, None, slot=self.INDEX)

    def test_restart_before_the_next_attempt_ends_the_search_as_restarted(self):
        self.engine.request_restart(self.INDEX + 1)
        execute, calls = _executor(_climbing)
        trials = self._search(execute)
        self.assertTrue(trials.restarted)
        self.assertTrue(trials.skipped)
        self.assertEqual(calls, [])
        self.assertFalse(self.engine.skipped(self.INDEX))
        self.assertFalse(self.engine.restarted(self.INDEX))

    def test_restart_that_cuts_a_running_backtest_ends_the_search_as_restarted(self):
        def cut(sessions, params):
            self.engine.request_restart(self.INDEX + 1)
            return self.engine.skipped_measurement("5m")

        execute, _calls = _executor(cut)
        trials = self._search(execute)
        self.assertTrue(trials.restarted)
        self.assertFalse(self.engine.restarted(self.INDEX))

    def test_skip_stays_a_skip_even_after_a_restart(self):
        self.engine.request_skip(self.INDEX + 1)
        execute, _calls = _executor(_climbing)
        trials = self._search(execute)
        self.assertTrue(trials.skipped)
        self.assertFalse(trials.restarted)
        self.engine.request_restart(self.INDEX + 1)
        self.engine.request_skip(self.INDEX + 1)
        self.assertFalse(self.engine.restarted(self.INDEX))
        trials = self._search(_executor(_climbing)[0])
        self.assertFalse(trials.restarted)

    def test_a_restart_no_search_read_does_not_skip_the_next_study(self):
        # Restart pressed while the engine looked busy, but no search read the cut (the busy flag was stale,
        # or the cut came after the last attempt). The loop top takes the restart and its skip flag.
        self.engine.request_restart(self.INDEX + 1)
        self.assertTrue(self.engine.skipped(self.INDEX))
        self.assertTrue(learn._take_engine_restart(self.INDEX))
        self.assertFalse(learn._take_engine_restart(self.INDEX))
        self.assertEqual((self.engine.skipped(self.INDEX), self.engine.restarted(self.INDEX)), (False, False))
        execute, calls = _executor(_climbing)
        trials = self._search(execute)
        self.assertFalse(getattr(trials, "skipped", False))
        self.assertTrue(calls)
        # The chart engine's loop (developer_loop inside _run_learning) reads the restart through this helper.
        self.assertIn("if _take_engine_restart(index):", inspect.getsource(learn._run_learning))

    def test_a_skip_is_left_for_the_next_study_by_the_loop_top(self):
        self.engine.request_skip(self.INDEX + 1)
        self.assertFalse(learn._take_engine_restart(self.INDEX))
        self.assertTrue(self.engine.skipped(self.INDEX))

    def test_a_restart_of_a_search_outside_the_stages_does_not_promise_a_resume(self):
        board = _Board()
        self.engine.request_restart(self.INDEX + 1)
        execute, _calls = _executor(_climbing)
        learn._search({"length": 10}, execute, learn.ACCOUNT_SIZE, 5, board, None, None, slot=self.INDEX, developer="Creation tester")
        texts = [text for _author, text, _channel in board.posts if "restarted" in text]
        self.assertEqual(texts, [f"Engine {self.INDEX + 1} restarted. Taking the next script."])
        board = _Board()
        self.engine.request_restart(self.INDEX + 1)
        learn._search({"length": 10}, _executor(_climbing)[0], learn.ACCOUNT_SIZE, 5, board, None, None, slot=self.INDEX, stage_days=30)
        texts = [text for _author, text, _channel in board.posts if "restarted" in text]
        self.assertEqual(texts, [f"Engine {self.INDEX + 1} restarted. the script resumes at this stage on its next turn. Taking the next script."])


class ProveStagesResumeTests(_Isolated):
    def setUp(self):
        super().setUp()
        _default_book(self)

    def _prove(self, execute, board=None, facts=None):
        return learn._prove_stages(
            {"length": 10}, execute, board, self.script, None, facts, "5 min chart developer", "Chart researcher", None,
            root="ES", timeframe="5m",
        )

    def test_a_stop_at_30_days_resumes_there_from_the_7_day_pass(self):
        self._stop_at(30, {7: 20})
        kept_at = self._entry()["passed"]["7"]["at"]
        seen = {}

        def results(sessions, params):
            if not seen:
                entry = self._entry()
                seen.update(stage=entry["stage"], status=entry["status"], resumes=entry.get("resumes"),
                            interruptions=entry.get("interruptions"), passed=sorted(entry["passed"]))
            return _climbing(sessions, params)

        execute, calls = _executor(results)
        settings, stopped = self._prove(execute)
        self.assertIsNone(stopped)
        self.assertEqual(calls[0], (21, {"length": 20}))
        self.assertNotIn(5, [sessions for sessions, _params in calls])
        # While the 30-day stage ran, the 7-day pass stayed. The desk restart that stopped it was counted once,
        # as an interruption (not against MAX_RESUMES), when it stopped; starting again does not count it again.
        self.assertEqual(seen, {"stage": 30, "status": "testing", "resumes": None, "interruptions": 1, "passed": ["7"]})
        entry = self._entry()
        self.assertEqual(entry["passed"]["7"]["at"], kept_at)
        self.assertEqual(sorted(entry["passed"]), ["30", "7", "90"])
        self.assertEqual((entry["stage"], entry["status"]), (365, "testing"))
        # The skipped stage is filled in from the ledger for the notes and the end of the study.
        self.assertEqual(execute.source["stage_attempts"][7]["used"], 30)
        self.assertTrue(execute.source["stage_attempts"][7]["resumed"])
        self.assertEqual(execute.source["stage_runs"][7]["params"], {"length": 20})
        self.assertEqual(sorted(execute.source["stage_runs"]), [7, 30, 90])
        self.assertEqual(settings["length"], 20)

    def test_a_stop_at_90_days_resumes_from_the_30_day_pass(self):
        self._stop_at(90, {7: 20, 30: 16})
        execute, calls = _executor(_climbing)
        settings, stopped = self._prove(execute)
        self.assertIsNone(stopped)
        self.assertEqual(calls[0], (64, {"length": 16}))
        self.assertEqual({sessions for sessions, _params in calls}, {64})
        self.assertEqual(sorted(self._entry()["passed"]), ["30", "7", "90"])
        self.assertEqual(sorted(execute.source["stage_attempts"]), [7, 30, 90])

    def test_a_stop_at_the_year_starts_the_year_search_at_once(self):
        self._stop_at(365, {7: 20, 30: 18, 90: 17})
        execute, calls = _executor(_climbing)
        board = _Board()
        settings, stopped = self._prove(execute, board)
        self.assertEqual((settings, stopped, calls), ({"length": 17}, None, []))
        entry = self._entry()
        # The year's testing mark keeps the passes and the count the stop made (one interruption).
        self.assertEqual((entry["stage"], entry["status"], entry.get("resumes"), entry["interruptions"]), (365, "testing", None, 1))
        self.assertEqual(sorted(entry["passed"]), ["30", "7", "90"])
        self.assertEqual(entry["attempts"]["365"]["budget"], learn.stage_attempts(None))
        [(author, text, channel)] = board.posts
        self.assertEqual((author, channel), ("Chart researcher", "Chart researcher"))
        self.assertIn("resumes at the one-year search", text)
        self.assertIn("from the 90-day pass", text)
        self.assertIn("The 7, 30 and 90-day passes are kept.", text)
        self.assertEqual(sorted(execute.source["stage_runs"]), [7, 30, 90])

    def test_a_row_missing_an_earlier_pass_starts_at_the_7_day_stage(self):
        self._stop_at(90, {7: 20})
        # The 30-day pass is missing, as when the row was written before that stage could pass.
        execute, calls = _executor(_climbing)
        self._prove(execute)
        self.assertEqual(calls[0][0], 5)

    def test_a_skipped_stop_starts_again_at_the_7_day_stage(self):
        self._stop_at(90, {7: 20, 30: 16}, resume=False)
        execute, calls = _executor(_climbing)
        self._prove(execute)
        self.assertEqual(calls[0], (5, {"length": 10}))

    def _prove_limited(self, execute, board=None):
        from futuresfund.pine_report import pine_facts

        facts = pine_facts(LIMITS_SCRIPT)
        base = learn._pine_params(facts)
        return facts, learn._prove_stages(
            base, execute, board, self.script, None, facts, "5 min chart developer", "Chart researcher", None,
            root="ES", timeframe="5m",
        )

    def _assert_limits_on(self, params):
        self.assertEqual(params["length"], 16)
        self.assertIs(params["use_daily_limits"], True)
        self.assertEqual(params["daily_loss_limit"], 600.0)
        self.assertEqual((params["desk_daily_loss"], params["desk_max_trades_day"]), (500, 4))
        self.assertIsInstance(params["desk_daily_loss"], int)

    def test_a_resume_at_90_days_keeps_the_kept_pass_trade_limits_on(self):
        from futuresfund.strategy_team import has_trade_limits

        self._stop_at(90, {7: {"length": 20}, 30: LIMITED_PASS})
        execute, calls = _executor(_climbing, LIMITS_SCRIPT)
        _facts, (settings, stopped) = self._prove_limited(execute)
        self.assertIsNone(stopped)
        sessions, first = calls[0]
        self.assertEqual(sessions, 64)
        # The daily limits the trade cut turned on are not zeroed, and the desk limits are written in again.
        self._assert_limits_on(first)
        self.assertTrue(has_trade_limits(execute.source["text"]))
        self.assertEqual(execute.source["stamp"], 1)
        self.assertTrue(execute.source["trade_cut"]["touched"])

    def test_a_resume_at_the_year_keeps_the_trade_limits_in_the_year_search(self):
        self._stop_at(365, {7: {"length": 20}, 30: {"length": 18}, 90: LIMITED_PASS})
        execute, calls = _executor(_climbing, LIMITS_SCRIPT)
        facts, (settings, stopped) = self._prove_limited(execute)
        self.assertEqual((stopped, calls), (None, []))
        self._assert_limits_on(settings)
        trials = learn._search(settings, execute, learn.ACCOUNT_SIZE, 2, None, self.script, None, facts, extras=False, year=True)
        self.assertTrue(trials)
        sessions, first = calls[0]
        self.assertIsNone(sessions)
        self._assert_limits_on(first)

    def test_a_resume_from_a_pass_without_limits_changes_nothing(self):
        self._stop_at(90, {7: 20, 30: 16})
        execute, calls = _executor(_climbing, LIMITS_SCRIPT)
        self._prove_limited(execute)
        self.assertEqual(execute.source["text"], LIMITS_SCRIPT)
        self.assertFalse(execute.source.get("trade_cut", {}).get("touched"))
        self.assertEqual(calls[0], (64, {"length": 16, "use_daily_limits": False, "daily_loss_limit": 0.0}))

    def test_the_kept_params_take_the_scripts_own_types_and_limits(self):
        from futuresfund.pine_report import pine_facts

        text = (
            '//@version=6\nstrategy("Types")\nlength = input.int(10, "Length", minval=2, maxval=50)\n'
            'mult = input.float(1.5, "Mult", minval=0.5, maxval=4.0)\nuse_filter = input.bool(true, "Use filter")\n'
            'mode = input.string("Fast", "Mode", options=["Fast", "Slow"])\n'
        )
        facts = pine_facts(text)
        base = learn._pine_params(facts)
        merged = learn._resume_settings(base, {"length": 80.0, "mult": 2, "use_filter": "yes", "mode": "Gone", "ghost": 3}, facts, "ES")
        self.assertEqual(merged, {"length": 50, "mult": 2.0, "use_filter": True, "mode": "Fast"})
        self.assertIsInstance(merged["length"], int)
        self.assertIsInstance(merged["mult"], float)
        merged = learn._resume_settings(base, {"length": 12, "mode": "Slow", "use_filter": False}, facts, "ES")
        self.assertEqual(merged, {"length": 12, "mult": 1.5, "use_filter": False, "mode": "Slow"})


class NotesWriteTests(_Isolated):
    ROW = {"timeframe": "5m", "instrument": "ES", "trades": 4, "net_profit": 120.0, "max_drawdown": 50.0}

    def setUp(self):
        super().setUp()
        pause = mock.patch.object(learn, "NOTE_WRITE_PAUSE", 0.0)
        pause.start()
        self.addCleanup(pause.stop)

    def _leftovers(self):
        return [path.name for path in self.notes.iterdir() if path.name.endswith(".tmp")]

    def test_a_refused_write_is_tried_again(self):
        real = os.replace
        tries = []

        def flaky(source, target):
            tries.append(1)
            if len(tries) <= 2:
                raise OSError(22, "Invalid argument")
            return real(source, target)

        with mock.patch("os.replace", flaky):
            learn._remember_chart_attempt(self.script, {"title": "Resume"}, dict(self.ROW), 1)
        self.assertEqual(len(tries), 3)
        saved = json.loads((self.notes / "resume.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["instruments"]["ES"]["charts"]["5m"]["trials"][0]["net_profit"], 120.0)
        self.assertEqual(self._leftovers(), [])

    def test_a_write_that_keeps_failing_leaves_the_old_note_and_does_not_raise(self):
        note = self.notes / "resume.json"
        note.write_text(json.dumps({"file": "resume.pine", "owner_answers": {"length": 12}}), encoding="utf-8")
        before = note.read_text(encoding="utf-8")
        real_write = Path.write_text

        def refuse(path, *args, **kwargs):
            if path.name.endswith(".tmp"):
                raise OSError(22, "Invalid argument")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", refuse), mock.patch("sys.stderr"):
            learn._remember_chart_attempt(self.script, {"title": "Resume"}, dict(self.ROW), 1)
        self.assertEqual(note.read_text(encoding="utf-8"), before)
        self.assertEqual(self._leftovers(), [])

    def _refusing(self):
        real_write = Path.write_text

        def refuse(path, *args, **kwargs):
            if path.name.endswith(".tmp"):
                raise OSError(22, "Invalid argument")
            return real_write(path, *args, **kwargs)

        return mock.patch.object(Path, "write_text", refuse)

    def test_the_end_of_study_write_is_tried_again(self):
        real = os.replace
        tries = []

        def flaky(source, target):
            tries.append(1)
            if len(tries) <= 2:
                raise OSError(22, "Invalid argument")
            return real(source, target)

        row = dict(self.ROW)
        with mock.patch("os.replace", flaky):
            learn._save_notes(self.script, {"title": "Resume"}, [row], "", "", row, "ES", "5m")
        self.assertEqual(len(tries), 3)
        saved = json.loads((self.notes / "resume.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["instruments"]["ES"]["charts"]["5m"]["trials"][0]["net_profit"], 120.0)
        self.assertEqual(self._leftovers(), [])

    def test_a_refused_end_of_study_write_keeps_the_old_note_whole_and_raises(self):
        # write_text straight onto the note truncated it first, so a refused write left it empty: every other
        # chart of the file became pending again. Now the old note stays, and the study's stage resumes.
        note = self.notes / "resume.json"
        note.write_text(json.dumps({"file": "resume.pine", "instruments": {"NQ": {"charts": {"2m": {"attempts": 3}}}}}), encoding="utf-8")
        before = note.read_text(encoding="utf-8")
        row = dict(self.ROW)
        with self._refusing(), mock.patch("sys.stderr"), self.assertRaises(OSError):
            learn._save_notes(self.script, {"title": "Resume"}, [row], "", "", row, "ES", "5m")
        self.assertEqual(note.read_text(encoding="utf-8"), before)
        self.assertEqual(self._leftovers(), [])

    def test_a_refused_over_cap_note_is_let_go(self):
        note = self.notes / "resume.json"
        note.write_text(json.dumps({"file": "resume.pine", "owner_answers": {"length": 12}}), encoding="utf-8")
        before = note.read_text(encoding="utf-8")
        with self._refusing(), mock.patch("sys.stderr"):
            learn._mark_over_cap(self.script, 1200, "ES|5m")
        self.assertEqual(note.read_text(encoding="utf-8"), before)
        learn._mark_over_cap(self.script, 1200, "ES|5m")
        self.assertEqual(json.loads(note.read_text(encoding="utf-8"))["over_trade_charts"], {"ES|5m": 1200})


class RestartHoldTests(_Isolated):
    """A study that Restart ended is held on its chart wherever it ran, so the hung script is not taken again at once."""

    def test_engine_4_holds_a_restarted_script(self):
        from futuresfund.roster import CHART_DESKS

        desk = CHART_DESKS[0]
        board = mock.MagicMock()
        board.cancel = threading.Event()
        released = []
        with mock.patch.object(learn, "_job_take", lambda root, timeframe: self.script), \
                mock.patch.object(learn, "_job_release", lambda *args: released.append(args)), \
                mock.patch.object(learn, "_spare_charts", lambda: [("ES", desk)]), \
                mock.patch("futuresfund.backtrader_engine.begin_study"), \
                mock.patch.object(learn, "_study", return_value="restarted"):
            self.assertEqual(learn.study_learning_on_engine_4(board, None), "restarted")
        self.assertIn((str(self.script), desk["timeframe"], "ES"), learn._held_back)
        self.assertEqual(released, [(self.script, "ES", desk["timeframe"])])

    def test_a_restarted_validated_repair_is_held(self):
        job = {"path": self.script, "timeframe": "5m", "root": "ES"}
        with mock.patch.object(learn, "collect_validated", return_value=("ready", job)), \
                mock.patch.object(learn, "_study", return_value="restarted"):
            self.assertEqual(learn.pickup_validated(None, "5 min chart developer", {}, None, 0, root="ES"), "restarted")
        self.assertIn((str(self.script), "5m", "ES"), learn._held_back)


if __name__ == "__main__":
    unittest.main()
