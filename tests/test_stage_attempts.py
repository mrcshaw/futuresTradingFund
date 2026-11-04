"""Each proving stage has its own attempts, and a stage keeps searching after a pass for a better one.

The 7, 30 and 90-day stages get 100, 150 and 200 different results, the year 300. A pass no longer ends a
stage: it keeps the pass with the most profit after drawdown, stops when STAGE_PATIENCE runs in a row found
no better pass, and hands that best pass to the next stage and the proving ledger.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import book, learn

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "futuresfund" / "static"


def _default_book(test):
    """The acceptance boxes at their code defaults, not the ones saved on this desk."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    patcher = mock.patch.object(book, "BOOK_PATH", Path(folder.name) / "book.json")
    patcher.start()
    test.addCleanup(patcher.stop)
    patcher = mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args, **kwargs: None)
    patcher.start()
    test.addCleanup(patcher.stop)


def _executor(results):
    calls = []

    def execute(params):
        calls.append((execute.source["sessions"], dict(params)))
        return results(execute.source["sessions"], params)

    execute.source = {"text": "", "stamp": 0, "sessions": None}
    return execute, calls


def _climbing(sessions, params):
    """Every run passes. The profit rises with length up to 20 and falls after it, so length 20 is the best pass."""
    length = float(params.get("length", 10))
    profit = 300 + 20 * min(length, 20) - 30 * max(0.0, length - 20)
    return {"net_profit": profit, "max_drawdown": 100, "trades": 2 * sessions, "days": sessions}


def _losing(sessions, params):
    # Two trades a session stays under the 600-a-year cap, so the trade cut does not start.
    return {"net_profit": -300 - float(params.get("length", 0)), "max_drawdown": 400, "trades": 2 * sessions, "days": sessions}


def _prove(execute, **kwargs):
    return learn._prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None, **kwargs)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)

    def test_each_stage_has_its_own_attempts_and_the_year_has_the_year_gauge(self):
        self.assertEqual((learn.STAGE_7_ATTEMPTS, learn.STAGE_30_ATTEMPTS, learn.STAGE_90_ATTEMPTS), (100, 150, 200))
        self.assertEqual(learn.ATTEMPT_LIMIT, 300)
        self.assertEqual(learn.STAGE_PATIENCE, 25)
        self.assertFalse(hasattr(learn, "STAGE_ATTEMPTS"))
        self.assertEqual([learn.stage_attempts(days) for days in (7, 30, 90, 365, None)], [100, 150, 200, 300, 300])

    def test_the_budget_is_read_when_the_stage_starts(self):
        with mock.patch.object(learn, "STAGE_30_ATTEMPTS", 7), mock.patch.object(learn, "ATTEMPT_LIMIT", 412):
            self.assertEqual(learn.stage_attempts(30), 7)
            self.assertEqual(learn.stage_attempts(None), 412)
        self.assertEqual(learn.stage_attempts(30), 150)

    def test_each_stage_searches_with_its_own_budget(self):
        limits = {}

        def search(base, execute, account_size, limit, *args, **kwargs):
            limits[kwargs["stage_days"]] = limit
            trials = learn._Trials()
            trials.append({"params": dict(base), "net_profit": 500.0, "max_drawdown": 50.0, "stage_passed": True})
            return trials

        execute, _calls = _executor(_climbing)
        with mock.patch.object(learn, "_search", search), mock.patch.object(learn, "STAGE_90_ATTEMPTS", 33), \
                mock.patch.object(learn, "_mark_stage", lambda *args, **kwargs: None):
            settings, stopped = _prove(execute)
        self.assertIsNone(stopped)
        self.assertEqual(limits, {7: 100, 30: 150, 90: 33})


class KeepSearchingTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)

    def test_a_stage_keeps_searching_after_its_first_pass_and_carries_the_best_one(self):
        execute, calls = _executor(_climbing)
        with mock.patch.object(learn, "_mark_stage", lambda *args, **kwargs: None):
            settings, stopped = _prove(execute)
        self.assertIsNone(stopped)
        week = [params for sessions, params in calls if sessions == 5]
        month = [params for sessions, params in calls if sessions == 21]
        # The first run passed, and the stage went on to find length 20.
        self.assertGreater(len(week), 2)
        self.assertEqual(execute.source["stage_runs"][7]["params"]["length"], 20)
        self.assertEqual(execute.source["stage_attempts"][7]["best_attempt"], 2)
        self.assertTrue(execute.source["stage_attempts"][7]["patience_stop"])
        # The 30-day stage starts from the best 7-day pass, not the first one.
        self.assertEqual(month[0]["length"], 20)
        self.assertEqual(settings["length"], 20)

    def test_the_best_pass_has_the_most_profit_after_drawdown_then_the_lower_drawdown(self):
        rows = [
            {"net_profit": 900.0, "max_drawdown": 500.0, "stage_passed": True},
            {"net_profit": 700.0, "max_drawdown": 200.0, "stage_passed": True},
            {"net_profit": 5000.0, "max_drawdown": 100.0, "stage_passed": False},
            {"net_profit": 600.0, "max_drawdown": 100.0, "stage_passed": True},
            {"net_profit": 700.0, "max_drawdown": 200.0, "stage_passed": True},
        ]
        # 400, 500 and 500 after drawdown: the tie goes to the lower drawdown, and an equal later pass does not replace it.
        self.assertIs(learn._best_pass(rows), rows[3])
        self.assertIs(learn._best_pass(rows[:2]), rows[1])
        self.assertIs(learn._best_pass(rows[1:2] + rows[4:]), rows[1])
        self.assertIsNone(learn._best_pass([rows[2]]))
        self.assertIsNone(learn._best_pass([]))

    def test_a_stage_stops_when_the_patience_runs_out(self):
        def worse_after_the_first(sessions, params):
            length = float(params.get("length", 10))
            return {"net_profit": 800 - 10 * abs(length - 10), "max_drawdown": 100, "trades": 6, "days": 5}

        execute, calls = _executor(worse_after_the_first)
        execute.source["sessions"] = 5
        with mock.patch.object(learn, "STAGE_PATIENCE", 5):
            trials = learn._search({"length": 10}, execute, 50000, 100, None, None, None, extras=False,
                                   developer="5 min chart developer", stage_days=7)
        self.assertTrue(trials.patience_stop)
        self.assertIs(trials.best_pass, trials[0])
        # The first pass, then five runs in a row that did not beat it.
        self.assertLessEqual(len(calls), 6)
        self.assertEqual(trials.attempt_budget, 100)

    def test_a_better_pass_starts_the_patience_over(self):
        execute, calls = _executor(_climbing)
        execute.source["sessions"] = 5
        with mock.patch.object(learn, "STAGE_PATIENCE", 3):
            trials = learn._search({"length": 10}, execute, 50000, 100, None, None, None, extras=False,
                                   developer="5 min chart developer", stage_days=7)
        self.assertTrue(trials.patience_stop)
        self.assertEqual(trials.best_pass["params"]["length"], 20)
        best_at = trials.index(trials.best_pass)
        self.assertGreater(best_at, 0)
        self.assertLessEqual(len(trials) - 1 - best_at, 3)

    def test_a_stage_that_never_passes_spends_its_whole_budget(self):
        execute, calls = _executor(_losing)
        with mock.patch.object(learn, "STAGE_7_ATTEMPTS", 30), mock.patch.object(learn, "_mark_stage", lambda *args, **kwargs: None):
            settings, stopped = _prove(execute)
        self.assertIsNone(settings)
        self.assertEqual(stopped.stage_failed, 7)
        # The old stage stopped at 20. Now a stage with no pass uses every attempt it has.
        self.assertEqual(len(stopped), 30)
        self.assertFalse(stopped.patience_stop)
        self.assertEqual(execute.source["stage_attempts"][7], {"used": 30, "budget": 30, "passed": False, "engine_runs": 30, "patience_stop": False})

    def test_the_engine_runs_of_a_stage_stay_capped_at_twice_its_attempts(self):
        execute, calls = _executor(lambda sessions, params: {"net_profit": -200, "max_drawdown": 300, "trades": 5, "days": sessions})
        execute.source["sessions"] = 5
        trials = learn._search({"length": 10, "stop_dollars": 500.0, "tp_dollars": 1000.0}, execute, 50000, 6, None, None, None,
                               extras=False, developer="5 min chart developer", stage_days=7)
        self.assertLessEqual(len(calls), 2 * 6 + 1)
        self.assertLessEqual(trials.engine_runs, 2 * 6 + 1)

    def test_a_skip_after_a_pass_does_not_start_the_next_stage(self):
        from futuresfund.backtrader_engine import clear_restarts, clear_skips, finish_study, request_skip

        self.addCleanup(clear_skips)
        self.addCleanup(clear_restarts)
        self.addCleanup(finish_study, 0)

        def skipped_on_the_third_run(sessions, params):
            if len(calls) == 3:
                request_skip(1)
            return _climbing(sessions, params)

        execute, calls = _executor(skipped_on_the_third_run)
        marks = []
        with mock.patch.object(learn, "_mark_stage", lambda path, root, timeframe, days, status, row=None, source="", fresh=False: marks.append((days, status))):
            settings, stopped = learn._prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", 0)
        self.assertIsNone(settings)
        self.assertTrue(stopped.skipped)
        self.assertFalse(hasattr(stopped, "stage_failed"))
        self.assertNotIn(21, [sessions for sessions, _params in calls])
        self.assertEqual(marks, [(7, "testing")])


class EngineCardTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)

    def tearDown(self):
        from futuresfund.backtrader_engine import clear_restarts, clear_skips, finish_study

        clear_skips()
        clear_restarts()
        finish_study(0)

    def test_the_engine_row_carries_the_budget_and_the_test(self):
        from futuresfund.backtrader_engine import begin_study, engine_status, finish_study

        begin_study(0, "card.pine", 10, limit=150, stage=30)
        row = engine_status()[0]
        self.assertEqual((row["attempt_limit"], row["stage"], row["attempt"]), (150, 30, 0))
        finish_study(0)
        row = engine_status()[0]
        self.assertEqual((row["attempt_limit"], row["stage"]), (0, 0))

    def test_a_search_tells_the_engine_its_budget_and_stage(self):
        from futuresfund.backtrader_engine import engine_status

        seen = []

        def execute(params):
            row = engine_status()[0]
            seen.append((row["attempt_limit"], row["stage"], row["attempt"]))
            return {"net_profit": -100.0 - float(params.get("length", 0)), "max_drawdown": 80.0, "trades": 20, "days": 21}

        execute.source = {"text": "", "stamp": 0, "sessions": 21}
        learn._search({"length": 10}, execute, 50000, 4, None, None, None, extras=False, developer="5 min chart developer", slot=0, stage_days=30)
        self.assertEqual(seen[0], (4, 30, 0))
        self.assertEqual(seen[1], (4, 30, 1))
        seen.clear()
        execute.source["sessions"] = None
        learn._search({"length": 10}, execute, 50000, 3, None, None, None, extras=False, developer="5 min chart developer", slot=0, year=True)
        self.assertEqual(seen[0][:2], (3, 365))

    def test_the_attempt_posts_name_the_real_limit(self):
        from futuresfund import script_diagnosis as diagnosis
        from futuresfund.board import Board

        board = Board()

        def execute(params):
            return {"net_profit": 80.0 + float(params.get("length", 0)), "max_drawdown": 50.0, "trades": 3, "days": 5, "profit_factor": 1.5, "win_rate": 50.0}

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            script = folder / "s.pine"
            script.write_text('//@version=5\nstrategy("S")\n', encoding="utf-8")
            with mock.patch.object(diagnosis, "HOME", folder / "d"), mock.patch.object(learn, "LEARNING", folder), \
                    mock.patch.object(learn, "NOTES", folder / "notes"), mock.patch.object(learn, "_ensure_worker", lambda: None):
                learn._search({"length": 10}, execute, 50000, 3, board, script, None, stage_days=7)
                learn.drain_decisions()
        texts = [message["text"] for message in board.messages]
        self.assertTrue(any(text.startswith("Attempt 1 of 3 on s.pine") for text in texts))
        self.assertTrue(any("passed the 7-day stage on attempt 1 of 3" in text for text in texts))
        self.assertTrue(any("keeps searching for more profit with less drawdown" in text for text in texts))


class LedgerAndNotesTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        from futuresfund import proving

        patcher = mock.patch.object(proving, "_home", lambda: self.home / "proving")
        patcher.start()
        self.addCleanup(patcher.stop)
        # The script sits in a temporary learning folder, so the search keeps it and saves nothing in the desk's folders.
        learning = self.home / "learningStrategies"
        learning.mkdir()
        self.script = learning / "ledger.pine"
        self.script.write_text('//@version=5\nstrategy("Ledger")\nlength = input.int(10, "Length")\n', encoding="utf-8")
        for name, value in (("NOTES", self.home / "researchNotes"), ("LEARNING", learning)):
            patcher = mock.patch.object(learn, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _entry(self):
        from futuresfund import proving

        return next(row for row in proving.rows() if row["file"] == "ledger.pine")

    def test_the_ledger_keeps_the_best_pass_and_the_attempts(self):
        execute, _calls = _executor(_climbing)
        execute.source["text"] = self.script.read_text(encoding="utf-8")
        settings, stopped = learn._prove_stages(
            {"length": 10}, execute, None, self.script, None, None, "5 min chart developer", "Chart researcher", None,
            root="ES", timeframe="5m",
        )
        self.assertIsNone(stopped)
        entry = self._entry()
        week = entry["passed"]["7"]
        self.assertEqual(week["params"]["length"], 20)
        self.assertEqual(week["best_attempt"], 2)
        self.assertEqual(week["attempt_budget"], 100)
        self.assertEqual(week["attempts_used"], execute.source["stage_attempts"][7]["used"])
        self.assertEqual(entry["attempts"]["7"], {"used": week["attempts_used"], "budget": 100})
        self.assertEqual(entry["attempts"]["30"]["budget"], 150)
        self.assertEqual(entry["attempts"]["90"]["budget"], 200)
        # The year is testing with its budget and no attempts used yet.
        self.assertEqual(entry["status"], "testing")
        self.assertEqual(entry["attempts"]["365"], {"used": None, "budget": 300})
        saved = (self.home / "proving" / week["pine"]).read_text(encoding="utf-8")
        self.assertIn("input.int(20", saved)

    def test_a_failed_stage_records_its_attempts_on_the_ledger(self):
        execute, _calls = _executor(_losing)
        with mock.patch.object(learn, "STAGE_7_ATTEMPTS", 12), mock.patch.object(learn, "STAGE_REVIEW_ON", False):
            learn._prove_stages(
                {"length": 10}, execute, None, self.script, None, None, "5 min chart developer", "Chart researcher", None,
                root="ES", timeframe="5m",
            )
        entry = self._entry()
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["attempts"], {"7": {"used": 12, "budget": 12}})

    def test_starting_the_7_day_stage_again_clears_the_old_attempts(self):
        from futuresfund import proving

        proving.mark("ledger.pine", "ES", "5m", 30, "failed", {"attempts_used": 150, "attempt_budget": 150})
        self.assertIn("30", self._entry()["attempts"])
        proving.mark("ledger.pine", "ES", "5m", 7, "testing", {"attempt_budget": 100})
        self.assertEqual(self._entry()["attempts"], {"7": {"used": None, "budget": 100}})

    def test_the_notes_keep_the_attempts_each_test_used(self):
        notes = self.home / "researchNotes"
        trials = learn._Trials([{"params": {"length": 20}, "net_profit": 700.0, "max_drawdown": 100.0, "trades": 120, "days": 260}])
        trials.attempt_budget = 300
        stages = {7: {"used": 27, "budget": 100, "passed": True, "best_attempt": 2}, 365: {"used": 1, "budget": 300}}
        learn._save_notes(self.script, {"title": "Notes"}, trials, "", "", dict(trials[0]), "ES", "5m", stages=stages)
        saved = json.loads((notes / "ledger.json").read_text(encoding="utf-8"))
        chart = saved["instruments"]["ES"]["charts"]["5m"]
        self.assertEqual(chart["stages"]["7"], {"used": 27, "budget": 100, "passed": True, "best_attempt": 2})
        self.assertEqual(chart["stages"]["365"], {"used": 1, "budget": 300})
        self.assertEqual(chart["attempt_budget"], 300)


