"""The indicator library the desks build strategies from."""

import tempfile
import unittest
from pathlib import Path

import futuresfund.indicators as indicators

SCRIPT = '''//@version=5
strategy("Sample", overlay=true, process_orders_on_close=true)

// ===== INPUTS =====
length = input.int(20, "Length")

// ===== VOLUME PROFILE =====
// Where the most volume traded over the lookback.
float top = ta.highest(high, length)
float bottom = ta.lowest(low, length)
float mid = (top + bottom) / 2

score(x) =>
    x > ta.ema(close, length) ? 1 : 0

// ===== ENTRIES =====
if close > mid and score(close) == 1
    strategy.entry("Long", strategy.long)
    strategy.exit("XL", "Long", limit=close + 2, stop=close - 2)
inSession = not na(time(timeframe.period, "1800-1645:12345", "America/New_York"))
if not inSession
    strategy.close_all(comment="Session")
'''


class IndicatorLibraryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.saved = indicators.LIBRARY_PATH
        indicators.LIBRARY_PATH = Path(self.folder.name) / "indicator_library.json"

    def tearDown(self):
        indicators.LIBRARY_PATH = self.saved
        self.folder.cleanup()

    def test_a_script_gives_its_built_ins_sections_and_functions(self):
        found = {item["id"]: item for item in indicators.scan_script("sample.pine", SCRIPT)}
        self.assertIn("ta.highest", found)
        self.assertIn("ta.ema", found)
        self.assertIn("section:volume_profile", found)
        self.assertIn("ta.highest(high, length)", found["section:volume_profile"]["source"])
        self.assertIn("Where the most volume traded", found["section:volume_profile"]["summary"])
        self.assertIn("function:score", found)
        self.assertNotIn("section:inputs", found)
        self.assertNotIn("section:entries", found)
        self.assertTrue(found["section:volume_profile"]["engine"])

    def test_a_new_indicator_is_added_once(self):
        first = indicators.record_script("sample.pine", SCRIPT, "2min chart developer")
        self.assertTrue(any(item["id"] == "section:volume_profile" for item in first))
        again = indicators.record_script("sample.pine", SCRIPT, "2min chart developer")
        self.assertEqual(again, [])
        other = indicators.record_script("other.pine", SCRIPT, "5 min chart developer")
        self.assertEqual(other, [])
        row = indicators.indicator("section:volume_profile")
        self.assertEqual(row["scripts"], ["sample.pine", "other.pine"])
        self.assertEqual(row["added_by"], "2min chart developer")

    def test_the_library_starts_from_every_script_on_file(self):
        held = indicators.seed()
        names = {row["id"] for row in indicators.all_indicators()}
        self.assertGreater(held, 40)
        self.assertIn("ta.ema", names)
        self.assertIn("section:bos_detection", names)
        self.assertIn("Indicator library", indicators.agent_catalog())
        self.assertIn("ta.atr(length)", indicators.creation_context())

    def test_the_creation_team_reads_the_library_and_the_checker_needs_the_engine(self):
        from futuresfund.strategy_team import pine_problems, prompt_for

        indicators.record_script("sample.pine", SCRIPT)
        self.assertIn("Indicator library", prompt_for([]))
        self.assertEqual(pine_problems(SCRIPT), [])
        broken = SCRIPT.replace('length = input.int(20, "Length")', 'length = input.int(20, "Length")\nstrategy.option = strategy.option.none')
        self.assertTrue(any("The engine cannot run it" in problem for problem in pine_problems(broken)))


if __name__ == "__main__":
    unittest.main()
