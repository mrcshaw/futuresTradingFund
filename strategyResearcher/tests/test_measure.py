import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from research_department.charts import ET, session_day
from research_department.measure import measure, summarize
from research_department.stages import Day, day_succeeded


def _trade(pnl, minute):
    when = datetime(2026, 10, 1, 12, minute, tzinfo=ET)
    return {"pnl": pnl, "exit_time": when.strftime("%Y-%m-%d %H:%M")}


class SessionLogicTests(unittest.TestCase):
    def setUp(self):
        when = datetime(2026, 10, 1, 12, 0, tzinfo=ET)
        self.day = session_day(int(when.timestamp()))

    def test_a_session_stops_once_it_is_up_500(self):
        day = measure([_trade(300, 0), _trade(200, 1), _trade(80, 2)], [self.day])[0]
        self.assertEqual(day.trades, 2)
        self.assertEqual(day.wins, 2)
        self.assertEqual(day.profit, 500)
        self.assertTrue(day_succeeded(day))
        self.assertEqual(summarize([day, Day(200, 0, 1, 1)]).get("profit"), 700)

    def test_a_session_stops_after_two_losing_trades(self):
        day = measure([_trade(-40, 0), _trade(-25, 1), _trade(600, 2)], [self.day])[0]
        self.assertEqual(day.trades, 2)
        self.assertEqual(day.losses, 2)
        self.assertEqual(day.wins, 0)
        self.assertEqual(day.profit, -65)
        self.assertFalse(day_succeeded(day))

    def test_a_session_with_no_trade_does_not_count(self):
        day = measure([], [self.day])[0]
        self.assertEqual(day.trades, 0)
        self.assertFalse(day_succeeded(day))
        self.assertFalse(day_succeeded(Day(500, 100, 1, 3, 3)))
