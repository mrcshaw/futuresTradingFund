"""The engine-table columns make one smooth curve. Better backtests sit higher."""

import json
import math
import unittest
from pathlib import Path


def _base(**updates):
    row = {
        "days": 260,
        "net_profit": 5000.0,
        "max_drawdown": 1500.0,
        "profit_factor": 1.2,
        "profit_ratio": 1.3,
        "trades": 200,
        "win_rate": 45.0,
    }
    row.update(updates)
    return row


def _correlation(xs, ys):
    count = len(xs)
    if count < 3:
        return None
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    spread_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    spread_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if spread_x == 0 or spread_y == 0:
        return None
    return num / (spread_x * spread_y)


class ResultCurveTests(unittest.TestCase):
    def test_each_column_moves_the_curve_the_right_way(self):
        from futuresfund.objective import result_curve

        start = result_curve(_base())
        self.assertGreater(result_curve(_base(net_profit=12000)), start)
        self.assertLess(result_curve(_base(max_drawdown=8000)), start)
        self.assertGreater(result_curve(_base(profit_factor=2.0)), start)
        self.assertGreater(result_curve(_base(profit_ratio=2.0)), start)
        self.assertGreater(result_curve(_base(win_rate=60)), start)
        self.assertGreater(result_curve(_base(days=260)), result_curve(_base(days=30)))
        self.assertGreater(result_curve(_base(trades=200)), result_curve(_base(trades=8)))
        self.assertGreater(result_curve(_base(trades=200)), result_curve(_base(trades=1400)))

    def test_a_one_dollar_change_does_not_jump(self):
        from futuresfund.objective import result_curve

        low = result_curve(_base(net_profit=4999))
        high = result_curve(_base(net_profit=5001))
        self.assertLess(abs(high - low), 0.01)

    def test_a_rising_sequence_smooths_upward_and_a_falling_one_smooths_down(self):
        from futuresfund.objective import smooth_result_curve

        rising = [
            _base(net_profit=profit, max_drawdown=drawdown, win_rate=win)
            for profit, drawdown, win in (
                (-2000, 6000, 28),
                (0, 4500, 32),
                (2000, 3000, 36),
                (5000, 2000, 42),
                (8000, 1400, 48),
                (12000, 900, 55),
            )
        ]
        up = smooth_result_curve(rising)
        self.assertEqual(len(up), len(rising))
        self.assertTrue(all(up[index] < up[index + 1] for index in range(len(up) - 1)))
        down = smooth_result_curve(list(reversed(rising)))
        self.assertTrue(all(down[index] > down[index + 1] for index in range(len(down) - 1)))

    def test_a_clock_stamp_is_not_a_result(self):
        from futuresfund.objective import result_curve

        self.assertIsNone(result_curve({}))
        self.assertIsNone(result_curve({"time": "02:16:01"}))

    def test_real_trials_stay_on_the_curve_and_profit_still_lifts_it(self):
        from futuresfund.objective import result_curve

        path = Path(__file__).resolve().parents[1] / "futuresfund" / "researchNotes" / "trendlines.json"
        trials = json.loads(path.read_text(encoding="utf-8"))["trials"]
        scored = []
        for row in trials:
            height = result_curve(row)
            if height is None:
                continue
            self.assertGreater(height, 0.0)
            self.assertLess(height, 1.0)
            scored.append((height, float(row["net_profit"])))
        self.assertGreater(len(scored), 50)
        link = _correlation([profit for _height, profit in scored], [height for height, _profit in scored])
        self.assertIsNotNone(link)
        self.assertGreater(link, 0.2)
