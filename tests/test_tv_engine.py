"""The TradingView fill rules that the published ES report uses."""

import unittest

from futuresfund.contracts import tick_size
from futuresfund.tv_engine import market_fill, stop_fill, to_tick, trade_net


class FillTests(unittest.TestCase):
    def test_es_tick_is_a_quarter_point(self):
        self.assertEqual(tick_size("ES1!"), 0.25)

    def test_same_close_round_trip_loses_thirty_six_dollars(self):
        # One tick each side is $25, and $5.50 is charged on each fill.
        entry = market_fill(7785.0, "buy", 0.25)
        exit_fill = market_fill(7785.0, "sell", 0.25)
        self.assertEqual(entry, 7785.25)
        self.assertEqual(exit_fill, 7784.75)
        self.assertEqual(trade_net(1, entry, exit_fill, 50), -36.0)

    def test_short_stop_fills_at_the_stop_plus_one_tick(self):
        entry = market_fill(7750.0, "sell", 0.25)
        # Six points against the short, plus one tick of slippage, is a $336 loss.
        exit_fill = market_fill(7756.0, "buy", 0.25)
        self.assertEqual(trade_net(-1, entry, exit_fill, 50), -336.0)


    def test_short_breakeven_stop_loses_thirty_six(self):
        # The stop sits one tick above the fill. Covering it costs one more tick.
        entry = 7724.0
        stop = to_tick(entry + 0.25, 0.25)
        bar = {"o": 7724.0, "h": 7725.0, "l": 7723.0, "c": 7724.0}
        fill = stop_fill(-1, stop, bar, 0.25)
        self.assertEqual(fill, 7724.5)
        self.assertEqual(trade_net(-1, entry, fill, 50), -36.0)

    def test_stop_prices_land_on_a_tick(self):
        self.assertEqual(to_tick(7785.491, 0.25), 7785.5)


if __name__ == "__main__":
    unittest.main()
