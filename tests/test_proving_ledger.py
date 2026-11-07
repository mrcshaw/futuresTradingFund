import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = """//@version=6
strategy("Ledger test")
length = input.int(10, "Length")
"""


class ProvingLedgerTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.patch = mock.patch("futuresfund.learn.NOTES", Path(self.home.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.home.cleanup()

    def test_pass_saves_the_script_with_the_passing_inputs(self):
        from futuresfund.proving import mark, passed_script, rows

        mark("ledger.pine", "ES", "5m", 7, "testing")
        mark("ledger.pine", "ES", "5m", 7, "passed", {"net_profit": 410, "trades": 6, "params": {"length": 14}}, SCRIPT)
        mark("ledger.pine", "ES", "5m", 30, "testing")
        [row] = rows()
        self.assertEqual(row["label"], "Testing 30 days")
        self.assertEqual(row["passed"]["7"]["net_profit"], 410)
        script = passed_script(row["id"], 7)
        self.assertIn('input.int(14, "Length")', script["pine"])
        self.assertIsNone(passed_script(row["id"], 30))

    def test_failed_stage_and_a_new_study_clear_the_old_passes(self):
        from futuresfund.proving import mark, rows

        mark("ledger.pine", "NQ", "15m", 7, "passed", {"params": {}}, SCRIPT)
        mark("ledger.pine", "NQ", "15m", 30, "failed")
        self.assertEqual(rows()[0]["label"], "Failed 30 days")
        mark("ledger.pine", "NQ", "15m", 7, "testing")
        self.assertEqual(rows()[0]["passed"], {})

    def test_only_the_newest_rows_stay_with_their_saved_scripts(self):
        from futuresfund import proving

        with mock.patch.object(proving, "LIMIT", 2), mock.patch.object(proving, "_now") as now:
            now.return_value = "2026-10-30 01:00:00"
            proving.mark("running.pine", "ES", "5m", 30, "testing")
            proving.mark("old.pine", "ES", "5m", 7, "passed", {"params": {}}, SCRIPT)
            saved = Path(self.home.name) / "proving" / "7_day" / "old_ES_5m.pine"
            self.assertTrue(saved.is_file())
            now.return_value = "2026-10-30 02:00:00"
            proving.mark("newer.pine", "ES", "5m", 7, "failed")
            now.return_value = "2026-10-30 03:00:00"
            proving.mark("newest.pine", "ES", "5m", 7, "failed")
        self.assertEqual([row["file"] for row in proving.rows()], ["newest.pine", "running.pine"])
        self.assertFalse(saved.exists())

    def test_the_checks_of_one_year_settings_are_written_to_the_ledger(self):
        from futuresfund.learn import _mark_checks, _stage_checks
        from futuresfund.proving import mark, rows

        def execute(params):
            sessions = execute.source["sessions"]
            profit = 900 if sessions in (5, 21) else -100
            # Two trades a session: the library count on every window, and inside 600 a year at that pace.
            return {"net_profit": profit, "max_drawdown": 100, "trades": 2 * sessions, "win_rate": 60, "profit_factor": 2, "biggest_loser": -100}

        execute.source = {"text": SCRIPT, "stamp": 0, "sessions": None}
        path = Path(self.home.name) / "ledger.pine"
        mark(path.name, "ES", "5m", 7, "testing")
        mark(path.name, "ES", "5m", 365, "testing")
        failed = _mark_checks(path, "ES", "5m", _stage_checks(execute), {"params": {"length": 14}}, SCRIPT)
        self.assertEqual(failed, 90)
        [row] = rows()
        self.assertEqual(row["label"], "Failed 90 days")
        self.assertEqual(sorted(row["passed"]), ["30", "7"])

    def test_a_stopped_stage_keeps_its_passes_and_says_why(self):
        from futuresfund.proving import mark, rows, stop_testing

        mark("ledger.pine", "MNQ", "2m", 7, "passed", {"params": {}}, SCRIPT)
        mark("ledger.pine", "MNQ", "2m", 365, "testing")
        mark("other.pine", "MNQ", "5m", 30, "testing")
        stop_testing("The engine stopped.", file="ledger.pine", timeframe="2m")
        stopped, other = sorted(rows(), key=lambda row: row["file"])
        self.assertEqual(stopped["label"], "Stopped at 365 days")
        self.assertEqual(stopped["reason"], "The engine stopped.")
        self.assertIn("7", stopped["passed"])
        self.assertEqual(other["label"], "Testing 30 days")
        mark("ledger.pine", "MNQ", "2m", 7, "testing")
        self.assertNotIn("reason", next(row for row in rows() if row["file"] == "ledger.pine"))

    def test_research_starting_stops_every_stage_left_testing(self):
        from futuresfund.proving import mark, rows, stop_testing

        mark("a.pine", "ES", "5m", 365, "testing")
        mark("b.pine", "YM", "2m", 30, "testing")
        mark("c.pine", "ES", "15m", 7, "failed")
        self.assertEqual(len(stop_testing("Research restarted.")), 2)
        self.assertEqual(sorted(row["label"] for row in rows()), ["Failed 7 days", "Stopped at 30 days", "Stopped at 365 days"])

    def test_a_study_that_ends_without_a_result_marks_its_stage_stopped(self):
        from futuresfund import learn
        from futuresfund.proving import mark, rows

        path = Path(self.home.name) / "ledger.pine"

        def engine_stops(board, path, *args):
            mark(path.name, "YM", "2m", 365, "testing")
            learn._tell_administrator(None, 0, path, "PineTS did not finish within 10 minutes.")
            return "engine"

        def finishes(board, path, *args):
            mark(path.name, "YM", "5m", 365, "testing")
            mark(path.name, "YM", "5m", 365, "failed")

        with mock.patch("futuresfund.learn._study_chart", side_effect=engine_stops), mock.patch("futuresfund.backtrader_engine.set_engine_notice"):
            learn._study(None, path, {}, None, timeframe="2m")
        with mock.patch("futuresfund.learn._study_chart", side_effect=finishes):
            learn._study(None, path, {}, None, timeframe="5m")
        two, five = sorted(rows(), key=lambda row: row["timeframe"])
        # An engine fault is not the script's result, so that stage resumes there on its next study.
        self.assertEqual(two["label"], "Stopped at 365 days, resumes there")
        self.assertTrue(two["resume"])
        self.assertEqual(two["reason"], "The engine stopped. PineTS did not finish within 10 minutes.")
        self.assertEqual(five["label"], "Failed 365 days")


if __name__ == "__main__":
    unittest.main()
