"""The Research workflow Design page: its settings file, the research switches it drives, and its API.

Every test points config.WORKFLOW_DESIGN_PATH at a temporary file, so the owner's saved settings
are never read, and every module value a knob sets is put back after each test.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from futuresfund import config, learn
from futuresfund import workflow_design as wd

PINE = "\n".join([
    "//@version=5",
    'strategy("Design test", overlay=true)',
    'length = input.int(10, "Length")',
    "if close > open",
    '    strategy.entry("Long", strategy.long)',
    "",
])


def _default_book(test):
    """The acceptance boxes at their code defaults, not the ones saved on this desk."""
    from futuresfund import book

    folder = TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    patcher = mock.patch.object(book, "BOOK_PATH", Path(folder.name) / "book.json")
    patcher.start()
    test.addCleanup(patcher.stop)


class _Isolated(unittest.TestCase):
    """A temporary settings file, fresh captured defaults, and every knob's module value restored afterwards."""

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.settings = self.home / "research_workflow.json"
        patcher = mock.patch.object(config, "WORKFLOW_DESIGN_PATH", self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        snapshot = {
            (knob["module"], knob["attr"]): getattr(importlib.import_module(knob["module"]), knob["attr"])
            for knob in wd.KNOBS
        }

        def restore():
            for (module, attr), value in snapshot.items():
                setattr(importlib.import_module(module), attr, value)

        self.addCleanup(restore)
        wd._RAW.clear()
        self.addCleanup(wd._RAW.clear)
        wd._CHANGES.clear()
        self.addCleanup(wd._CHANGES.clear)
        wd._LEARNING_FILES.clear()
        self.addCleanup(wd._LEARNING_FILES.clear)

    def saved(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))


class DefaultTests(_Isolated):
    def test_the_code_values_are_todays_behaviour(self):
        from futuresfund import objective, research_bars, script_diagnosis

        self.assertIs(learn.USE_ALERT_BARS, True)
        self.assertIs(learn.READ_BEFORE_TESTING, True)
        self.assertIs(learn.ALLOW_REPAIR_REWRITES, True)
        self.assertEqual(learn.STAGES_OFF, frozenset())
        self.assertIs(learn.STAGE_REVIEW_ON, True)
        self.assertIs(learn.REOPEN_FOR_ANSWERS, True)
        self.assertIs(learn.RECHECK_BEFORE_FILING, False)
        self.assertEqual(learn.COMPILE_REPAIR_TRIES, 3)
        # The researcher's repair count keeps its own name and value.
        self.assertEqual(learn.REPAIR_TRIES, 3)
        self.assertEqual(research_bars.LIVE_REFRESH, 900)
        self.assertEqual(script_diagnosis.RESPONSE_SESSIONS, 21)
        # The extra trades-a-week floor is off: the fewest trades come from the library count, pro-rated.
        self.assertEqual(objective.STAGE_TRADES_PER_WEEK, 0)
        expected = {
            "use_alert_bars": True, "live_refresh": 900, "compile_repair_tries": 3, "read_before_testing": True,
            "response_sessions": 21, "empty_charts": 3, "allow_repair_rewrites": True, "stage_7_on": True,
            "stage_30_on": True, "stage_90_on": True, "stage_7_attempts": 100, "stage_30_attempts": 150,
            "stage_90_attempts": 200, "stage_patience": 25, "trade_cut_attempts": 12, "stage_trades_per_week": 0.0,
            "stage_review": True, "stage_reviews": 6, "review_backlog": 12, "reopen_for_answers": True,
            "reopen_limit": 3, "attempt_limit": 300, "loss_look": 40, "impact_percent": 0.05, "creation_attempts": 100,
            "flat_runs": 20, "recheck_before_filing": False, "email_questions": True, "inbox_poll_seconds": 120,
            # One worker for every core but one, at least 2: the code value depends on the machine.
            "engines_at_once": max(2, (os.cpu_count() or 2) - 1),
        }
        self.assertEqual(wd.defaults(), expected)

    def test_every_knob_has_the_fields_the_page_draws(self):
        for knob in wd.KNOBS:
            with self.subTest(knob=knob["key"]):
                for name in ("key", "label", "node", "type", "min", "max", "step", "unit", "help", "module", "attr"):
                    self.assertIn(name, knob)
                self.assertIn(knob["type"], {"bool", "int", "float"})
                if knob["type"] != "bool":
                    self.assertLessEqual(knob["min"], wd.defaults()[knob["key"]])
                    self.assertGreaterEqual(knob["max"], wd.defaults()[knob["key"]])

    def test_the_saved_file_is_only_read_where_research_starts(self):
        source = inspect.getsource(learn)
        self.assertEqual(source.count("apply_saved("), 1)
        self.assertIn("apply_saved(", inspect.getsource(learn._run_learning))
        self.assertNotIn("workflow_design", inspect.getsource(learn.begin_research))
        self.assertNotIn("WORKFLOW_DESIGN_PATH", inspect.getsource(learn))


