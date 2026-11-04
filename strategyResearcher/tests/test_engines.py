import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from research_department.engines import EngineList


class EngineTests(unittest.TestCase):
    def test_the_original_three_are_present(self):
        desk = EngineList()
        cards = desk.cards()
        self.assertEqual([card["timeframe"] for card in cards], ["2m", "5m", "1m"])
        self.assertEqual([card["id"] for card in cards], [1, 2, 3])

    def test_an_added_engine_returns_an_id_and_a_card(self):
        desk = EngineList()
        added = desk.add("5m")
        self.assertEqual(added.id, 4)
        self.assertEqual(desk.cards()[-1]["id"], 4)
        self.assertEqual(desk.cards()[-1]["timeframe"], "5m")

    def test_removing_an_added_engine_drops_its_card(self):
        desk = EngineList()
        added = desk.add("1m")
        self.assertEqual(desk.remove(added.id), "removed")
        self.assertEqual([card["id"] for card in desk.cards()], [1, 2, 3])

    def test_removing_an_original_engine_is_refused(self):
        desk = EngineList()
        self.assertEqual(desk.remove(2), "refused")
        self.assertEqual([card["id"] for card in desk.cards()], [1, 2, 3])

    def test_an_unfinished_script_is_set_aside_and_the_engine_is_free(self):
        desk = EngineList()
        desk.start(2, "slow.pine")
        desk.did_not_finish(2)
        card = desk.cards()[1]
        self.assertEqual(card["script"], "")
        self.assertTrue(card["free"])
        self.assertIsNone(card["result"])
        self.assertEqual(desk.aside, ["slow.pine"])
        desk.start(2, "next.pine")
        self.assertEqual(desk.cards()[1]["script"], "next.pine")
        self.assertFalse(desk.cards()[1]["free"])

    def test_one_minute_bars_stay_one_minute(self):
        from research_department.charts import Bar, _minutes, _resample

        self.assertEqual(_minutes("1m"), 1)
        bars = [Bar(1_633_305_600 + (index * 60), 1, 2, 0.5, 1.5, 10) for index in range(3)]
        built = _resample(bars, 1)
        self.assertEqual([bar.epoch for bar in built], [bar.epoch for bar in bars])
        self.assertEqual([bar.c for bar in built], [bar.c for bar in bars])
