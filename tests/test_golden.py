"""The golden strategy sets the idea, and its results are the bar the creators aim for on every instrument."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import golden

SCRIPT = '//@version=5\nstrategy("Gold standard", overlay=true)\nx = close\nplot(x)\n'
EXPORT = (
    "Trade number,Type,Date and time,Signal,Price USD,Size (qty),Size (value),Net PnL USD,Return %,Commission USD,"
    "Favorable excursion USD,Favorable excursion %,Adverse excursion USD,Adverse excursion %,Cumulative PnL USD,Cumulative PnL %,Duration (bars)\n"
    "1,Exit long,2026-05-11 11:30,XL,29952,1,0,2104,0,11,0,0,0,0,2104,0,1\n"
    "1,Entry long,2026-05-11 11:25,Long,29846.25,1,0,2104,0,11,0,0,0,0,2104,0,1\n"
)
NAME = "Gold_standard_CME_MINI_NQZ2026_2026-10-30_abc.csv"
IDEA = "Break out of the point of control with the delta behind the move."


class GoldenTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.notes = mock.patch("futuresfund.learn.NOTES", Path(self.home.name))
        self.notes.start()
        self.report = {"expected": 1, "covered": 100, "matched": 100, "extra": 0, "covered_profit": 2104.0, "engine_covered_profit": 2104.0}
        self.check = mock.patch("futuresfund.tv_checks.check_export", lambda path, source=None, title="": self.report)
        self.check.start()

    def tearDown(self):
        self.check.stop()
        self.notes.stop()
        self.home.cleanup()

    def test_the_idea_is_the_standard_and_the_results_are_the_bar(self):
        row = golden.enter(SCRIPT, NAME, EXPORT, IDEA, wait=True)
        self.assertEqual(golden.active()["id"], row["id"])
        self.assertEqual(row["contract"], "NQZ6")
        self.assertEqual(row["title"], "Gold standard")
        self.assertIn(IDEA, golden.standard_line())
        self.assertIn("every instrument", golden.standard_line())
        self.assertNotIn("2,104", golden.standard_line())
        self.assertIn("made $2,104", golden.results_line())
        self.assertIn("on NQ 5m", golden.results_line())
        self.assertIn("made $2,104", golden.creation_brief())

    def test_the_script_reaches_the_team_with_the_idea(self):
        self.report.update({"covered": 124, "matched": 0})
        golden.enter(SCRIPT, NAME, EXPORT, IDEA, wait=True)
        self.assertFalse(golden.confirmed(golden.active()))
        brief = golden.creation_brief()
        self.assertIn(IDEA, brief)
        self.assertIn('strategy("Gold standard"', brief)
        self.assertIn("Do not copy it", brief)

    def test_a_strategy_needs_its_idea(self):
        with self.assertRaisesRegex(ValueError, "idea"):
            golden.enter(SCRIPT, NAME, EXPORT, "  ")

    def test_too_few_trades_do_not_confirm(self):
        self.report.update({"covered": 3, "matched": 3})
        golden.enter(SCRIPT, NAME, EXPORT, IDEA, wait=True)
        self.assertFalse(golden.confirmed(golden.active()))

    def test_a_script_tradingview_refuses_is_not_entered(self):
        with self.assertRaisesRegex(ValueError, "refuse"):
            golden.enter(SCRIPT + "if x > 1\n    plot(x)\n", NAME, EXPORT, IDEA)

    def test_a_script_needs_its_export(self):
        with self.assertRaisesRegex(ValueError, "CSV"):
            golden.enter(SCRIPT, "", "", IDEA)

    def test_the_standard_can_be_changed_and_removed(self):
        first = golden.enter(SCRIPT, NAME, EXPORT, IDEA, wait=True)
        second = golden.enter(SCRIPT + "// second\n", NAME, EXPORT, IDEA, wait=True)
        self.assertEqual(golden.active()["id"], second["id"])
        self.assertTrue(golden.make_active(first["id"]))
        self.assertEqual(golden.active()["id"], first["id"])
        self.assertTrue(golden.remove(first["id"]))
        self.assertEqual(golden.active()["id"], second["id"])

    def test_no_golden_strategy_adds_nothing_to_a_brief(self):
        self.assertEqual(golden.standard_line(), "")
        self.assertEqual(golden.creation_brief(), "")


@unittest.skipUnless(shutil.which("node"), "PineTS needs Node")
class ExportComparisonTests(unittest.TestCase):
    def test_a_script_is_compared_as_written(self):
        from futuresfund import tv_checks

        calls = []

        def fake_trades(source, bars, inputs, root, timeframe, as_written=False):
            calls.append((source, inputs, as_written))
            return []

        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp) / NAME
            export.write_text(EXPORT, encoding="utf-8")
            with mock.patch("futuresfund.backtrader_engine.script_trades", fake_trades), \
                    mock.patch("futuresfund.research_bars.contract_series", lambda symbol, timeframe: [{"t": 1788307200, "o": 1, "h": 2, "l": 0, "c": 1, "v": 1}]):
                report = tv_checks.check_export(export, source=SCRIPT, title="Gold standard")
        self.assertEqual(calls, [(SCRIPT, None, True)])
        self.assertEqual(report["inputs"], "the script as written")


if __name__ == "__main__":
    unittest.main()
