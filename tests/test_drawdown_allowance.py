"""The drawdown limit is tight while a new account is under its profit target, and grows slowly past it."""

import unittest
from unittest import mock

from futuresfund import objective
from futuresfund.tv_broker import deepest_dips, tv_metrics


def _trades(pnls):
    return [{"exit_time": str(index), "pnl": pnl} for index, pnl in enumerate(pnls)]


class AllowanceTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("drawdown_start", lambda size=None: 2500.0), ("drawdown_target", lambda size=None: 3000.0)):
            patcher = mock.patch.object(objective, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_allowed_drawdown_holds_until_the_target_then_grows_by_30_percent(self):
        self.assertEqual(objective.drawdown_allowance(0), 2500.0)
        self.assertEqual(objective.drawdown_allowance(3000), 2500.0)
        self.assertAlmostEqual(objective.drawdown_allowance(6000), 3400.0)
        self.assertAlmostEqual(objective.drawdown_allowance(10000), 4600.0)
        self.assertAlmostEqual(objective.drawdown_allowance(20000), 7600.0)

    def test_a_small_profit_no_longer_hides_a_deep_drawdown(self):
        self.assertFalse(objective.account_survives({"net_profit": 1000, "max_drawdown": 2600}))
        self.assertTrue(objective.account_survives({"net_profit": 1000, "max_drawdown": 2400}))

    def test_an_old_row_is_held_to_the_limit_at_its_final_profit(self):
        self.assertTrue(objective.account_survives({"net_profit": 10000, "max_drawdown": 4500}))
        self.assertFalse(objective.account_survives({"net_profit": 10000, "max_drawdown": 4700}))

    def test_each_dip_is_held_to_the_limit_at_the_high_it_fell_from(self):
        early = {"net_profit": 20000, "max_drawdown": 3000, "dips": [[500.0, 3000.0]]}
        late = {"net_profit": 20000, "max_drawdown": 6000, "dips": [[1000.0, 1000.0], [18000.0, 6000.0]]}
        self.assertFalse(objective.account_survives(early))
        self.assertTrue(objective.account_survives(late))

    def test_a_box_turned_off_has_no_limit(self):
        with mock.patch.object(objective, "drawdown_start", lambda size=None: float("inf")):
            self.assertTrue(objective.account_survives({"net_profit": 100, "max_drawdown": 90000}))


class DipTests(unittest.TestCase):
    def test_the_metrics_keep_the_deepest_dip_below_each_high(self):
        measured = tv_metrics(_trades([1000, -600, 2000, -300, -900, 4000]))
        self.assertEqual(measured["max_drawdown"], 1200.0)
        self.assertEqual(measured["dips"], [[1000.0, 600.0], [2400.0, 1200.0]])

    def test_a_dip_no_deeper_than_one_below_a_lower_high_is_left_out(self):
        self.assertEqual(deepest_dips({0.0: 200.0, 500.0: 100.0, 900.0: 700.0}), [[0.0, 200.0], [900.0, 700.0]])


if __name__ == "__main__":
    unittest.main()