class ValidationTests(_Isolated):
    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ValueError):
            wd.validate("no_such_knob", 3)
        with self.assertRaises(ValueError):
            wd.save_knobs({"no_such_knob": 3})
        with self.assertRaises(ValueError):
            wd.reset(["no_such_knob"])

    def test_a_value_out_of_range_is_refused(self):
        for key, value in (("stage_7_attempts", 0), ("stage_7_attempts", 1001), ("attempt_limit", 1001), ("stage_patience", 0), ("live_refresh", 30), ("impact_percent", 0.9), ("flat_runs", 2)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                wd.validate(key, value)

    def test_a_switch_takes_only_true_or_false(self):
        for value in (1, 0, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wd.validate("use_alert_bars", value)
        self.assertIs(wd.validate("use_alert_bars", False), False)

    def test_a_number_takes_no_switch_and_a_count_takes_a_whole_number(self):
        with self.assertRaises(ValueError):
            wd.validate("stage_30_attempts", True)
        with self.assertRaises(ValueError):
            wd.validate("stage_30_attempts", 2.5)
        with self.assertRaises(ValueError):
            wd.validate("stage_30_attempts", "many")
        self.assertEqual(wd.validate("stage_30_attempts", 25.0), 25)
        self.assertEqual(wd.validate("stage_30_attempts", "25"), 25)
        self.assertEqual(wd.validate("stage_trades_per_week", 3.5), 3.5)

    def test_the_extra_weekly_floor_runs_from_0_for_off_to_10(self):
        from futuresfund import objective

        knob = next(row for row in wd.KNOBS if row["key"] == "stage_trades_per_week")
        self.assertEqual((knob["min"], knob["max"]), (0, 10))
        self.assertIn("0 is off", knob["help"])
        self.assertEqual(wd.validate("stage_trades_per_week", 0), 0.0)
        for value in (-0.5, 10.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wd.validate("stage_trades_per_week", value)
        wd.save_knobs({"stage_trades_per_week": 3})
        self.assertEqual(objective.STAGE_TRADES_PER_WEEK, 3)
        self.assertEqual(objective.stage_bars(7, {"use_trades": True, "min_trades": 60})["min_trades"], 3)
        wd.reset(["stage_trades_per_week"])
        self.assertEqual(objective.STAGE_TRADES_PER_WEEK, 0)
        self.assertEqual(objective.stage_bars(7, {"use_trades": True, "min_trades": 60})["min_trades"], 2)

    def test_one_wrong_value_saves_nothing(self):
        with self.assertRaises(ValueError):
            wd.save_knobs({"stage_7_attempts": 25, "flat_runs": 1})
        self.assertFalse(self.settings.exists())
        self.assertEqual(learn.STAGE_7_ATTEMPTS, 100)


class ApplyResetTests(_Isolated):
    def test_save_applies_the_values_and_reset_puts_the_code_values_back(self):
        from futuresfund import objective, research_bars, script_diagnosis

        before = {
            (knob["module"], knob["attr"]): getattr(importlib.import_module(knob["module"]), knob["attr"]) for knob in wd.KNOBS
        }
        result = wd.save_knobs({
            "stage_7_attempts": 25, "use_alert_bars": False, "stage_30_on": False, "stage_trades_per_week": 4,
            "live_refresh": 120, "response_sessions": 30, "recheck_before_filing": True, "compile_repair_tries": 0,
            "impact_percent": 0.1,
        })
        self.assertEqual(learn.STAGE_7_ATTEMPTS, 25)
        self.assertEqual(learn.stage_attempts(7), 25)
        self.assertIs(learn.USE_ALERT_BARS, False)
        self.assertEqual(learn.STAGES_OFF, frozenset({30}))
        # A whole number stays a whole number where the code wrote one, so the text it builds reads the same.
        self.assertEqual(objective.STAGE_TRADES_PER_WEEK, 4)
        self.assertIsInstance(objective.STAGE_TRADES_PER_WEEK, int)
        self.assertEqual(research_bars.LIVE_REFRESH, 120)
        self.assertEqual(script_diagnosis.RESPONSE_SESSIONS, 30)
        self.assertIs(learn.RECHECK_BEFORE_FILING, True)
        self.assertEqual(learn.COMPILE_REPAIR_TRIES, 0)
        self.assertEqual(learn.REPAIR_TRIES, 3)
        self.assertEqual(learn.IMPACT_PERCENT, 0.1)
        rows = {row["key"]: row for row in result["knobs"]}
        self.assertTrue(rows["stage_7_attempts"]["changed"])
        self.assertEqual(rows["stage_7_attempts"]["value"], 25)
        self.assertEqual(rows["stage_7_attempts"]["default"], 100)
        self.assertEqual(rows["stage_7_attempts"]["current"], 25)
        self.assertFalse(rows["stage_30_attempts"]["changed"])
        self.assertFalse(rows["flat_runs"]["changed"])
        stored = self.saved()
        self.assertEqual(stored["knobs"]["stage_7_attempts"], 25)
        self.assertEqual(stored["knobs"]["stage_30_on"], False)
        self.assertNotIn("flat_runs", stored["knobs"])
        self.assertTrue(stored["updated"])

        wd.reset(["stage_7_attempts", "stage_30_on"])
        self.assertEqual(learn.STAGE_7_ATTEMPTS, 100)
        self.assertEqual(learn.STAGES_OFF, frozenset())
        self.assertNotIn("stage_7_attempts", self.saved()["knobs"])
        self.assertIn("live_refresh", self.saved()["knobs"])

        wd.reset([])
        self.assertEqual(self.saved()["knobs"], {})
        after = {
            (knob["module"], knob["attr"]): getattr(importlib.import_module(knob["module"]), knob["attr"]) for knob in wd.KNOBS
        }
        self.assertEqual(after, before)
        for name, value in before.items():
            self.assertIs(type(after[name]), type(value), name)

    def test_saving_the_code_value_stores_nothing(self):
        wd.save_knobs({"stage_90_attempts": 30})
        wd.save_knobs({"stage_90_attempts": 200})
        self.assertEqual(self.saved()["knobs"], {})
        self.assertEqual(learn.STAGE_90_ATTEMPTS, 200)

    def test_research_start_applies_the_valid_saved_values_only(self):
        self.settings.write_text(json.dumps({
            "knobs": {"stage_30_attempts": 30, "stage_7_on": False, "flat_runs": 1000, "no_such_knob": 5, "loss_look": 40},
        }), encoding="utf-8")
        changed = wd.apply_saved()
        self.assertEqual(changed, {"stage_30_attempts": 30, "stage_7_on": False})
        self.assertEqual(learn.STAGE_30_ATTEMPTS, 30)
        self.assertEqual(learn.STAGES_OFF, frozenset({7}))
        self.assertEqual(learn.FLAT_RUNS, 20)
        line = wd.describe(changed)
        self.assertIn("30-day stage attempts: 30 (the code value is 150)", line)
        self.assertIn("Run the 7-day stage: off", line)

    def test_the_old_shared_stage_attempts_key_is_ignored_and_dropped_on_the_next_save(self):
        # Before each stage had its own count, one "stage_attempts" number was saved for all three.
        self.settings.write_text(json.dumps({"knobs": {"stage_attempts": 25, "stage_review": False}}), encoding="utf-8")
        self.assertEqual(wd.apply_saved(), {"stage_review": False})
        self.assertEqual((learn.STAGE_7_ATTEMPTS, learn.STAGE_30_ATTEMPTS, learn.STAGE_90_ATTEMPTS), (100, 150, 200))
        self.assertTrue(all(row["key"] != "stage_attempts" for row in wd.knob_rows()))
        wd.save_knobs({"stage_patience": 40})
        self.assertEqual(self.saved()["knobs"], {"stage_review": False, "stage_patience": 40})
        self.assertEqual(learn.STAGE_PATIENCE, 40)

    def test_no_file_changes_nothing(self):
        self.assertEqual(wd.apply_saved(), {})
        self.assertEqual(learn.STAGE_7_ATTEMPTS, 100)
        self.assertEqual(learn.STAGES_OFF, frozenset())

    def test_the_defaults_are_the_values_before_anything_was_applied(self):
        wd.save_knobs({"attempt_limit": 150})
        self.assertEqual(learn.ATTEMPT_LIMIT, 150)
        self.assertEqual(learn.stage_attempts(None), 150)
        self.assertEqual(wd.defaults()["attempt_limit"], 300)

    def test_research_start_applies_the_file_before_anything_else(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings.write_text(json.dumps({"knobs": {"stage_7_attempts": 33}}), encoding="utf-8")
        seen = {}

        class Started(Exception):
            pass

        def first_step():
            seen["attempts"] = learn.STAGE_7_ATTEMPTS
            raise Started

        from futuresfund.board import Board

        board = Board()
        with mock.patch.object(learn, "LEARNED", Path(folder.name) / "learned"), \
                mock.patch.object(learn, "NOTES", Path(folder.name) / "notes"), \
                mock.patch.object(learn, "clear_holds", first_step):
            with self.assertRaises(Started):
                learn._run_learning(board)
        self.assertEqual(seen["attempts"], 33)
        self.assertTrue(any("7-day stage attempts: 33" in message["text"] for message in board.messages))


class LayoutQueueTests(_Isolated):
    def test_the_layout_is_saved_and_cleared(self):
        self.assertEqual(wd.save_layout({"stage7": [80, 710.25], "year": [1100.0, 700]}), {"layout": {"stage7": [80, 710.2], "year": [1100, 700]}})
        self.assertEqual(wd.load()["layout"], {"stage7": [80, 710.2], "year": [1100, 700]})
        self.assertEqual(wd.state()["layout"], {"stage7": [80, 710.2], "year": [1100, 700]})
        self.assertEqual(wd.save_layout({}), {"layout": {}})
        self.assertEqual(wd.load()["layout"], {})

    def test_a_wrong_layout_is_refused(self):
        for layout in ({"stage7": [1]}, {"stage7": ["a", 2]}, {"bad id!": [1, 2]}, {"stage7": [1e9, 2]}, {"stage7": [True, 2]}, ["stage7"]):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                wd.save_layout(layout)

    def test_the_queue_is_written_and_read_by_the_engines(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        notes = Path(folder.name)
        with mock.patch.object(learn, "NOTES", notes):
            learn._QUEUE_FIRST.clear()
            self.addCleanup(learn._QUEUE_FIRST.clear)
            result = wd.save_queue([" first.txt ", "second.pine", "first.txt", ""])
            self.assertEqual(result, {"queue_first": ["first.txt", "second.pine"]})
            self.assertEqual(json.loads((notes / "queue_first.json").read_text(encoding="utf-8")), ["first.txt", "second.pine"])
            self.assertEqual(learn._queue_first(), ["first.txt", "second.pine"])
            self.assertFalse((notes / "queue_first.json.tmp").exists())
            for names in (["../up.txt"], ["a/b.txt"], [5], "first.txt"):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    wd.save_queue(names)
            self.assertEqual(learn._queue_first(), ["first.txt", "second.pine"])

    def test_the_learning_files_are_listed_in_queue_order(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        learning = Path(folder.name) / "learning"
        notes = Path(folder.name) / "notes"
        learning.mkdir()
        notes.mkdir()
        for name in ("alpha.pine", "beta.pine", "gamma.pine"):
            (learning / name).write_text(PINE, encoding="utf-8")
        (learning / "readme.txt").write_text("not a script", encoding="utf-8")
        (notes / "queue_first.json").write_text(json.dumps(["gamma.pine"]), encoding="utf-8")
        with mock.patch.object(learn, "LEARNING", learning), mock.patch.object(learn, "NOTES", notes):
            learn._QUEUE_FIRST.clear()
            self.addCleanup(learn._QUEUE_FIRST.clear)
            files = wd.learning_files()
            self.assertEqual(files, [path.name for path in learn.queue(learning)])
            self.assertEqual(files[0], "gamma.pine")
            self.assertNotIn("readme.txt", files)
            self.assertEqual(wd.state()["queue_first"], ["gamma.pine"])


class LiveCountTests(_Isolated):
    def test_the_folders_the_waiting_list_and_the_ledger_are_counted(self):
        from futuresfund import library

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        places = {name: base / name for name in ("learning", "created", "learned", "cannot", "drafts", "unproven", "notes", "library")}
        for place in places.values():
            place.mkdir()
        (places["learning"] / "one.pine").write_text(PINE, encoding="utf-8")
        (places["learning"] / "two.pine").write_text(PINE, encoding="utf-8")
        (places["learned"] / "done.txt").write_text(PINE, encoding="utf-8")
        (places["cannot"] / "bad.pine").write_text(PINE, encoding="utf-8")
        (places["cannot"] / "bad.reason.txt").write_text("It could not run.", encoding="utf-8")
        (places["unproven"] / "created_x.pine").write_text(PINE, encoding="utf-8")
        (places["unproven"] / "created_x.reason.txt").write_text("No profit.", encoding="utf-8")
        (places["unproven"] / "created_x.py").write_text("# twin", encoding="utf-8")
        # The awaiting folder sits next to cannotRunStrategies. Its .question.txt notes are not scripts.
        waiting = base / "awaitingAnswerStrategies"
        waiting.mkdir()
        (waiting / "asked.pine").write_text(PINE, encoding="utf-8")
        (waiting / "asked.question.txt").write_text("FFQ-abc123 asked.pine", encoding="utf-8")
        (places["library"] / "untested_library.json").write_text(json.dumps({"rows": [{"id": "a"}, {"id": "b"}]}), encoding="utf-8")
        (places["library"] / "library_index.json").write_text(json.dumps({"rows": [{"id": "c"}]}), encoding="utf-8")
        ledger = places["notes"] / "proving" / "stages.json"
        ledger.parent.mkdir()
        ledger.write_text(json.dumps({
            "a.pine|ES|5m": {"file": "a.pine", "root": "ES", "timeframe": "5m", "stage": 30, "status": "testing", "passed": {"7": {}}, "updated": "2026-11-06 10:00:00"},
            "b.pine|NQ|2m": {"file": "b.pine", "root": "NQ", "timeframe": "2m", "stage": 7, "status": "failed", "passed": {}, "updated": "2026-11-06 09:00:00"},
            "c.pine|ES|15m": {"file": "c.pine", "root": "ES", "timeframe": "15m", "stage": 365, "status": "passed", "passed": {"7": {}, "30": {}, "90": {}, "365": {}}, "updated": "2026-11-06 08:00:00"},
            "d.pine|GC|5m": {"file": "d.pine", "root": "GC", "timeframe": "5m", "stage": 90, "status": "stopped", "passed": {"7": {}, "30": {}}, "updated": "2026-11-06 07:00:00"},
        }), encoding="utf-8")
        with mock.patch.object(learn, "LEARNING", places["learning"]), mock.patch.object(learn, "CREATED", places["created"]), \
                mock.patch.object(learn, "LEARNED", places["learned"]), mock.patch.object(learn, "CANNOT_RUN", places["cannot"]), \
                mock.patch.object(learn, "DRAFTS", base / "missing"), mock.patch.object(learn, "UNPROVEN", places["unproven"]), \
                mock.patch.object(learn, "NOTES", places["notes"]), mock.patch.object(library, "_NOTES", places["library"]):
            learn._QUEUE_FIRST.clear()
            self.addCleanup(learn._QUEUE_FIRST.clear)
            found = wd.state()["live"]
        self.assertEqual(found["folders"], {"learning": 2, "created": 0, "learned": 1, "cannot_run": 1, "awaiting": 1, "drafts": 0, "unproven": 1})
        self.assertIsInstance(found["questions"], list)
        self.assertEqual(found["waiting"], 2)
        self.assertEqual(found["shelf"], 1)
        self.assertEqual(found["ledger"]["7"], {"testing": 0, "passed": 3, "failed": 1, "stopped": 0})
        self.assertEqual(found["ledger"]["30"], {"testing": 1, "passed": 2, "failed": 0, "stopped": 0})
        self.assertEqual(found["ledger"]["90"], {"testing": 0, "passed": 1, "failed": 0, "stopped": 1})
        self.assertEqual(found["ledger"]["365"], {"testing": 0, "passed": 1, "failed": 0, "stopped": 0})
        self.assertEqual(found["testing"], [{"file": "a.pine", "root": "ES", "timeframe": "5m", "stage": 30}])


def _trial(stage, profit, trades, drawdown=100.0, change="x"):
    row = {
        "net_profit": profit, "max_drawdown": drawdown, "trades": trades, "win_rate": 50.0, "profit_factor": 1.4,
        "days": {7: 5, 30: 21, 90: 64}.get(stage, 260), "change": change, "impact": f"{change} moved.", "timeframe": "5m",
        "instrument": "ES", "biggest_loser": -100.0, "stop_dollars": 250.0,
    }
    if stage != 365:
        row["stage_days"] = stage
    return row


class ChangeImpactTests(_Isolated):
    def setUp(self):
        super().setUp()
        _default_book(self)
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.notes = Path(folder.name)
        patcher = mock.patch.object(learn, "NOTES", self.notes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _note(self, name, charts, age_hours=0.0):
        path = self.notes / name
        path.write_text(json.dumps({"file": name.replace(".json", ".pine"), "instruments": {"ES": {"charts": charts}}}), encoding="utf-8")
        if age_hours:
            stamp = time.time() - age_hours * 3600
            os.utime(path, (stamp, stamp))
        return path

    def test_each_attempt_is_measured_against_the_one_before_on_its_chart_and_stage(self):
        from futuresfund import objective

        week = [_trial(7, -100.0, 3, change="Starting settings"), _trial(7, 250.0, 4, change="length 10 to 12"), _trial(7, 0.0, 0, 0.0, change="length 12 to 30")]
        year = [_trial(365, 5000.0, 150, 900.0, change="Starting settings"), _trial(365, 4000.0, 140, 1500.0, change="length 10 to 8")]
        self._note("alpha.json", {"5m": {"trials": week}, "15m": {"trials": year}})
        self._note("old.json", {"5m": {"trials": week}}, age_hours=100)
        (self.notes / "queue_first.json").write_text("[]", encoding="utf-8")
        found = wd.changes(80)
        rows = found["rows"]
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["file"] for row in rows}, {"alpha.pine"})
        weekly = sorted((row for row in rows if row["stage"] == 7), key=lambda row: row["attempt"])
        self.assertEqual([row["attempt"] for row in weekly], [1, 2, 3])
        self.assertIsNone(weekly[0]["d_profit"])
        self.assertEqual(weekly[0]["verdict"], "flat")
        self.assertEqual(weekly[1]["d_profit"], 350.0)
        self.assertEqual(weekly[1]["d_trades"], 1)
        expected = {"closer": "toward", "farther": "away"}.get(objective.scale_move(week[0], week[1], 50000), "flat")
        self.assertEqual(weekly[1]["verdict"], expected)
        self.assertEqual(weekly[2]["verdict"], "no_trades")
        self.assertEqual(weekly[1]["misses"], list(objective.acceptance_misses(week[1], days=7)))
        self.assertEqual(weekly[1]["passes"], not weekly[1]["misses"])
        self.assertEqual(weekly[1]["trades_per_week"], objective.trades_per_week(week[1]))
        yearly = [row for row in rows if row["stage"] == 365]
        self.assertEqual(len(yearly), 2)
        self.assertEqual(sorted(row["timeframe"] for row in yearly), ["15m", "15m"])
        second = next(row for row in yearly if row["attempt"] == 2)
        self.assertEqual(second["d_drawdown"], 600.0)
        self.assertEqual(second["misses"], list(objective.acceptance_misses(year[1])))
        # No ledger row lists this chart, so only the notes file's time is known, shown as "before" it.
        self.assertEqual(second["noted"], "")
        self.assertTrue(second["noted_before"])
        summary = found["summary"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["toward"] + summary["away"] + summary["flat"] + summary["no_trades"], 5)
        self.assertEqual(summary["passes"], sum(1 for row in rows if row["passes"]))
        self.assertFalse(any(key.startswith("_") for row in rows for key in row))

    def test_the_newest_chart_comes_first_and_the_limit_holds(self):
        self._note("alpha.json", {"5m": {"trials": [_trial(7, 10.0 * index, 3) for index in range(6)]}})
        rows = wd.changes(4)["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["attempt"] for row in rows], [6, 5, 4, 3])

    def test_the_rows_are_kept_for_a_minute(self):
        self._note("alpha.json", {"5m": {"trials": [_trial(7, 10.0, 3)]}})
        first = wd.changes(80)
        self._note("beta.json", {"5m": {"trials": [_trial(7, 20.0, 3)]}})
        self.assertIs(wd.changes(80), first)
        wd._CHANGES.clear()
        self.assertEqual(len(wd.changes(80)["rows"]), 2)

    def test_a_row_without_its_session_count_does_not_load_the_bars(self):
        row = _trial(365, 5000.0, 150)
        row.pop("days")
        self._note("alpha.json", {"5m": {"trials": [row, dict(row, net_profit=6000.0)]}})
        with mock.patch("futuresfund.research_bars.research_series", side_effect=AssertionError("no bars may load")):
            rows = wd.changes(80)["rows"]
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["trades_per_week"])


class _Study:
    """Drive learn._study_chart to the end with everything around the switches replaced."""

    def __init__(self, test: unittest.TestCase):
        self.test = test
        folder = TemporaryDirectory()
        test.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.path = base / "design.pine"
        self.path.write_text(PINE, encoding="utf-8")
        self.read = mock.Mock(side_effect=lambda board, path, execute, facts, settings, researcher, slot: (settings, facts))
        self.prove = mock.Mock(return_value=({"length": 10}, None))
        self.review = mock.Mock()
        self.gate_during_year = "not searched"
        self.send = mock.Mock(side_effect=AssertionError("no email in a test"))

        def search(*args, **kwargs):
            self.gate_during_year = getattr(learn._stage_gate, "check", None)
            trials = learn._Trials()
            trials.append({"net_profit": -100.0, "max_drawdown": 200.0, "trades": 40, "days": 260, "engine": "pinets", "params": {"length": 10}})
            return trials

        def execute(params):
            return {}

        execute.source = {"text": PINE, "stamp": 0, "sessions": None}
        bars = [{"t": 120 * index, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1} for index in range(100)]
        self.patches = [
            mock.patch.object(learn, "_is_strategy_file", lambda path: True),
            mock.patch.object(learn, "_in_learning", lambda path: True),
            mock.patch.object(learn, "_read_notes", lambda path: {}),
            mock.patch.object(learn, "pending_for_chart", lambda notes, timeframe, instruments=None: ["ES"]),
            mock.patch.object(learn, "_copy_finished", lambda path, timeframe, root: False),
            mock.patch.object(learn, "promote_fixed_exits", lambda text: text),
            mock.patch.object(learn, "promote_fixed_pivots", lambda text: text),
            mock.patch.object(learn, "describe_later", lambda *args: None),
            mock.patch.object(learn, "file_indicators", lambda *args: []),
            mock.patch.object(learn, "_load_frames", lambda root="ES", **kwargs: {"2m": bars, "5m": bars, "15m": bars}),
            mock.patch.object(learn, "_engine_execute", lambda *args, **kwargs: execute),
            mock.patch.object(learn, "_read_before_testing", self.read),
            mock.patch.object(learn, "_prove_stages", self.prove),
            mock.patch.object(learn, "_search", search),
            mock.patch.object(learn, "_save_notes", mock.Mock()),
            mock.patch.object(learn, "adjustment_notes", lambda trials: ("", "")),
            mock.patch.object(learn, "_library_notice", mock.Mock()),
            mock.patch.object(learn, "_mark_stage", mock.Mock()),
            mock.patch.object(learn, "_try_skip_window", mock.Mock(return_value=None)),
            mock.patch.object(learn, "_review_failed_stage", self.review),
            mock.patch.object(learn, "_worth_email", lambda row: False),
            mock.patch.object(learn, "_send", self.send),
            mock.patch.object(learn, "ready_to_file", lambda notes, instruments=None: False),
            mock.patch.object(learn, "_tell_administrator", mock.Mock()),
            mock.patch("futuresfund.strategy_team.atr_exit", lambda text: ""),
            mock.patch("futuresfund.delta_core.is_core_file", lambda path: False),
            mock.patch("futuresfund.entry_rules.repair_dead_entry", lambda text: text),
            mock.patch("futuresfund.strategy_team.prepare_backtest", lambda text, root: text),
            mock.patch("futuresfund.backtrader_engine._bars_from", lambda bars: bars),
            mock.patch("futuresfund.backtrader_engine.finish_study", lambda slot: None),
            mock.patch("futuresfund.backtrader_engine.set_engine_notice", lambda slot, text: None),
            mock.patch("futuresfund.backtrader_engine.slot_for", lambda timeframe, root=None: 0),
            mock.patch("futuresfund.backtrader_engine.begin_study", lambda *args: None),
            mock.patch("futuresfund.entry_rules.ensure_entry_rule", lambda *args, **kwargs: True),
            mock.patch("futuresfund.pine_runtime.supported", lambda text: True),
            mock.patch("futuresfund.strategy_team.compile_error", lambda text: None),
            mock.patch("futuresfund.strategy_team.repair_for_pine", lambda text: (text, [])),
            mock.patch("futuresfund.objective.passes_acceptance", lambda row: False),
            mock.patch("futuresfund.mailer.write_result", mock.Mock(side_effect=AssertionError("no email in a test"))),
        ]

    def run(self):
        from futuresfund.board import Board

        board = Board()
        for patch in self.patches:
            patch.start()
        try:
            learn._study_chart(board, self.path, {}, None, developer="2min chart developer", timeframe="2m")
        finally:
            for patch in reversed(self.patches):
                patch.stop()
        return board


class StudyWiringTests(_Isolated):
    def test_the_study_reads_the_script_unless_the_switch_is_off(self):
        study = _Study(self)
        study.run()
        self.assertEqual(study.read.call_count, 1)
        study = _Study(self)
        with mock.patch.object(learn, "READ_BEFORE_TESTING", False):
            study.run()
        study.read.assert_not_called()
        study.prove.assert_called_once()

    def test_the_recent_window_check_is_set_for_the_year_search_and_cleared_after(self):
        study = _Study(self)
        study.run()
        self.assertIsNone(study.gate_during_year)
        study = _Study(self)
        with mock.patch.object(learn, "RECHECK_BEFORE_FILING", True), mock.patch.object(learn, "_stage_checks", lambda execute: "the check"):
            study.run()
        self.assertEqual(study.gate_during_year, "the check")
        self.assertIsNone(getattr(learn._stage_gate, "check", None))

    def test_the_check_is_cleared_when_the_year_search_fails(self):
        study = _Study(self)

        def broken(*args, **kwargs):
            study.gate_during_year = getattr(learn._stage_gate, "check", None)
            raise RuntimeError("engine went away")

        study.patches[[getattr(patch, "attribute", None) for patch in study.patches].index("_search")] = mock.patch.object(learn, "_search", broken)
        with mock.patch.object(learn, "RECHECK_BEFORE_FILING", True), mock.patch.object(learn, "_stage_checks", lambda execute: "the check"):
            with self.assertRaises(RuntimeError):
                study.run()
        self.assertEqual(study.gate_during_year, "the check")
        self.assertIsNone(getattr(learn._stage_gate, "check", None))

    def test_a_failed_year_is_reviewed_unless_the_switch_is_off(self):
        study = _Study(self)
        study.run()
        self.assertEqual([call.args[3] for call in study.review.call_args_list], [365])
        study = _Study(self)
        with mock.patch.object(learn, "STAGE_REVIEW_ON", False):
            study.run()
        study.review.assert_not_called()


class StageWiringTests(_Isolated):
    def setUp(self):
        super().setUp()
        _default_book(self)

    def _executor(self, results):
        windows = []

        def execute(params):
            windows.append(execute.source["sessions"])
            return results(execute.source["sessions"], params)

        execute.source = {"text": "", "stamp": 0, "sessions": None}
        return execute, windows

    def test_a_stage_that_is_off_is_skipped_without_a_ledger_mark(self):
        execute, windows = self._executor(lambda sessions, params: {"net_profit": 600, "max_drawdown": 100, "trades": 40, "days": sessions})
        marks = []
        with mock.patch.object(learn, "STAGES_OFF", frozenset({7})), \
                mock.patch.object(learn, "_mark_stage", lambda path, root, timeframe, days, status, row=None, source="", fresh=False: marks.append((days, status, fresh))), \
                mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: None):
            settings, stopped = learn._prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None, root="ES", timeframe="5m")
        self.assertIsNone(stopped)
        self.assertEqual(settings["length"], 10)
        self.assertNotIn(5, windows)
        self.assertIn(21, windows)
        self.assertNotIn(7, [days for days, _status, _fresh in marks])
        self.assertEqual(sorted(execute.source["stage_runs"]), [30, 90])
        self.assertIn((365, "testing", False), marks)
        # The first stage that runs clears the passes of an earlier study, because the 7-day mark does not run.
        self.assertIn((30, "testing", True), marks)
        self.assertIn((90, "testing", False), marks)

    def test_the_first_stage_that_runs_clears_old_passes_on_the_ledger(self):
        from futuresfund import proving

        with TemporaryDirectory() as tmp, mock.patch.object(proving, "_home", lambda: Path(tmp)):
            proving.mark("old.pine", "ES", "5m", 7, "passed", {"params": {}}, 'strategy("x")\n')
            proving.mark("old.pine", "ES", "5m", 30, "testing")
            self.assertIn("7", proving._load()["old.pine|ES|5m"]["passed"])
            proving.mark("old.pine", "ES", "5m", 30, "testing", fresh=True)
            self.assertEqual(proving._load()["old.pine|ES|5m"]["passed"], {})

    def test_with_every_stage_off_the_year_mark_clears_old_passes(self):
        execute, _windows = self._executor(lambda sessions, params: {"net_profit": 600, "max_drawdown": 100, "trades": 40, "days": sessions})
        marks = []
        with mock.patch.object(learn, "STAGES_OFF", frozenset({7, 30, 90})), \
                mock.patch.object(learn, "_mark_stage", lambda path, root, timeframe, days, status, row=None, source="", fresh=False: marks.append((days, status, fresh))):
            learn._prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None, root="ES", timeframe="5m")
        self.assertEqual(marks, [(365, "testing", True)])

    def test_with_every_stage_off_the_year_starts_from_the_scripts_settings(self):
        execute, windows = self._executor(lambda sessions, params: {"net_profit": 600, "max_drawdown": 100, "trades": 40, "days": sessions})
        with mock.patch.object(learn, "STAGES_OFF", frozenset({7, 30, 90})), mock.patch.object(learn, "_mark_stage", lambda *args, **kwargs: None):
            settings, stopped = learn._prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None)
        self.assertEqual((settings, stopped, windows), ({"length": 10}, None, []))

    def test_a_failed_stage_is_reviewed_unless_the_switch_is_off(self):
        losing = lambda sessions, params: {"net_profit": -200, "max_drawdown": 300, "trades": 5, "days": sessions}
        for switch, expected in ((True, 1), (False, 0)):
            execute, _windows = self._executor(losing)
            review = mock.Mock()
            with self.subTest(switch=switch), mock.patch.object(learn, "STAGE_REVIEW_ON", switch), \
                    mock.patch.object(learn, "_review_failed_stage", review), \
                    mock.patch.object(learn, "_mark_stage", lambda *args, **kwargs: None), \
                    mock.patch.object(learn, "STAGE_7_ATTEMPTS", 3), \
                    mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args: None):
                settings, stopped = learn._prove_stages({"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None)
            self.assertEqual(stopped.stage_failed, 7)
            self.assertEqual(review.call_count, expected)


class SearchWiringTests(_Isolated):
    def test_changing_the_year_limit_mid_search_keeps_the_loss_stop(self):
        calls = {"n": 0}
        limit = learn.ATTEMPT_LIMIT

        def execute(params):
            calls["n"] += 1
            if calls["n"] == 5:
                learn.ATTEMPT_LIMIT = limit + 50
            return {"net_profit": -100.0 - calls["n"], "max_drawdown": 80.0, "trades": 20, "days": 260}

        trials = learn._search({"length": 10}, execute, 50000, limit, None, None, None, extras=False, developer="15 min chart developer", year=True)
        self.assertEqual(learn.ATTEMPT_LIMIT, limit + 50)
        self.assertTrue(getattr(trials, "loss_stop", False))
        self.assertEqual(len(trials), learn.LOSS_LOOK)

    def test_the_year_keeps_its_loss_stop_whatever_its_limit(self):
        def execute(params):
            return {"net_profit": -100.0 - float(params.get("length", 0)), "max_drawdown": 80.0, "trades": 20, "days": 260}

        # The year search is marked by the caller, so a year gauge that differs from ATTEMPT_LIMIT keeps the stop.
        with mock.patch.object(learn, "LOSS_LOOK", 3):
            trials = learn._search({"length": 10}, execute, 50000, 7, None, None, None, extras=False, developer="15 min chart developer", year=True)
        self.assertTrue(getattr(trials, "loss_stop", False))
        self.assertEqual(len(trials), 3)

    def test_a_shorter_search_still_has_no_loss_stop(self):
        def execute(params):
            return {"net_profit": -100.0 - float(params.get("length", 0)), "max_drawdown": 80.0, "trades": 20, "days": 260}

        # A search not marked as the year has no loss stop, even when its limit equals the year gauge.
        with mock.patch.object(learn, "LOSS_LOOK", 3), mock.patch.object(learn, "ATTEMPT_LIMIT", 6):
            trials = learn._search({"length": 10}, execute, 50000, 6, None, None, None, extras=False, developer="15 min chart developer")
        self.assertFalse(getattr(trials, "loss_stop", False))

    def test_the_alert_bars_follow_the_switch(self):
        series = mock.Mock(return_value=[])
        with mock.patch("futuresfund.research_bars.research_series", series):
            learn._load_frames("NQ")
            self.assertTrue(all(call.kwargs == {"include_live": True} for call in series.call_args_list))
            self.assertEqual(sorted(call.args[1] for call in series.call_args_list), sorted(learn.RESEARCH_CHARTS))
            series.reset_mock()
            with mock.patch.object(learn, "USE_ALERT_BARS", False):
                learn._load_frames("NQ")
            self.assertTrue(all(call.kwargs == {"include_live": False} for call in series.call_args_list))
            # A chart engine studies one chart, so it builds only that chart's bars.
            series.reset_mock()
            self.assertEqual(list(learn._load_frames("NQ", timeframes=("5m",))), ["5m"])
            self.assertEqual([call.args[:2] for call in series.call_args_list], [("NQ", "5m")])

    def test_compile_repairs_use_their_own_count(self):
        from futuresfund import questions

        path = Path("broken.pine")
        ask = mock.Mock(return_value="awaiting")
        start = mock.Mock()
        with mock.patch.object(questions, "ask_owner", ask), mock.patch.object(learn, "start_compile_repair", start):
            learn._repair_tries[str(path)] = 2
            self.assertEqual(learn.send_to_repair(None, path, "dev", "5m", "", "Bad.", 0), "repairing")
            start.assert_called_once()
            ask.assert_not_called()
            learn._repair_tries[str(path)] = 3
            # After the repairs the researcher asks the owner: the script waits for the answer, it is not parked.
            self.assertEqual(learn.send_to_repair(None, path, "dev", "5m", "", "Bad.", 0), "awaiting")
            self.assertIn("3 repairs did not make it compile.", ask.call_args.args[3])
            self.assertEqual(ask.call_args.args[4], "compile")
            start.reset_mock()
            ask.reset_mock()
            with mock.patch.object(learn, "COMPILE_REPAIR_TRIES", 0), mock.patch.object(learn, "REPAIR_TRIES", 7):
                self.assertEqual(learn.send_to_repair(None, path, "dev", "5m", "", "Bad.", 0), "awaiting")
            start.assert_not_called()
            self.assertIn("Compile repairs are turned off", ask.call_args.args[3])
        # With questions by email off, the old park in cannotRunStrategies is kept.
        leave = mock.Mock(return_value="cannot_run")
        with mock.patch.object(questions, "EMAIL_QUESTIONS", False), mock.patch.object(learn, "leave_unrunnable", leave):
            learn._repair_tries[str(path)] = 3
            self.assertEqual(learn.send_to_repair(None, path, "dev", "5m", "", "Bad.", 0), "cannot_run")
            self.assertIn("3 repairs did not make it compile.", leave.call_args.args[3])
        learn._repair_tries.pop(str(path), None)


class DecideWiringTests(_Isolated):
    def _decide(self, kind, answer):
        from futuresfund.board import Board

        asked = []
        saved = {}
        reopened = mock.Mock(return_value=True)

        def reasoned(board, path, script, trials, bounds, choices, why, response, redesign):
            asked.append(why)
            return dict(answer)

        job = {
            "key": f"k|{kind}", "kind": kind, "review": "", "board": Board(), "path": Path("s.pine"),
            "script": PINE, "trials": [{"params": {"length": 10}, "net_profit": -5}], "bounds": {}, "choices": {},
            "why": "Repair it.", "response": None, "researcher": "Chart researcher",
        }
        with mock.patch.object(learn, "_reasoned_next", reasoned), \
                mock.patch("futuresfund.script_diagnosis.load", lambda script: {}), \
                mock.patch("futuresfund.script_diagnosis.save", lambda script, record: saved.update(record)), \
                mock.patch.object(learn, "_reopen_for_answer", reopened):
            learn._decide(job)
        return asked, saved, reopened

    def test_a_repair_answer_rewrites_only_while_allowed(self):
        answer = {"because": "Fix the stop.", "script": "//@version=5\nstrategy('fixed')\n", "params": {"length": 12}}
        asked, saved, _reopened = self._decide("repair", answer)
        self.assertEqual(saved["answer"]["script"], answer["script"])
        self.assertNotIn("Only a SETTING line or KEEP is used", asked[0])
        with mock.patch.object(learn, "ALLOW_REPAIR_REWRITES", False):
            asked, saved, _reopened = self._decide("repair", answer)
        self.assertIsNone(saved["answer"]["script"])
        self.assertEqual(saved["answer"]["params"], {"length": 12})
        self.assertIn("Only a SETTING line or KEEP is used", asked[0])

    def test_finished_charts_run_again_for_an_answer_unless_the_switch_is_off(self):
        answer = {"because": "Use 12.", "params": {"length": 12}}
        _asked, _saved, reopened = self._decide("read", answer)
        reopened.assert_called_once()
        with mock.patch.object(learn, "REOPEN_FOR_ANSWERS", False):
            _asked, saved, reopened = self._decide("read", answer)
        reopened.assert_not_called()
        # The answer still waits for the next chart that opens the script.
        self.assertEqual(saved["answer"]["params"], {"length": 12})

    def test_no_chart_runs_again_while_the_read_before_testing_is_off(self):
        # Only the read before testing uses a waiting answer, so reopening would rerun every chart for nothing.
        answer = {"because": "Use 12.", "params": {"length": 12}}
        with mock.patch.object(learn, "READ_BEFORE_TESTING", False):
            _asked, saved, reopened = self._decide("read", answer)
        reopened.assert_not_called()
        self.assertEqual(saved["answer"]["params"], {"length": 12})


class ApiTests(_Isolated):
    @classmethod
    def setUpClass(cls):
        import futuresfund.server as server

        cls.server = server

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        patcher = mock.patch.object(self.server.board, "on_error", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.notes = Path(folder.name)
        patcher = mock.patch.object(learn, "NOTES", self.notes)
        patcher.start()
        self.addCleanup(patcher.stop)
        learn._QUEUE_FIRST.clear()
        self.addCleanup(learn._QUEUE_FIRST.clear)
        _default_book(self)
        self.client = TestClient(self.server.app)

    def test_get_save_and_reset_the_knobs(self):
        found = self.client.get("/api/workflow-design")
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        for name in ("knobs", "queue_first", "learning_files", "layout", "updated", "live"):
            self.assertIn(name, body)
        for name in ("folders", "waiting", "shelf", "ledger", "testing"):
            self.assertIn(name, body["live"])
        saved = self.client.post("/api/workflow-design/knobs", json={"knobs": {"stage_7_attempts": 25, "stage_review": False}})
        self.assertEqual(saved.status_code, 200, saved.text)
        rows = {row["key"]: row for row in saved.json()["knobs"]}
        self.assertEqual(rows["stage_7_attempts"]["value"], 25)
        self.assertTrue(rows["stage_review"]["changed"])
        self.assertEqual(learn.STAGE_7_ATTEMPTS, 25)
        self.assertIs(learn.STAGE_REVIEW_ON, False)
        refused = self.client.post("/api/workflow-design/knobs", json={"knobs": {"stage_7_attempts": 1001}})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("7-day stage attempts", refused.json()["detail"])
        self.assertEqual(self.client.post("/api/workflow-design/knobs", json={"knobs": {"nope": 1}}).status_code, 400)
        reset = self.client.post("/api/workflow-design/reset", json={"keys": []})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertFalse(any(row["changed"] for row in reset.json()["knobs"]))
        self.assertEqual(learn.STAGE_7_ATTEMPTS, 100)
        self.assertIs(learn.STAGE_REVIEW_ON, True)
        self.assertEqual(self.client.post("/api/workflow-design/reset", json={"keys": ["nope"]}).status_code, 400)

    def test_layout_queue_and_changes(self):
        layout = self.client.post("/api/workflow-design/layout", json={"layout": {"stage7": [90, 720]}})
        self.assertEqual(layout.json(), {"layout": {"stage7": [90, 720]}})
        self.assertEqual(self.client.get("/api/workflow-design").json()["layout"], {"stage7": [90, 720]})
        self.assertEqual(self.client.post("/api/workflow-design/layout", json={"layout": {"stage7": [1]}}).status_code, 400)
        self.assertEqual(self.client.post("/api/workflow-design/layout", json={"layout": {}}).json(), {"layout": {}})
        queued = self.client.post("/api/workflow-design/queue", json={"names": ["b.pine", "a.pine"]})
        self.assertEqual(queued.json(), {"queue_first": ["b.pine", "a.pine"]})
        self.assertEqual(json.loads((self.notes / "queue_first.json").read_text(encoding="utf-8")), ["b.pine", "a.pine"])
        self.assertEqual(self.client.get("/api/workflow-design").json()["queue_first"], ["b.pine", "a.pine"])
        self.assertEqual(self.client.post("/api/workflow-design/queue", json={"names": ["../x.pine"]}).status_code, 400)
        (self.notes / "alpha.json").write_text(json.dumps({"file": "alpha.pine", "instruments": {"ES": {"charts": {"5m": {"trials": [_trial(7, 10.0, 3), _trial(7, 30.0, 4)]}}}}}), encoding="utf-8")
        changes = self.client.get("/api/workflow-design/changes", params={"limit": 80})
        self.assertEqual(changes.status_code, 200, changes.text)
        self.assertEqual(changes.json()["summary"]["total"], 2)
        self.assertEqual(changes.json()["rows"][0]["attempt"], 2)


if __name__ == "__main__":
    unittest.main()