class CreationAttemptTests(unittest.TestCase):
    def test_engine_4_keeps_its_own_count_for_drafts(self):
        import inspect

        from futuresfund import strategy_team

        self.assertEqual(strategy_team.CREATION_ATTEMPTS, 100)
        source = inspect.getsource(strategy_team._develop_queued)
        self.assertIn("CREATION_ATTEMPTS,", source)
        self.assertNotIn("ATTEMPT_LIMIT", source)


class KnobAndPageTests(unittest.TestCase):
    def test_the_knobs_are_on_their_nodes_with_the_new_ranges(self):
        from futuresfund import workflow_design as wd

        knobs = {knob["key"]: knob for knob in wd.KNOBS}
        self.assertNotIn("stage_attempts", knobs)
        expected = {
            "stage_7_attempts": ("stage7", 1, 1000, 100),
            "stage_30_attempts": ("stage30", 1, 1000, 150),
            "stage_90_attempts": ("stage90", 1, 1000, 200),
            "stage_patience": ("stage7,stage30,stage90", 1, 1000, 25),
            "attempt_limit": ("year", 10, 1000, 300),
            "creation_attempts": ("engine4", 10, 1000, 100),
        }
        defaults = wd.defaults()
        for key, (node, low, high, value) in expected.items():
            with self.subTest(key=key):
                self.assertEqual((knobs[key]["node"], knobs[key]["min"], knobs[key]["max"], defaults[key]), (node, low, high, value))
                self.assertNotIn("days", knobs[key])

    def test_the_catalog_lists_the_knobs_on_the_stage_nodes(self):
        from futuresfund import workflow_design as wd

        catalog = json.loads((STATIC / "workflow_catalog.json").read_text(encoding="utf-8"))
        ids = [node["id"] for node in catalog["nodes"]]
        self.assertEqual(len(ids), len(set(ids)))
        for edge in catalog["edges"]:
            self.assertIn(edge["from"], ids)
            self.assertIn(edge["to"], ids)
        keys = {knob["key"] for knob in wd.KNOBS}
        nodes = {node["id"]: node for node in catalog["nodes"]}
        for node in catalog["nodes"]:
            for key in node.get("knobs") or []:
                self.assertIn(key, keys, f"{node['id']} lists {key}")
        for stage in ("stage7", "stage30", "stage90"):
            self.assertIn(f"{stage.replace('stage', 'stage_')}_attempts", nodes[stage]["knobs"])
            self.assertIn("stage_patience", nodes[stage]["knobs"])
            self.assertNotIn("stage_attempts", nodes[stage]["knobs"])
        self.assertIn("creation_attempts", nodes["engine4"]["knobs"])
        self.assertNotIn("attempt_limit", nodes["engine4"]["knobs"])
        text = json.dumps(catalog)
        self.assertNotIn("20 tries, no pass", text)
        self.assertNotIn("STAGE_ATTEMPTS = 20", text)

    def test_the_page_mirror_of_the_knobs_matches_the_code(self):
        from futuresfund import workflow_design as wd

        js = (STATIC / "workflow.js").read_text(encoding="utf-8")
        defaults = wd.defaults()
        for knob in wd.KNOBS:
            if knob["key"] not in ("stage_7_attempts", "stage_30_attempts", "stage_90_attempts", "stage_patience", "attempt_limit", "creation_attempts"):
                continue
            with self.subTest(key=knob["key"]):
                pattern = (
                    rf'\["{knob["key"]}", "{re.escape(knob["label"])}", "{knob["node"]}", "int", {knob["min"]}, {knob["max"]}, 1, '
                    rf'"{knob["unit"]}", {defaults[knob["key"]]},'
                )
                self.assertRegex(js, pattern)
        self.assertNotIn('["stage_attempts"', js)

    def test_the_cards_show_the_attempt_of_its_budget_and_the_test(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        flow = (STATIC / "workflow.js").read_text(encoding="utf-8")
        self.assertIn("function attemptText(engine)", app)
        self.assertIn("engine.attempt_limit", app)
        self.assertIn("-day test", app)
        self.assertIn("function engineAttempt(engine)", flow)
        self.assertIn("engine?.attempt_limit", flow)
        self.assertIn("engineAttempt(engine)", flow.split("function enginesHtml(node)")[1])
        # The proving table shows the attempts each stage used of its budget.
        self.assertIn("function provingAttempts(row, days)", app)
        self.assertIn("attempts_used", app)

    @unittest.skipUnless(shutil.which("node"), "the page text runs in Node")
    def test_the_page_words_the_attempt_the_budget_and_the_test(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        flow = (STATIC / "workflow.js").read_text(encoding="utf-8")

        def function(source, name):
            start = source.index(f"function {name}(")
            end = source.index("\n}\n", start) + 3
            return source[start:end]

        engines = [
            {"attempt": 12, "attempt_limit": 100, "stage": 7},
            {"attempt": 3, "attempt_limit": 300, "stage": 365},
            {"attempt": 0, "attempt_limit": 150, "stage": 30},
            {"attempt": 5, "attempt_limit": 100, "stage": 0},
            {},
        ]
        rows = [
            ({"passed": {"7": {"attempts_used": 37, "attempt_budget": 100, "best_attempt": 12}}, "attempts": {"7": {"used": 37, "budget": 100}}}, 7),
            ({"passed": {}, "attempts": {"30": {"used": None, "budget": 150}}}, 30),
            ({"passed": {}, "attempts": {"90": {"used": 200, "budget": 200}}}, 90),
            ({"passed": {"7": {"net_profit": 400}}}, 7),
        ]
        script = (
            function(app, "attemptText") + function(flow, "engineAttempt") + function(app, "provingAttempts")
            + f"const engines = {json.dumps(engines)};\nconst rows = {json.dumps(rows)};\n"
            + "console.log(JSON.stringify({app: engines.map(attemptText), flow: engines.map(engineAttempt), "
            + "proving: rows.map(([row, days]) => provingAttempts(row, days))}));\n"
        )
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        shown = json.loads(done.stdout)
        expected = ["attempt 12 of 100 · 7-day test", "attempt 3 of 300 · year", "30-day test · 150 attempts", "attempt 5 of 100", ""]
        self.assertEqual(shown["app"], expected)
        self.assertEqual(shown["flow"], expected)
        self.assertEqual(shown["proving"], ["37 of 100 attempts, best #12", "150 attempts allowed", "200 of 200 attempts", ""])


if __name__ == "__main__":
    unittest.main()
