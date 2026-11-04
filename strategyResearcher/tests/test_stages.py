import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from harness import days
from research_department.stages import (
    STAGE_DAYS,
    Day,
    StageBook,
    day_succeeded,
    resolve_settings,
    reward_note,
    reward_reading,
    window_rate,
)


class StageTests(unittest.TestCase):
    def test_empty_settings_use_the_defaults(self):
        rate, attempts = resolve_settings(None, None)
        self.assertEqual(rate, 0.60)
        self.assertEqual(attempts, 20)

    def test_the_same_day_list_is_sliced_for_each_stage(self):
        year = days(7, profit=500, worst_loss=200) + days(358, profit=0, worst_loss=400)
        self.assertEqual(window_rate(year, 7), 1.0)
        self.assertLess(window_rate(year, 30), 0.60)
        self.assertEqual(tuple(STAGE_DAYS), (7, 30, 90, 365))

    def test_a_pass_stores_the_row_and_starts_the_next_stage_at_attempt_1(self):
        book = StageBook()
        outcome = book.submit(
            days(365, profit=600, worst_loss=250, wins=3, trades=4),
            script="five.pine",
            instrument="MNQ",
            timeframe="5m",
            change="watch_bars 6",
            justification="Steadier days and a higher win percentage.",
        )
        self.assertEqual(outcome, "passed")
        row = book.rows[7][0]
        self.assertEqual(row.script, "five.pine")
        self.assertEqual(row.instrument, "MNQ")
        self.assertEqual(row.timeframe, "5m")
        self.assertEqual(row.change, "watch_bars 6")
        self.assertEqual(row.justification, "Steadier days and a higher win percentage.")
        self.assertEqual(row.days, 7)
        self.assertEqual(row.success_rate, 1.0)
        self.assertEqual(row.win_percentage, 0.75)
        self.assertEqual(row.worst_day, 600)
        self.assertEqual(row.trade_count, 28)
        self.assertEqual(row.profit, 4200)
        self.assertEqual(row.attempt, 1)
        self.assertEqual(book.days, 30)
        self.assertEqual(book.attempt, 1)

    def test_each_stage_uses_the_same_success_rate(self):
        book = StageBook(success_rate=0.60)
        year = days(7, profit=500, worst_loss=100, wins=1, trades=1) + days(358, profit=0, worst_loss=500)
        self.assertEqual(book.submit(year, script="a", instrument="ES", timeframe="2m", change="k 0.8", justification="both"), "passed")
        self.assertEqual(book.days, 30)
        failed = book.submit(year, script="a", instrument="ES", timeframe="2m", change="k 0.9", justification="both")
        self.assertEqual(failed, "failed")
        self.assertEqual(book.rows[30], [])
        self.assertEqual(book.attempt, 2)

    def test_attempt_20_stops_and_keeps_the_stages_already_passed(self):
        book = StageBook(attempts=20)
        good = days(365, wins=2, trades=2)
        self.assertEqual(book.submit(good, script="a", instrument="ES", timeframe="15m", change="one", justification="both"), "passed")
        bad = days(365, profit=0, worst_loss=500)
        outcome = ""
        for _ in range(20):
            outcome = book.submit(bad, script="a", instrument="ES", timeframe="15m", change="two", justification="both")
        self.assertEqual(outcome, "stopped")
        self.assertTrue(book.stopped)
        self.assertEqual(len(book.rows[7]), 1)
        self.assertEqual(book.rows[30], [])

    def test_a_day_inside_the_loss_and_on_the_target_counts(self):
        self.assertTrue(day_succeeded(Day(500, 400, 1, 1)))
        self.assertFalse(day_succeeded(Day(499, 0, 1, 1)))
        self.assertFalse(day_succeeded(Day(500, 401, 1, 1)))
        self.assertFalse(day_succeeded(Day(500, 100, 0, 0)))
        self.assertFalse(day_succeeded(Day(500, 100, 1, 4, 3)))
        book = StageBook(success_rate=0.40, attempts=5)
        six = [Day(600, 40, 1, 2)] * 3 + [Day(0, 0, 0, 0)] * 4
        self.assertEqual(
            book.submit(six, script="a", instrument="ES", timeframe="5m", change="x", justification="both"),
            "passed",
        )
        self.assertEqual(book.days, 30)
        self.assertEqual(book.rows[7][0].trade_count, 6)
        thin = StageBook(success_rate=0.40, attempts=5)
        one = [Day(500, 10, 1, 1)] + [Day(0, 0, 0, 0)] * 6
        self.assertEqual(
            thin.submit(one, script="a", instrument="ES", timeframe="5m", change="x", justification="both"),
            "failed",
        )
        self.assertEqual(thin.rows[7], [])
        self.assertEqual(thin.days, 7)
        two = StageBook(success_rate=0.40, attempts=5)
        pair = [Day(500, 10, 1, 1), Day(0, 10, 0, 1)] + [Day(0, 0, 0, 0)] * 5
        self.assertEqual(
            two.submit(pair, script="a", instrument="ES", timeframe="5m", change="x", justification="both"),
            "failed",
        )
        self.assertEqual(two.days, 7)
        book = StageBook()
        made = [Day(500, 400, 1, 1)] * 7 + days(358)
        self.assertEqual(book.submit(made, script="a", instrument="ES", timeframe="5m", change="x", justification="both"), "passed")

    def test_the_saved_success_rate_is_the_win_percentage(self):
        book = StageBook(success_rate=0.40)
        low_wins = [Day(500, 100, 1, 4)] * 7
        self.assertEqual(
            book.submit(low_wins, script="a", instrument="ES", timeframe="2m", change="k 1", justification="both"),
            "failed",
        )
        self.assertEqual(book.rows[7], [])
        flat = [Day(0, 100, 2, 4)] * 7
        self.assertEqual(
            book.submit(flat, script="a", instrument="ES", timeframe="2m", change="k 2", justification="both"),
            "failed",
        )
        self.assertEqual(book.rows[7], [])
        short = [Day(249, 100, 2, 4)] * 7
        self.assertEqual(
            book.submit(short, script="a", instrument="ES", timeframe="2m", change="k 2", justification="both"),
            "failed",
        )
        self.assertEqual(book.days, 7)
        high_wins = [Day(250, 100, 2, 4)] * 7
        self.assertEqual(
            book.submit(high_wins, script="a", instrument="ES", timeframe="2m", change="k 2", justification="both"),
            "passed",
        )
        self.assertEqual(book.rows[7][0].win_percentage, 0.5)
        self.assertEqual(book.rows[7][0].profit, 1750)
        self.assertEqual(book.rows[7][0].profit_goal, 1750)
        self.assertEqual(book.rows[7][0].success_rate, 0.0)

    def test_the_reward_scale_moves_toward_the_win_rate_goal(self):
        first = reward_reading(None, 0.25, 0.40, 100)
        self.assertEqual(first["direction"], "waiting")
        self.assertFalse(first["at_goal"])
        toward = reward_reading(0.25, 0.33, 0.40, 80)
        self.assertEqual(toward["direction"], "toward")
        self.assertFalse(toward["at_goal"])
        away = reward_reading(0.33, 0.18, 0.40, -40)
        self.assertEqual(away["direction"], "away")
        self.assertFalse(away["profitable"])
        arrived = reward_reading(0.33, 0.46, 0.40, 220)
        self.assertEqual(arrived["direction"], "toward")
        self.assertTrue(arrived["at_goal"])
        self.assertTrue(arrived["profitable"])
        note = reward_note(away)
        self.assertIn("moved away from the goal", note)
        self.assertIn("profit goal", note)
        self.assertIn("moved toward the goal", reward_note(toward))
        higher = reward_reading(0.2, 0.3, 0.5, 400, 200)
        self.assertEqual(higher["profit_goal"], 1750)
        self.assertEqual(higher["profit_total"], 3500)
        self.assertEqual(higher["profit_direction"], "toward")
        self.assertFalse(higher["profit_at_goal"])
        month = reward_reading(0.2, 0.3, 0.5, 8000, 400, days=30)
        self.assertEqual(month["profit_total"], 15000)
        self.assertEqual(month["profit_goal"], 7500)
        self.assertTrue(month["profit_at_goal"])
        lower = reward_reading(0.3, 0.2, 0.5, 200, 1800)
        self.assertEqual(lower["profit_direction"], "away")
        arrived = reward_reading(0.2, 0.4, 0.5, 3500, 200)
        self.assertTrue(arrived["profit_at_goal"])
        self.assertEqual(arrived["profit_direction"], "toward")
        self.assertIn("starts at $0", reward_note(higher))
        self.assertIn("50% of that total", reward_note(higher))
        self.assertIn("$1,750", reward_note(higher))
        self.assertIn("$3,500", reward_note(higher))
        losing = StageBook(success_rate=0.40)
        down = [Day(-20, 100, 3, 4)] * 7
        self.assertEqual(
            losing.submit(down, script="a", instrument="ES", timeframe="2m", change="k 3", justification="both"),
            "failed",
        )
        self.assertEqual(losing.days, 7)
