"""An ATR stop is checked on the bar it is placed, and a gap cannot lose more than the dollar stop."""

import unittest

from futuresfund.backtrader_engine import Bar
from futuresfund.tv_broker import Broker


def _bar(price_open, high, low, close) -> Bar:
    return Bar(epoch=1_700_000_000, o=price_open, h=high, l=low, c=close, v=1)


def _long(broker, entry: float) -> None:
    broker.position = 1
    broker._open = {
        "index": 0,
        "checks_from": 0,
        "side": "long",
        "size": 1,
        "entry_time": "2026-10-15 10:00",
        "entry_ms": 0,
        "entry_price": entry,
        "entry_signal": "L",
        "entry_fee": 0.0,
        "best": entry,
        "worst": entry,
    }


class AtrStopTests(unittest.TestCase):
    def test_a_wide_atr_stop_is_checked_on_the_same_bar(self):
        broker = Broker([_bar(20000, 20010, 19800, 19850)], point=20, commission=0, slip_ticks=0, on_close=False)
        broker.arm_risk({"stop_dollars": 1500, "tp_dollars": 2000})
        _long(broker, 20000)
        broker.exit("x", "L", stop=19000, limit=22000)
        broker.end_bar(0)
        self.assertEqual(len(broker.trades), 1)
        self.assertAlmostEqual(broker.trades[0]["exit_price"], 19925, places=2)
        self.assertGreaterEqual(broker.trades[0]["pnl"], -1500)

    def test_a_gap_through_the_stop_does_not_lose_more_than_the_dollar_stop(self):
        broker = Broker([_bar(19000, 19000, 18900, 18950)], point=20, commission=0, slip_ticks=0, on_close=False)
        broker.arm_risk({"stop_dollars": 1500, "tp_dollars": 2000})
        _long(broker, 20000)
        broker.exit("x", "L", stop=19000, limit=22000)
        broker.start_bar(0)
        self.assertEqual(len(broker.trades), 1)
        self.assertAlmostEqual(broker.trades[0]["exit_price"], 19925, places=2)
        self.assertGreaterEqual(broker.trades[0]["pnl"], -1500)

    def test_commission_stays_inside_the_1500_cap(self):
        broker = Broker([_bar(20000, 20010, 19800, 19850)], point=20, commission=5.50, slip_ticks=0, on_close=False)
        broker.arm_risk({"stop_dollars": 1500, "tp_dollars": 2000})
        _long(broker, 20000)
        broker.exit("x", "L", stop=19000, limit=22000)
        broker.end_bar(0)
        self.assertEqual(len(broker.trades), 1)
        self.assertGreaterEqual(broker.trades[0]["pnl"], -1500)
        self.assertGreater(broker.trades[0]["exit_price"], 19925)
