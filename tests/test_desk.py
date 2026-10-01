"""The futures floor uses the firm jobs, and the 4:45pm ET halt is a clock rule."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from futuresfund.roster import ROSTER
from futuresfund.session import trading_halt

ET = ZoneInfo("America/New_York")

NAMES = {
    "Quantitative Trader",
    "Portfolio Manager",
    "Floor Trader",
    "2 min researcher",
    "5 min researcher",
    "15 min researcher",
    "Risk Manager",
    "Trading Analyst",
    "2min chart developer",
    "5 min chart developer",
    "15 min chart developer",
    "Systems Administrator",
    "Compliance & Operations",
    "Ingestion",
}


class DeskTests(unittest.TestCase):
    def test_the_chart_folder_has_two_five_and_fifteen_minute_files(self):
        from futuresfund.charts import chart_paths

        found = chart_paths()
        self.assertTrue({"2m", "5m", "15m"} <= set(found))
        self.assertIn(", 15_", found["15m"].name)
        self.assertIn(", 5_", found["5m"].name)
        self.assertIn(", 2_", found["2m"].name)

    def test_the_better_timeframe_is_the_one_kept(self):
        from futuresfund.research import compare_timeframes

        rising = [{"t": str(i), "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 1} for i in range(80)]
        flat = [{"t": str(i), "o": 100, "h": 101, "l": 99, "c": 100, "v": 1} for i in range(80)]
        result = compare_timeframes(
            {"2m": rising, "5m": flat},
            "MES1!",
            50000,
            10,
            "ema_cross",
            {"fast": 5, "slow": 20, "stop": 500},
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["timeframe"], "2m")
        self.assertTrue(result["proven"])
        self.assertEqual({item["timeframe"] for item in result["frames"]}, {"2m", "5m"})

    def test_bollinger_and_macd_have_an_entry_and_a_separate_exit(self):
        from futuresfund.pines import render

        bollinger = render("bollinger", {"length": 14, "dev": 2, "stop": 250}, "Bollinger", 50, 50000)
        macd = render("macd", {"fast": 12, "slow": 34, "signal": 9, "stop": 250}, "MACD", 50, 50000)
        self.assertIn('strategy.entry("Long"', bollinger)
        self.assertIn('strategy.close("Long")', bollinger)
        self.assertIn("strategy.exit", bollinger)
        self.assertIn("ta.crossover", macd)
        self.assertIn("strategy.exit", macd)
        self.assertNotIn("no Pine form yet", bollinger)
        self.assertNotIn("no Pine form yet", macd)

    def test_the_floor_includes_the_indicator_researcher(self):
        self.assertIn("5 min researcher", {agent["name"] for agent in ROSTER})
        self.assertIn("15 min researcher", {agent["name"] for agent in ROSTER})
        self.assertIn("15 min chart developer", {agent["name"] for agent in ROSTER})

    def test_a_rising_market_is_accepted_before_one_hundred_changes(self):
        import tempfile
        from pathlib import Path
        from futuresfund import lab
        bars = []
        for i in range(80):
            price = 100 + i
            bars.append({"t": str(i), "o": price - 1, "h": price + 1, "l": price - 2, "c": price, "v": 10})
        original = lab.LAB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            lab.LAB_PATH = Path(tmp) / "lab.json"
            try:
                report = lab.search(bars, instrument="MES1!", account_size=50000, profit_target=10)
            finally:
                lab.LAB_PATH = original
        self.assertGreaterEqual(len(report["accepted"]), 3)
        self.assertTrue(all(item["changes"] <= 100 for item in report["scrapped"]))

    def test_a_fifty_k_account_fails_when_the_trail_is_touched(self):
        from futuresfund.research import _simulate

        bars = [{"t": str(i), "o": 5000, "h": 5000, "l": 5000 - i * 10, "c": 5000 - i * 10, "v": 1} for i in range(30)]
        result = _simulate(bars, [1] * len(bars), 1, 50, 50000)
        self.assertTrue(result["breached"])
        self.assertFalse(result["proven"])

    def test_a_giveback_larger_than_the_allowance_is_not_eligible(self):
        from futuresfund.research import _simulate

        bars = [
            {"t": "0", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1},
            {"t": "1", "o": 200, "h": 200, "l": 200, "c": 200, "v": 1},
            {"t": "2", "o": 150, "h": 150, "l": 150, "c": 150, "v": 1},
        ]
        result = _simulate(bars, [1, 1, 1], 1, 50, 50000)
        self.assertGreater(result["max_drawdown"], 2000)
        self.assertFalse(result["proven"])

    def test_stored_rules_over_the_limit_are_rejected(self):
        from futuresfund.prop_rules import gate

        item = {"title": "Bollinger 20 x2", "qty": 1, "recommended": True, "backtest": {"net_profit": 32245.0, "max_drawdown": 18969.0, "trades": 767}}
        self.assertFalse(gate(item, 50000))
        self.assertFalse(item["recommended"])

    def test_a_stop_flattens_before_the_trail_is_touched(self):
        from futuresfund.research import _simulate

        bars = [{"t": str(i), "o": 100 - i * 20, "h": 100, "l": 100 - i * 20, "c": 100 - i * 20, "v": 1} for i in range(10)]
        result = _simulate(bars, [1] * len(bars), 1, 50, 50000, stop=500)
        self.assertFalse(result["breached"])
        self.assertLessEqual(result["max_drawdown"], 2000)

    def test_five_contracts_are_over_the_fifty_k_cap(self):
        from futuresfund.research import _simulate

        bars = [{"t": str(i), "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 1} for i in range(20)]
        result = _simulate(bars, [1] * len(bars), 5, 5, 50000)
        self.assertFalse(result["proven"])
    def test_the_floor_lists_the_futures_jobs(self):
        self.assertEqual({agent["name"] for agent in ROSTER}, NAMES)

    def test_orders_stop_fifteen_minutes_before_the_five_pm_halt(self):
        self.assertIsNone(trading_halt(datetime(2026, 9, 25, 16, 44, tzinfo=ET)))
        self.assertIsNotNone(trading_halt(datetime(2026, 9, 25, 16, 45, tzinfo=ET)))
        self.assertIsNotNone(trading_halt(datetime(2026, 9, 25, 17, 30, tzinfo=ET)))
        self.assertIsNone(trading_halt(datetime(2026, 9, 25, 18, 0, tzinfo=ET)))


if __name__ == "__main__":
    unittest.main()
