"""The order-block strategy takes a return into the last opposite candle, long and short."""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from futuresfund.backtrader_engine import Bar
from futuresfund.pine_runtime import run_pine

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "futuresfund" / "learningStrategies" / "smc_order_block.pine").read_text(encoding="utf-8")
ET = ZoneInfo("America/New_York")
INPUTS = {"swing_left": 2, "swing_right": 2, "reward_risk": 1.0, "stop_pad": 1.0, "cooldown_bars": 0}

LONG_ROWS = [
    (100, 101, 99, 100.5),
    (100.5, 103, 100, 102),
    (102, 106, 101.5, 105),
    (105, 110, 104.5, 109),
    (109, 114, 108, 113),
    (113, 120, 112, 119),
    (119, 118, 114, 115),
    (115, 116, 110, 111),
    (111, 113, 109, 112),
    (112, 114, 110, 113),
    (113, 121, 112.5, 120.5),
    (120.5, 120.5, 115.5, 117),
    (117, 119, 116, 118),
    (118, 122, 117, 121),
    (121, 126, 120, 125),
    (125, 126, 124, 125),
]
SHORT_ROWS = [
    (120, 121, 119, 119.5),
    (119.5, 120, 117, 118),
    (118, 118.5, 114, 115),
    (115, 115.5, 110, 111),
    (111, 112, 106, 107),
    (107, 108, 100, 101),
    (101, 106, 102, 105),
    (105, 110, 104, 109),
    (109, 111, 107, 108),
    (108, 110, 106, 107),
    (107, 108, 99, 99.5),
    (99.5, 105, 99, 103),
    (103, 104, 100, 101),
    (101, 102, 97, 98),
    (98, 99, 94, 95),
    (95, 96, 94, 95),
]


def bars(rows, start, step=15):
    found = []
    for index, (open_, high, low, close) in enumerate(rows):
        stamp = int((start + timedelta(minutes=step * index)).timestamp())
        found.append(Bar(stamp, open_, high, low, close, 10))
    return found


class OrderBlockTests(unittest.TestCase):
    def test_a_return_into_the_block_is_a_long_and_a_short(self):
        start = datetime(2026, 10, 7, 9, 30, tzinfo=ET)
        long = run_pine(SOURCE, bars(LONG_ROWS, start), INPUTS, root="NQ", timeframe="15m")
        short = run_pine(SOURCE, bars(SHORT_ROWS, start), INPUTS, root="NQ", timeframe="15m")
        self.assertEqual(len(long), 1)
        self.assertEqual(long[0]["side"], "long")
        self.assertEqual(long[0]["entry_price"], 117.25)
        self.assertEqual(long[0]["exit_signal"], "Long exit")
        self.assertEqual(long[0]["exit_price"], 125.0)
        self.assertEqual(long[0]["pnl"], 144.0)
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0]["side"], "short")
        self.assertEqual(short[0]["entry_price"], 102.75)
        self.assertEqual(short[0]["exit_signal"], "Short exit")
        self.assertEqual(short[0]["exit_price"], 95.0)
        self.assertEqual(short[0]["pnl"], 144.0)

    def test_the_halt_does_not_open_a_trade(self):
        start = datetime(2026, 10, 7, 16, 50, tzinfo=ET)
        trades = run_pine(SOURCE, bars(LONG_ROWS, start, step=1), INPUTS, root="NQ", timeframe="15m")
        self.assertEqual(trades, [])


if __name__ == "__main__":
    unittest.main()
