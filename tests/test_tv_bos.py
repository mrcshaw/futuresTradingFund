"""The evening breakout on the 5-minute ES file, checked against the tester."""

import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT.parent / "tradingEngine"
sys.path.insert(0, str(ENGINE))

from engine.backtester import Backtester
from engine.data_loader import DataLoader

_spec = importlib.util.spec_from_file_location(
    "bos_breakout_evening_strategy",
    ROOT / "futuresfund" / "learningStrategies" / "bos_breakout_evening_strategy.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
BOSBreakoutEveningStrategy = _mod.BOSBreakoutEveningStrategy


def _window():
    csv = next(ROOT.glob("CME_MINI_DL_ES1!, 5_*.csv"))
    raw = pd.read_csv(csv)
    raw["datetime"] = pd.to_datetime(raw["time"], unit="s", utc=True)
    frame = DataLoader._normalize(
        raw[["datetime", "open", "high", "low", "close"]].assign(volume=0)
    )
    end = frame.index[-1]
    return frame[frame.index >= end - pd.Timedelta(days=30)]


class EveningBreakoutTester(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        report = Backtester(_window(), BOSBreakoutEveningStrategy).run(progress=False)
        cls.trades = report.trades
        cls.metrics = report.metrics

    def test_last_five_trades_match_the_tester(self):
        # Tester trades 34-38. Price is the position value divided by $50.
        expected = [
            ("Long", 7708.25, 951.50),
            ("Short", 7833.75, 976.50),
            ("Short", 7752.25, 976.50),
            ("Long", 7730.75, -198.50),
            ("Long", 7760.25, 976.50),
        ]
        got = []
        for trade in self.trades[-5:]:
            side = "Long" if trade.direction.value == 1 else "Short"
            got.append((side, round(trade.entry_price, 2), round(float(trade.profit), 2)))
        self.assertEqual(got, expected)

    def test_full_target_keeps_one_tick_of_entry_slippage(self):
        # $1,000 target, entry slipped 1 tick, limit not slipped, $5.50 each side.
        winners = [round(float(t.profit), 2) for t in self.trades if t.profit > 900]
        self.assertIn(976.50, winners)

    def test_account_is_twenty_five_thousand(self):
        self.assertEqual(BOSBreakoutEveningStrategy.initial_capital, 25000.0)
        net = round(sum(t.profit for t in self.trades), 2)
        self.assertEqual(net, round(self.metrics["net_profit"], 2))


if __name__ == "__main__":
    unittest.main()
