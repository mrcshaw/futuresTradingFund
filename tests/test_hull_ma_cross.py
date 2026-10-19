"""Hull 16/65 crosses on the closed 4-hour bars, with the daily regression and the 16:45 flatten."""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from futuresfund.backtrader_engine import Bar
from futuresfund.pine_runtime import run_pine

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "futuresfund" / "learningStrategies" / "hull_ma_cross.pine").read_text(encoding="utf-8")
ET = ZoneInfo("America/New_York")
INPUTS = {"fastLen": 4, "slowLen": 8, "rsiLen": 3, "rsiLevel": 50.0, "regLen": 3, "maxLoss": 1800.0}


def bars(prices_at) -> list:
    start = datetime(2026, 10, 4, 18, 0, tzinfo=ET)
    rows = []
    for step in range(96 * 5):
        when = start + timedelta(minutes=15 * step)
        price = float(prices_at(when))
        rows.append(Bar(int(when.timestamp()), price, price + 0.5, price - 0.5, price, 10))
    return rows


def price(when: datetime) -> float:
    """Flat for three sessions, then a rise and a fall on Thursday."""
    start = datetime(2026, 10, 4, 18, 0, tzinfo=ET)
    hours = (when - start).total_seconds() / 3600.0
    if hours < 72:
        return 100.0
    if hours < 90:
        return 100.0 + (hours - 72) * 2.0
    return 136.0 - (hours - 90) * 3.0


class HullCrossTests(unittest.TestCase):
    def test_a_four_hour_hull_cross_enters_long_and_the_cross_back_exits(self):
        trades = run_pine(SOURCE, bars(price), INPUTS, root="NQ", timeframe="15m")
        self.assertGreaterEqual(len(trades), 1)
        self.assertGreater(trades[0]["entry_price"], 100)
        self.assertIn(trades[0]["exit_signal"], ("Hull", "Session", "Stop"))

    def test_the_halt_does_not_open_the_hull_cross(self):
        start = datetime(2026, 10, 7, 16, 50, tzinfo=ET)
        rows = []
        for step in range(8):
            price = 100.0 + step
            when = start + timedelta(minutes=step)
            rows.append(Bar(int(when.timestamp()), price, price + 0.5, price - 0.5, price, 10))
        trades = run_pine(SOURCE, rows, INPUTS, root="NQ", timeframe="15m")
        self.assertEqual(trades, [])
