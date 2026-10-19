"""Research turns daily caps off, then can add them once, and keeps the take profit larger than the stop."""

import unittest


class ResearchRuleTests(unittest.TestCase):
    def test_research_starts_without_daily_limits_and_with_the_reward_floor(self):
        from futuresfund.research_rules import prepare_research

        prepared = prepare_research({
            "use_daily_limits": True,
            "daily_loss_limit": 600.0,
            "daily_profit_cap": 2500.0,
            "max_daily_trades": 4,
            "stop_dollars": 100.0,
            "tp_dollars": 100.0,
            "length": 10,
        })
        self.assertFalse(prepared["use_daily_limits"])
        self.assertEqual(prepared["daily_loss_limit"], 0.0)
        self.assertEqual(prepared["daily_profit_cap"], 0.0)
        self.assertEqual(prepared["max_daily_trades"], 4)
        self.assertEqual(prepared["stop_dollars"], 100.0)
        self.assertGreater(prepared["tp_dollars"], 100.0)

    def test_a_stop_of_100_keeps_the_take_profit_larger(self):
        from futuresfund.learn import _clamp_plan

        held = _clamp_plan({"stop_dollars": 100, "tp_dollars": 100, "length": 10}, {})
        self.assertGreater(held["tp_dollars"], 100)
        higher = _clamp_plan({"stop_points": 10, "take_profit_points": 80}, {})
        self.assertEqual(higher["take_profit_points"], 80)

    def test_raising_the_stop_keeps_the_take_profit_larger(self):
        from futuresfund.research_rules import focus_keys, hold_reward

        before = {"stop_dollars": 100.0, "tp_dollars": 120.0}
        after = hold_reward({"stop_dollars": 150.0, "tp_dollars": 120.0})
        self.assertGreater(after["tp_dollars"], 150.0)
        self.assertEqual(focus_keys(before, after), ["stop_dollars"])

    def test_the_dollar_stop_stays_under_1500_and_the_target_under_2000(self):
        from futuresfund.learn import _clamp_plan
        from futuresfund.research_rules import hold_reward

        held = hold_reward({"stop_dollars": 2200, "tp_dollars": 5000})
        self.assertEqual(held["stop_dollars"], 1500)
        self.assertEqual(held["tp_dollars"], 2000)
        self.assertGreater(held["tp_dollars"], held["stop_dollars"])
        clamped = _clamp_plan({"stop_dollars": 1800.0, "tp_dollars": 4000.0, "length": 10}, {})
        self.assertEqual(clamped["stop_dollars"], 1500.0)
        self.assertEqual(clamped["tp_dollars"], 2000.0)
        self.assertEqual(clamped["length"], 10)

    def test_a_library_profit_keeps_the_opening_and_a_smaller_profit_walks_the_exits_up(self):
        from unittest.mock import patch

        from futuresfund.learn import next_plan

        origin = {"stop_pct": 0.1, "tp_pct": 1.0, "length": 10}
        limits = {"use_profit": True, "min_profit": 1000.0}
        with patch("futuresfund.book.strategy_acceptance", return_value=limits):
            kept = next_plan(origin, [{
                "params": dict(origin), "net_profit": 1500, "max_drawdown": 200, "trades": 10,
            }])
            short = next_plan(origin, [{
                "params": dict(origin), "net_profit": 500, "max_drawdown": 200, "trades": 10,
            }])
            losing = next_plan(origin, [{
                "params": dict(origin), "net_profit": -400, "max_drawdown": 200, "trades": 10,
            }])
            capped = next_plan(origin, [
                {"params": dict(origin), "net_profit": -400, "max_drawdown": 200, "trades": 10},
                {"params": {"stop_pct": 3.0, "tp_pct": 1.0, "length": 10}, "net_profit": -300, "max_drawdown": 200, "trades": 10},
            ])
            best = next_plan(origin, [
                {"params": dict(origin), "net_profit": -400, "max_drawdown": 200, "trades": 10},
                {"params": {"stop_pct": 3.0, "tp_pct": 1.0, "length": 10}, "net_profit": -50, "max_drawdown": 200, "trades": 10},
                {"params": {"stop_pct": 3.0, "tp_pct": 4.0, "length": 10}, "net_profit": -300, "max_drawdown": 200, "trades": 10},
            ])
            dollars = next_plan(
                {"stop_dollars": 50.0, "tp_dollars": 500.0, "length": 10},
                [{"params": {"stop_dollars": 50.0, "tp_dollars": 500.0, "length": 10}, "net_profit": -250, "max_drawdown": 40, "trades": 8}],
            )
        self.assertEqual(kept["stop_pct"], 0.1)
        self.assertEqual(kept["tp_pct"], 1.0)
        self.assertNotEqual(kept["length"], 10)
        self.assertEqual(short["stop_pct"], 0.5)
        self.assertEqual(short["tp_pct"], 1.0)
        self.assertEqual(losing["stop_pct"], 0.5)
        self.assertEqual(losing["tp_pct"], 1.0)
        self.assertEqual(losing["length"], 10)
        self.assertEqual(capped["stop_pct"], 3.0)
        self.assertEqual(capped["tp_pct"], 3.5)
        self.assertEqual(best["stop_pct"], 3.0)
        self.assertEqual(best["tp_pct"], 1.0)
        self.assertNotEqual(best["length"], 10)
        self.assertEqual(dollars["stop_dollars"], 250.0)
        self.assertEqual(dollars["tp_dollars"], 500.0)

    def test_a_1000_stop_and_1500_target_starts_at_250_and_500(self):
        from futuresfund.learn import _raise_stop_hundred, _search, _start_on_the_ladder

        opened = _start_on_the_ladder({"stop_dollars": 1000.0, "tp_dollars": 1500.0, "length": 20})
        self.assertEqual(opened["stop_dollars"], 250.0)
        self.assertEqual(opened["tp_dollars"], 500.0)
        self.assertEqual(opened["length"], 20)
        landed = _raise_stop_hundred({"stop_dollars": 250.0, "tp_dollars": 500.0}, "stop_dollars", set(), 50000)
        self.assertEqual(landed["stop_dollars"], 500.0)
        self.assertEqual(landed["tp_dollars"], 750.0)
        topped = _raise_stop_hundred({"stop_dollars": 1250.0, "tp_dollars": 1500.0}, "stop_dollars", set(), 50000)
        self.assertEqual(topped["stop_dollars"], 1500.0)
        self.assertEqual(topped["tp_dollars"], 1750.0)
        from futuresfund.learn import _raise_target_hundred

        target = _raise_target_hundred({"stop_dollars": 1500.0, "tp_dollars": 1750.0}, "tp_dollars", set(), 50000)
        self.assertEqual(target["stop_dollars"], 1500.0)
        self.assertEqual(target["tp_dollars"], 2000.0)
        seen = []

        def execute(params):
            seen.append(params.get("stop_dollars"))
            return {"net_profit": -float(params["stop_dollars"]), "max_drawdown": 200.0, "trades": 8, "days": 20}

        _search({"stop_dollars": 1000, "tp_dollars": 1500, "length": 20}, execute, 50000, 6, None, None, None, extras=False)
        self.assertEqual(seen, [250, 500, 750, 1000, 1250, 1500])

    def test_a_daily_limit_is_a_normal_input_and_a_gain_keeps_it(self):
        from futuresfund.learn import _search

        seen = []

        def execute(params):
            seen.append({
                "use_daily_limits": params.get("use_daily_limits"),
                "daily_loss_limit": params.get("daily_loss_limit"),
                "daily_profit_cap": params.get("daily_profit_cap"),
                "length": params.get("length"),
            })
            return {"net_profit": float(100 + len(seen)), "max_drawdown": 10.0, "trades": 20, "days": 20}

        _search(
            {"length": 10, "use_daily_limits": True, "daily_loss_limit": 600.0, "daily_profit_cap": 2500.0},
            execute,
            50000,
            3,
            None,
            None,
            None,
            extras=False,
        )
        self.assertFalse(seen[0]["use_daily_limits"])
        self.assertEqual(seen[0]["daily_loss_limit"], 0.0)
        self.assertEqual(seen[0]["daily_profit_cap"], 0.0)
        self.assertEqual(seen[1]["daily_loss_limit"], 600.0)
        self.assertEqual(seen[1]["length"], 10)
        self.assertEqual(seen[1]["daily_profit_cap"], 0.0)
        self.assertEqual(seen[2]["daily_loss_limit"], 600.0)
        self.assertEqual(seen[2]["daily_profit_cap"], 2500.0)
        self.assertEqual(seen[2]["length"], 10)

    def test_the_guide_states_the_daily_limit_and_the_reward_floor(self):
        from futuresfund.learn import research_guidelines

        text = research_guidelines()
        self.assertIn("larger than the stop", text)
        self.assertIn("under 5 percent of the account", text)
        self.assertIn("3.6 percent", text)
        self.assertIn("$1,500", text)
        self.assertIn("$2,000", text)
        self.assertIn("No ATR stop.", text)
        self.assertNotIn("300,000", text)
