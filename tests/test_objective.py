"""The research objective: hit the headquarters profit target inside its drawdown at 130 to 600 trades a year, then the lowest drawdown, then profit."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _row(profit, drawdown, trades, **extra):
    row = {"net_profit": profit, "max_drawdown": drawdown, "trades": trades}
    row.update(extra)
    return row


class LimitTests(unittest.TestCase):
    def test_the_target_and_the_drawdown_come_from_headquarters(self):
        from futuresfund.objective import band_width, drawdown_limit, limits, profit_target
        from futuresfund.prop_rules import active_limits

        rules = active_limits(50000)
        self.assertEqual(profit_target(), float(rules["profit_target"]))
        self.assertEqual(drawdown_limit(), float(rules["max_drawdown"]))
        self.assertEqual(limits()["size"], 50000.0)
        self.assertEqual(band_width(), round(drawdown_limit() * 0.05, 2))

    def test_the_stamp_changes_when_headquarters_changes_the_figures(self):
        from unittest import mock

        from futuresfund import objective
        from futuresfund.library import OBJECTIVE_TAG

        self.assertEqual(objective.stamp(), f"{OBJECTIVE_TAG}@{objective.profit_target():.0f}/{objective.drawdown_limit():.0f}/4-7w")
        with mock.patch.object(objective, "limits", lambda: {"profit_target": 4500.0, "max_drawdown": 2500.0, "size": 50000.0}):
            self.assertEqual(objective.stamp(), f"{OBJECTIVE_TAG}@4500/2500/4-7w")
            self.assertEqual(objective.band_width(), 125.0)

    def test_a_research_success_is_a_profit_inside_twenty_percent_of_the_account(self):
        from futuresfund.objective import research_drawdown_cap, research_success

        self.assertEqual(research_drawdown_cap(50000), 35000.0)
        paced = {"net_profit": 800.0, "max_drawdown": 35000.0, "trades": 20, "days": 20}
        self.assertTrue(research_success(paced, 50000))
        self.assertTrue(research_success({**paced, "max_drawdown": 25000.0}, 50000))
        band = {"net_profit": 53895.0, "max_drawdown": 29523.5, "trades": 400, "days": 260}
        self.assertTrue(research_success(band, 50000))
        self.assertFalse(research_success({"net_profit": 53895.0, "max_drawdown": 29523.5, "trades": 1335, "days": 260}, 50000))
        self.assertFalse(research_success({**paced, "max_drawdown": 35000.01}, 50000))
        self.assertFalse(research_success({**paced, "net_profit": 0}, 50000))
        self.assertFalse(research_success({**paced, "net_profit": -40}, 50000))
        self.assertFalse(research_success({"net_profit": 9000.0, "max_drawdown": 100.0, "trades": 2, "days": 20}, 50000))

    def test_a_profit_at_least_1_5_times_the_drawdown_counts_with_a_real_trade_count(self):
        from futuresfund.objective import counted_success, profit_to_drawdown, ratio_success, research_success

        measured = {"net_profit": 53895.0, "max_drawdown": 29523.5, "trades": 400, "days": 260}
        self.assertGreater(profit_to_drawdown(measured), 1.5)
        self.assertTrue(research_success(measured, 50000))
        self.assertTrue(ratio_success(measured))
        self.assertTrue(counted_success(measured, 50000))
        self.assertFalse(ratio_success({**measured, "trades": 1335}))
        self.assertFalse(ratio_success({"net_profit": 30000.0, "max_drawdown": 29523.5, "trades": 400, "days": 260}))
        self.assertFalse(ratio_success({"net_profit": 90000.0, "max_drawdown": 1000.0, "trades": 2, "days": 20}))
        self.assertFalse(ratio_success({"net_profit": 90000.0, "max_drawdown": 1000.0, "trades": 10, "days": 260}))
        self.assertFalse(ratio_success({"net_profit": -53895.0, "max_drawdown": 29523.5, "trades": 1335, "days": 260}))


class RankTests(unittest.TestCase):
    def setUp(self):
        from futuresfund.objective import drawdown_limit, profit_target

        self.target = profit_target()
        self.trail = drawdown_limit()

    def test_a_result_that_hits_the_target_beats_one_that_does_not_whatever_the_drawdown(self):
        from futuresfund.objective import rank

        self.assertGreater(rank(_row(self.target, self.trail - 100, 20)), rank(_row(self.target - 1, 50, 20)))

    def test_among_results_that_hit_the_target_the_smaller_drawdown_beats_the_larger_profit(self):
        from futuresfund.objective import rank

        steady = _row(self.target + 200, 300, 12)
        rich = _row(self.target * 3, 900, 20)
        self.assertGreater(rank(steady), rank(rich))

    def test_inside_the_same_band_the_larger_profit_wins(self):
        from futuresfund.objective import band_width, rank

        band = band_width()
        self.assertGreater(rank(_row(self.target + 900, 2 * band + 60, 15)), rank(_row(self.target + 100, 2 * band + 15, 15)))

    def test_under_the_target_the_larger_profit_wins_so_the_search_climbs(self):
        from futuresfund.objective import rank

        self.assertGreater(rank(_row(self.target - 100, 700, 15)), rank(_row(200, 50, 15)))

    def test_the_full_chart_search_keeps_the_lower_drawdown_once_a_run_is_profitable(self):
        from futuresfund.objective import research_rank

        tight = _row(1750, 14925, 19)
        rich = _row(9000, 36012, 35)
        loss = _row(-9112, 2000, 34)
        flat = _row(0, 0, 0)
        self.assertGreater(research_rank(loss), research_rank(tight))
        self.assertGreater(research_rank(tight), research_rank(rich))
        self.assertGreater(research_rank(rich), research_rank(flat))
        self.assertGreater(research_rank(_row(800, 200, 4)), research_rank(_row(100, 200, 4)))

    def test_two_or_three_trades_lose_to_a_result_at_four_to_seven_a_week(self):
        from futuresfund.objective import research_rank, tradable

        held = _row(118180, 11100, 2, days=1291)
        three = _row(50000, 1000, 3, days=1291)
        paced = _row(4000, 20000, 1291, days=1291)
        self.assertFalse(tradable(held))
        self.assertFalse(tradable(three))
        self.assertTrue(tradable(paced))
        busy = _row(152130, 8000, 4268, days=1291)
        self.assertFalse(tradable(busy))
        self.assertGreater(research_rank(paced), research_rank(held))
        self.assertGreater(research_rank(paced), research_rank(three))
        self.assertGreater(research_rank(paced), research_rank(busy))
        self.assertGreater(research_rank(_row(2000, 400, 1033, days=1291)), research_rank(paced))
        self.assertGreater(research_rank(_row(2000, 400, 1033, days=1291)), research_rank(busy))

    def test_trade_count_only_breaks_a_tie(self):
        from futuresfund.objective import rank

        self.assertGreater(rank(_row(self.target + 10, 200, 8)), rank(_row(self.target + 10, 900, 40)))
        self.assertGreater(rank(_row(self.target, 400, 20)), rank(_row(self.target, 400, 8)))
        self.assertGreater(rank(_row(2000, 800, 8)), rank(_row(500, 100, 40)))
        self.assertGreater(rank(_row(self.target, 900, 8)), rank(_row(self.target * 3, 100, 2)))

    def test_crossing_the_drawdown_is_a_loss_even_with_the_target_hit(self):
        from futuresfund.objective import TIER_COUNTS, TIER_LOSING, acceptable, rank, research_drawdown_cap, tier

        cap = research_drawdown_cap(50000)
        deep = _row(self.target * 2, cap + 1, 20)
        self.assertEqual(tier(deep), TIER_LOSING)
        self.assertFalse(acceptable(deep))
        self.assertTrue(acceptable(_row(self.target, cap, 20, days=20)))
        inside = _row(self.target * 2, 500, 20)
        self.assertEqual(tier(inside), TIER_COUNTS)
        self.assertGreater(rank(_row(50, 950, 9)), rank(deep))

    def test_the_engine_stop_at_the_target_counts_as_hitting_it(self):
        from futuresfund.objective import acceptable, hit_target

        stopped = _row(self.target - 400, 300, 9, stop_reason="Profit target reached")
        self.assertTrue(hit_target(stopped))
        self.assertTrue(acceptable(stopped))
        self.assertFalse(hit_target(_row(self.target - 400, 300, 9)))

    def test_no_trades_is_not_a_result(self):
        from futuresfund.objective import TIER_NONE, rank, tier

        flat = _row(0.0, 0.0, 0)
        self.assertEqual(tier(flat), TIER_NONE)
        self.assertEqual(tier(_row(None, None, 0, error="boom")), TIER_NONE)
        self.assertGreater(rank(_row(-200, 300, 9)), rank(flat))

    def test_improved_counts_and_acceptable(self):
        from futuresfund.objective import acceptable, counts, improved

        self.assertTrue(improved(_row(self.target + 50, 250, 10), _row(self.target + 500, 400, 10)))
        self.assertFalse(improved(_row(900, 600, 10), _row(1500, 300, 10)))
        self.assertTrue(counts(_row(self.target, self.trail, 8)))
        self.assertFalse(counts(_row(self.target, self.trail, 0)))
        self.assertFalse(counts(_row(self.target, self.trail, 2)))
        self.assertFalse(counts(_row(self.target, self.trail, 3)))
        self.assertTrue(acceptable(_row(self.target, self.trail, 8)))
        self.assertFalse(counts(_row(self.target - 1, 50, 80)))
        self.assertFalse(counts(_row(-1, 50, 80)))

    def test_describe_names_the_target_the_drawdown_the_pace_and_the_order(self):
        from futuresfund.objective import describe, short

        text = describe()
        self.assertIn("$3,000 profit target", text)
        self.assertIn("one-year drawdown is no higher than $35,000", text)
        self.assertIn("$50,000 account", text)
        self.assertIn("set on headquarters", text)
        self.assertIn("130 to 600 trades a year", text)
        self.assertIn("too many trades", text)
        self.assertIn("Two or three trades is not a strategy", text)
        from futuresfund.objective import scale_min_trades

        self.assertIn(f"trade count of {scale_min_trades():g}", text)
        self.assertIn("strategy library page", text)
        from futuresfund.book import strategy_acceptance

        pct = float(strategy_acceptance()["drawdown_pct"])
        self.assertIn(f"keep the drawdown under {pct:g} percent of the account, and meet the trade count", text)
        self.assertIn("A single loss cannot be more than 3 percent of the account", text)
        self.assertIn("larger than the stop loss", text)
        self.assertIn("more than 3 percent of the account", text)
        self.assertIn("Trade count only breaks a tie", text)
        self.assertIn("130 to 600 trades a year", short())

    def test_a_series_of_losses_under_2500_before_300000_profit_is_not_included(self):
        from futuresfund.objective import account_survives

        self.assertFalse(account_survives({"net_profit": 2000, "max_drawdown": 2600}))
        self.assertFalse(account_survives({"net_profit": 3000, "max_drawdown": 2600}))
        self.assertFalse(account_survives({"net_profit": 9000, "max_drawdown": 2500}))
        self.assertTrue(account_survives({"net_profit": 1000, "max_drawdown": 2499}))
        self.assertTrue(account_survives({"net_profit": 100, "max_drawdown": 0}))


class PaceTests(unittest.TestCase):
    """Several weeks of bars and two trades is one trade held for weeks, not a strategy."""

    def setUp(self):
        from futuresfund.objective import drawdown_limit, profit_target

        self.target = profit_target()
        self.trail = drawdown_limit()

    def test_a_few_trades_over_weeks_do_not_count_however_large_the_profit(self):
        from futuresfund.objective import TIER_OFF_PACE, acceptable, counts, pace, rank, tier

        held = _row(17378, 111, 2, days=20)
        self.assertEqual(pace(held), 0.1)
        self.assertEqual(tier(held), TIER_OFF_PACE)
        self.assertFalse(acceptable(held))
        self.assertFalse(counts(held))
        paced_hit = _row(3200, 900, 18, days=20)
        paced_short = _row(1500, 600, 16, days=20)
        self.assertTrue(counts(paced_hit))
        self.assertGreater(rank(paced_hit), rank(held))
        self.assertGreater(rank(paced_short), rank(held))

    def test_more_than_600_trades_a_year_does_not_count(self):
        from futuresfund.objective import TIER_COUNTS, TIER_OFF_PACE, paced, tier

        busy = _row(5000, 500, 90, days=20)
        self.assertFalse(paced(busy))
        self.assertEqual(tier(busy), TIER_OFF_PACE)
        self.assertTrue(paced(_row(5000, 500, 40, days=20)))
        self.assertTrue(paced(_row(5000, 500, 24, days=20)))
        self.assertTrue(paced(_row(5000, 500, 16, days=20)))
        self.assertEqual(tier(_row(5000, 500, 40, days=20)), TIER_COUNTS)
        self.assertFalse(paced(_row(5000, 500, 8, days=20)))
        self.assertFalse(paced(_row(5000, 500, 50, days=20)))

    def test_off_pace_results_climb_toward_the_band_before_profit(self):
        from futuresfund.objective import pace_gap, rank

        nearer = _row(2000, 300, 50, days=260)
        richer = _row(9000, 100, 2000, days=260)
        self.assertEqual(pace_gap(nearer), 80)
        self.assertEqual(pace_gap(richer), 1400)
        self.assertGreater(rank(nearer), rank(richer))
        self.assertEqual(pace_gap(_row(2000, 300, 20, days=20)), 0.0)

    def test_a_loss_stays_below_an_off_pace_profit(self):
        from futuresfund.objective import rank

        self.assertGreater(rank(_row(9000, 100, 2, days=20)), rank(_row(-50, 100, 30, days=20)))

    def test_days_come_from_the_result_or_the_research_chart(self):
        from unittest import mock

        from futuresfund import objective

        self.assertEqual(objective.days_for(_row(100, 10, 5, days=12)), 12.0)
        self.assertIsNone(objective.days_for(_row(100, 10, 5)))
        with mock.patch.object(objective, "research_days", lambda root, timeframe: 20 if timeframe == "15m" and root in {"GC", "QO"} else None):
            self.assertEqual(objective.days_for(_row(100, 10, 5, timeframe="15m", contract="GC1!")), 20.0)
            self.assertEqual(objective.pace(_row(100, 10, 30, timeframe="15m", instrument="QO")), 1.5)
            self.assertIsNone(objective.pace(_row(100, 10, 30, timeframe="2m", instrument="ES")))

    def test_unknown_days_are_not_held_against_a_result(self):
        from futuresfund.objective import counts, paced

        unknown = _row(self.target, 400, 8)
        self.assertIsNone(paced(unknown))
        self.assertTrue(counts(unknown))
        self.assertFalse(counts(_row(self.target, 400, 2)))
        self.assertFalse(counts(_row(self.target, 400, 3)))

    def test_a_trading_day_opens_at_six_in_the_evening_new_york(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from futuresfund.objective import session_days

        et = ZoneInfo("America/New_York")
        sunday_evening = int(datetime(2026, 9, 13, 18, 30, tzinfo=et).timestamp())
        monday_morning = int(datetime(2026, 9, 14, 9, 30, tzinfo=et).timestamp())
        monday_evening = int(datetime(2026, 9, 14, 19, 0, tzinfo=et).timestamp())
        tuesday_noon = int(datetime(2026, 9, 15, 12, 0, tzinfo=et).timestamp())
        self.assertEqual(session_days([sunday_evening, monday_morning]), 1)
        self.assertEqual(session_days([sunday_evening, monday_morning, monday_evening, tuesday_noon]), 2)
        self.assertEqual(session_days([]), 0)

    def test_the_research_window_is_one_year(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from futuresfund.objective import RESEARCH_SESSIONS, session_days, window_start

        et = ZoneInfo("America/New_York")
        epochs = []
        day = datetime(2024, 1, 2, 12, 0, tzinfo=et)
        while len(epochs) < 400:
            if day.weekday() < 5:
                epochs.append(int(day.timestamp()))
            day += timedelta(days=1)
        self.assertEqual(RESEARCH_SESSIONS, 260)
        start = window_start(epochs)
        kept = [stamp for stamp in epochs if stamp >= start]
        self.assertEqual(session_days(kept), 260)
        self.assertEqual(window_start(epochs[:10]), epochs[0])
        self.assertIsNone(window_start([]))

    def test_a_full_run_keeps_every_session_and_a_chart_run_keeps_one_year(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from futuresfund.backtrader_engine import execute_script
        from futuresfund.objective import RESEARCH_SESSIONS

        et = ZoneInfo("America/New_York")
        bars = []
        day = datetime(2021, 10, 4, 12, 0, tzinfo=et)
        price = 4000.0
        while len(bars) < 400:
            if day.weekday() < 5:
                price += 1.0
                bars.append({"t": int(day.timestamp()), "o": price, "h": price + 2, "l": price - 2, "c": price + 0.5, "v": 10})
            day += timedelta(days=1)
        source = (
            "//@version=5\nstrategy(\"Span\", overlay=true)\n"
            "fast = ta.ema(close, 5)\nif ta.crossover(close, fast)\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 5, limit=close + 10)\n"
        )
        year = execute_script(source, bars, "15m", None, "ES", "span", 0)
        full = execute_script(source, bars, "15m", None, "ES", "span", 0, full=True)
        self.assertEqual(year["days"], RESEARCH_SESSIONS)
        self.assertEqual(year["window"], "one year")
        self.assertFalse(year["full_dataset"])
        self.assertEqual(full["days"], 400)
        self.assertEqual(full["window"], "five years")
        self.assertTrue(full["full_dataset"])

    def test_the_engine_reports_the_days_and_the_pace(self):
        from futuresfund.backtrader_engine import execute_script

        bars = []
        price = 5000.0
        start = 1_757_800_000  # September 2026
        for index in range(26 * 60 * 24 // 5):
            price += 1.5 if index % 7 else -4.0
            bars.append({"t": start + index * 300, "o": price, "h": price + 2, "l": price - 2, "c": price + 0.5, "v": 100})
        source = (
            "//@version=5\nstrategy(\"Pace\", overlay=true)\nlen = input.int(20, minval=5, maxval=100)\n"
            "fast = ta.ema(close, len)\nif ta.crossover(close, fast)\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 5, limit=close + 10)\n"
        )
        measured = execute_script(source, bars, "5m", {"len": 20}, "ES", "pace", 1)
        self.assertGreater(measured["days"], 20)
        self.assertEqual(measured["trades_per_day"], round(measured["trades"] / measured["days"], 2))


class ResearcherTests(unittest.TestCase):
    def test_a_profitable_opening_does_not_raise_the_stop(self):
        from unittest.mock import patch

        from futuresfund.learn import next_plan

        limits = {"use_profit": True, "min_profit": 1000.0}
        with patch("futuresfund.book.strategy_acceptance", return_value=limits):
            plan = next_plan(
                {"stop": 20, "zone": 1.5},
                [{"params": {"stop": 20, "zone": 1.5}, "net_profit": 3500, "max_drawdown": 900, "trades": 10}],
            )
        self.assertEqual(plan["stop"], 20)
        self.assertNotEqual(plan["zone"], 1.5)

    def test_the_same_trade_count_does_not_extend_a_change_when_profit_rises(self):
        from futuresfund.learn import next_plan

        trials = [
            {"params": {"length": 10}, "net_profit": 500, "max_drawdown": 300, "trades": 10},
            {"params": {"length": 14}, "net_profit": 900, "max_drawdown": 600, "trades": 10},
        ]
        plan = next_plan({"length": 10}, trials)
        self.assertLess(plan["length"], 14)

    def test_a_profit_under_the_drawdown_line_does_not_extend_the_change(self):
        from futuresfund.learn import next_plan

        trials = [
            {"params": {"length": 10}, "net_profit": -500, "max_drawdown": 300, "trades": 10},
            {"params": {"length": 14}, "net_profit": 200, "max_drawdown": 900, "trades": 10},
        ]
        plan = next_plan({"length": 10}, trials)
        self.assertLess(plan["length"], 14)

    def test_more_profit_that_crosses_the_trail_is_reversed(self):
        from futuresfund.learn import next_plan

        trials = [
            {"params": {"length": 10}, "net_profit": 500, "max_drawdown": 300, "trades": 10},
            {"params": {"length": 14}, "net_profit": 900, "max_drawdown": 2600, "trades": 10},
        ]
        plan = next_plan({"length": 10}, trials)
        self.assertLess(plan["length"], 14)

    def test_fewer_trades_is_reversed_even_when_profit_rises(self):
        from futuresfund.learn import next_plan

        climbed = [
            {"params": {"length": 10}, "net_profit": 500, "max_drawdown": 300, "trades": 10},
            {"params": {"length": 14}, "net_profit": 2450, "max_drawdown": 150, "trades": 5},
        ]
        self.assertLess(next_plan({"length": 10}, climbed)["length"], 14)
        tied = [
            {"params": {"length": 10}, "net_profit": 500, "max_drawdown": 300, "trades": 10},
            {"params": {"length": 14}, "net_profit": 500, "max_drawdown": 300, "trades": 5},
        ]
        self.assertLess(next_plan({"length": 10}, tied)["length"], 14)

    def test_the_impact_and_the_note_follow_the_objective(self):
        from futuresfund.learn import _impact, adjustment_notes

        before = {"params": {"stop": 20}, "net_profit": 3500, "max_drawdown": 900, "trades": 10}
        after = {"params": {"stop": 16}, "net_profit": 3100, "max_drawdown": 450, "trades": 10}
        text = _impact(before, after)
        self.assertIn("did not move the result toward the goal", text)
        self.assertIn("Drawdown moved -450", text)
        self.assertIn("stop 20 to 16", text)
        rich = {"params": {"stop": 30}, "net_profit": 9000, "max_drawdown": 1500, "trades": 12}
        quant, indicator = adjustment_notes([before, after, rich])
        self.assertIn("Best change under the objective", quant)
        self.assertIn("Most profitable change", quant)
        self.assertIn("drawdown", indicator.lower())
        best = quant.split("Best change under the objective")[1].splitlines()[1]
        self.assertIn("stop 20 to 16", best)

    def test_the_guidelines_keep_drawdown_low_once_a_run_is_profitable(self):
        from futuresfund.learn import research_guidelines

        text = research_guidelines()
        self.assertIn("Limit risk", text)
        self.assertIn("Drawdown stays under 5 percent of the account.", text)
        self.assertIn("Until profit is above $3,000, a deeper drawdown is not included.", text)
        self.assertIn("1.5 times the drawdown", text)
        self.assertIn("One trade down more than 3 percent of the account is out", text)
        self.assertIn("3.6 percent", text)
        self.assertIn("The take profit stays larger than the stop", text)
        self.assertNotIn("300,000", text)
        self.assertNotIn("1800-1645:12345", text)
        self.assertEqual(text.count("Limit risk"), 1)


class TradeStatTests(unittest.TestCase):
    def test_sharpe_profit_ratio_and_the_extreme_trades(self):
        from futuresfund.tv_broker import sharpe_ratio, tv_metrics

        trades = [
            {"exit_time": "2024-01-01", "pnl": 300},
            {"exit_time": "2024-01-02", "pnl": -100},
            {"exit_time": "2024-01-03", "pnl": 150},
            {"exit_time": "2024-01-04", "pnl": -50},
        ]
        measured = tv_metrics(trades)
        self.assertEqual(measured["biggest_winner"], 300)
        self.assertEqual(measured["biggest_loser"], -100)
        self.assertEqual(measured["profit_ratio"], 3.0)
        self.assertEqual(measured["sharpe"], sharpe_ratio([300, -100, 150, -50]))
        self.assertGreater(sharpe_ratio([300, -100, 150, -50], 252), measured["sharpe"])


class ScaleTests(unittest.TestCase):
    @mock.patch("futuresfund.objective.scale_goal", return_value="win_rate")
    def test_the_goal_is_one_end_and_a_loss_is_toward_the_other(self, _goal):
        from futuresfund.objective import result_scale, scale_move

        goal = {"net_profit": 4000, "max_drawdown": 2000, "trades": 20, "days": 20}
        loss = {"net_profit": -150000, "max_drawdown": 170000, "trades": 5000, "days": 260}
        smaller = {"net_profit": -120000, "max_drawdown": 170000, "trades": 5000, "days": 260}
        quiet = {"net_profit": 0, "max_drawdown": 0, "trades": 0}
        ratio = {"net_profit": 53895.0, "max_drawdown": 29523.5, "trades": 1335, "days": 260}
        short = {"net_profit": 20000.0, "max_drawdown": 2000.0, "trades": 1335, "days": 260}
        in_band = {"net_profit": 20000.0, "max_drawdown": 2000.0, "trades": 40, "days": 20}
        self.assertLess(result_scale(goal, 50000), 1.0)
        self.assertGreater(result_scale(goal, 50000), 0.5)
        self.assertLess(result_scale(ratio, 50000), 1.0)
        self.assertEqual(result_scale({**goal, "profit_factor": 3.0}, 50000), 1.0)
        self.assertLess(result_scale({**ratio, "profit_factor": 3.4}, 50000), 1.0)
        self.assertLess(result_scale(short, 50000), 1.0)
        self.assertEqual(result_scale(quiet, 50000), 0.0)
        self.assertEqual(result_scale(smaller, 50000), result_scale(loss, 50000))
        self.assertGreater(result_scale({**loss, "max_drawdown": 20000}, 50000), result_scale(loss, 50000))
        self.assertLess(result_scale(smaller, 50000), 1.0)
        self.assertEqual(scale_move(loss, smaller, 50000), "closer")
        self.assertEqual(scale_move(smaller, loss, 50000), "farther")
        self.assertEqual(scale_move(loss, {**loss, "max_drawdown": 20000}, 50000), "closer")
        self.assertEqual(scale_move({**loss, "max_drawdown": 20000}, loss, 50000), "farther")
        self.assertEqual(scale_move(loss, loss, 50000), "flat")
        self.assertLess(result_scale(loss, 50000), 0.5)
        self.assertLess(result_scale(smaller, 50000), 0.5)
        self.assertGreater(result_scale(in_band, 50000), 0.5)
        self.assertLess(result_scale(short, 50000), result_scale(in_band, 50000))

    @mock.patch("futuresfund.objective.scale_goal", return_value="win_rate")
    def test_losses_tick_the_scale_away_from_the_goal_and_profit_ticks_toward_it(self, _goal):
        from futuresfund.learn import next_plan
        from futuresfund.objective import result_scale, walk_scale

        night = [
            {"net_profit": -9845.0, "max_drawdown": 16793.5, "trades": 195, "days": 260},
            {"net_profit": -10120.0, "max_drawdown": 17964.5, "trades": 195, "days": 260},
            {"net_profit": -19670.0, "max_drawdown": 26928.0, "trades": 195, "days": 260},
            {"net_profit": -10120.0, "max_drawdown": 17964.5, "trades": 195, "days": 260},
            {"net_profit": -5882.5, "max_drawdown": 17978.0, "trades": 195, "days": 260},
            {"net_profit": -15432.5, "max_drawdown": 27515.5, "trades": 195, "days": 260},
            {"net_profit": -5882.5, "max_drawdown": 17978.0, "trades": 195, "days": 260},
        ]
        self.assertLess(result_scale(night[0], 50000), 0.5)
        self.assertLess(walk_scale(night[:1], 50000), 0.5)
        self.assertLess(walk_scale(night, 50000), walk_scale(night[:2], 50000))
        self.assertLess(walk_scale(night, 50000), 0.2)

        growing = [
            {"net_profit": 5000.0, "max_drawdown": 2000.0, "trades": 40, "days": 20},
            {"net_profit": 15000.0, "max_drawdown": 2000.0, "trades": 40, "days": 20},
        ]
        self.assertGreater(walk_scale(growing[:1], 50000), 0.5)
        self.assertGreater(walk_scale(growing, 50000), walk_scale(growing[:1], 50000))
        self.assertLess(walk_scale(growing, 50000), 1.0)

        origin = {"stop_dollars": 1000.0, "length": 10}
        stops = [1000.0, 1200.0, 1400.0, 1600.0]
        profits = [-9845.0, -9000.0, -8000.0, -7000.0]
        trials = [
            {"params": {"stop_dollars": stop, "length": 10}, "net_profit": profit, "max_drawdown": 18000.0, "trades": 195, "days": 260}
            for stop, profit in zip(stops, profits)
        ]
        self.assertLess(walk_scale(trials, 50000), 0.2)
        plan = next_plan(origin, trials)
        self.assertEqual(plan["stop_dollars"], 1600.0)
        self.assertNotEqual(plan["length"], 10)

    @mock.patch("futuresfund.objective.scale_goal", return_value="win_rate")
    def test_a_profit_factor_moves_the_scale_toward_3_and_back(self, _goal):
        from futuresfund.objective import result_scale, scale_move, walk_scale

        def row(factor, profit=1000.0):
            return {
                "net_profit": profit,
                "max_drawdown": 2000.0,
                "trades": 40,
                "days": 20,
                "profit_factor": factor,
            }

        low = row(1.0, -2000.0)
        mid = row(2.0, 8000.0)
        goal = row(3.0, 20000.0)
        richer = row(4.5, 30000.0)
        dropped = row(1.5, 4000.0)
        self.assertEqual(result_scale(low, 50000), round(1.0 / 3.0, 4))
        self.assertEqual(result_scale(mid, 50000), round(2.0 / 3.0, 4))
        self.assertEqual(result_scale(goal, 50000), 1.0)
        self.assertEqual(result_scale(richer, 50000), 1.0)
        self.assertGreater(walk_scale([low, mid], 50000), walk_scale([low], 50000))
        self.assertLess(walk_scale([mid, low], 50000), walk_scale([mid], 50000))
        self.assertEqual(walk_scale([low, goal], 50000), 1.0)
        self.assertEqual(walk_scale([goal, richer], 50000), 1.0)
        self.assertAlmostEqual(walk_scale([richer, dropped], 50000), round(1.5 / 3.0, 4))
        self.assertEqual(scale_move(low, mid, 50000), "closer")
        self.assertEqual(scale_move(mid, low, 50000), "farther")
        self.assertEqual(scale_move(goal, richer, 50000), "flat")
        self.assertEqual(scale_move(richer, dropped, 50000), "farther")
        crowded = {**goal, "trades": 2000, "days": 260}
        self.assertLess(result_scale(crowded, 50000), 1.0)
        self.assertEqual(scale_move(goal, crowded, 50000), "farther")
        self.assertEqual(scale_move(crowded, goal, 50000), "closer")

    @mock.patch("futuresfund.objective.scale_goal", return_value="win_rate")
    def test_a_win_rate_of_60_is_the_goal_when_the_profit_factor_is_above_1_1(self, _goal):
        from futuresfund.objective import result_scale, scale_move, trade_loss_ok

        def row(rate, factor, loser=-400.0):
            return {
                "net_profit": 8000.0,
                "max_drawdown": 2000.0,
                "trades": 40,
                "days": 20,
                "win_rate": rate,
                "profit_factor": factor,
                "biggest_loser": loser,
            }

        halfway = row(30, 1.4)
        goal = row(60, 1.2)
        richer = row(72, 1.8)
        thin = row(60, 1.1)
        fraction = row(0.6, 1.2)
        huge = row(70, 2.0, -1800.01)
        with mock.patch("futuresfund.objective.scale_win_rate", return_value=60.0), mock.patch(
            "futuresfund.objective.scale_profit_factor", return_value=1.1
        ):
            self.assertEqual(result_scale(halfway, 50000), 0.5)
            self.assertEqual(result_scale(goal, 50000), 1.0)
            self.assertEqual(result_scale(richer, 50000), 1.0)
            self.assertEqual(result_scale(fraction, 50000), 1.0)
            self.assertLess(result_scale(thin, 50000), 1.0)
            self.assertLessEqual(result_scale(huge, 50000), 0.25)
            self.assertEqual(scale_move(halfway, goal, 50000), "closer")
            self.assertEqual(scale_move(goal, thin, 50000), "farther")
            self.assertEqual(scale_move(goal, huge, 50000), "farther")
            self.assertEqual(scale_move(huge, goal, 50000), "closer")
        with mock.patch("futuresfund.objective.scale_win_rate", return_value=48.0), mock.patch(
            "futuresfund.objective.scale_profit_factor", return_value=1.1
        ):
            self.assertEqual(result_scale(row(24, 1.4), 50000), 0.5)
            self.assertEqual(result_scale(row(48, 1.2), 50000), 1.0)
            self.assertEqual(result_scale(goal, 50000), 1.0)
        self.assertFalse(trade_loss_ok(huge))
        self.assertTrue(trade_loss_ok(row(60, 1.2, -1800.0)))

    def test_the_chosen_acceptance_number_is_the_scale_goal(self):
        import tempfile
        from pathlib import Path

        from futuresfund import book as bookmod
        from futuresfund.book import save_strategy_acceptance, strategy_acceptance
        from futuresfund.llm import _chat_system
        from futuresfund.objective import describe, goal_line, result_scale, scale_move

        original = bookmod.BOOK_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bookmod.BOOK_PATH = Path(tmp) / "book.json"
                boxes = {
                    "use_drawdown": True, "use_trades": True, "use_profit": True,
                    "use_factor": True, "use_win_rate": True,
                }
                save_strategy_acceptance({
                    "min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 60, "goal": "win_rate",
                    **boxes,
                })
                self.assertEqual(strategy_acceptance()["goal"], "win_rate")
                self.assertIn("A win rate of 48 percent. That is the goal on the scale.", goal_line())
                self.assertEqual(result_scale({"net_profit": 8000, "max_drawdown": 1000, "trades": 40, "days": 20, "win_rate": 24, "profit_factor": 1.4}, 50000), 0.5)
                save_strategy_acceptance({
                    "min_profit": 14000, "profit_factor": 1.1, "win_rate": 61, "min_trades": 60, "goal": "win_rate",
                    **boxes,
                })
                self.assertIn("A win rate of 61 percent. That is the goal on the scale.", goal_line())
                self.assertIn("A win rate of 61 percent. That is the goal on the scale.", _chat_system("15 min strategy developer"))

                save_strategy_acceptance({
                    "min_profit": 14000, "profit_factor": 2.2, "win_rate": 48, "min_trades": 60, "goal": "profit_factor",
                    **boxes,
                })
                factor_line = "A profit factor of 2.2. That is the goal on the scale."
                self.assertIn(factor_line, goal_line())
                self.assertIn(factor_line, _chat_system("15 min strategy developer"))
                self.assertIn(factor_line, describe())
                self.assertIn(factor_line, _chat_system("Portfolio Manager"))
                measured = {"net_profit": 8000, "max_drawdown": 1000, "trades": 40, "days": 20, "profit_factor": 1.1, "win_rate": 70}
                cleared = {**measured, "profit_factor": 2.2}
                self.assertEqual(result_scale(measured, 50000), 0.5)
                self.assertEqual(result_scale(cleared, 50000), 1.0)
                self.assertEqual(scale_move(measured, cleared, 50000), "closer")
                self.assertEqual(scale_move(cleared, measured, 50000), "farther")

                save_strategy_acceptance({
                    "min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 60, "goal": "min_profit",
                    **boxes,
                })
                self.assertIn("A profit of $14,000. That is the goal on the scale.", goal_line())
                self.assertIn("A profit of $14,000. That is the goal on the scale.", _chat_system("15 min strategy developer"))
                halfway = {"net_profit": 7000, "max_drawdown": 1000, "trades": 40, "days": 20}
                reached = {"net_profit": 14000, "max_drawdown": 1000, "trades": 40, "days": 20}
                self.assertEqual(result_scale(halfway, 50000), 0.5)
                self.assertEqual(result_scale(reached, 50000), 1.0)
                self.assertLessEqual(result_scale({**reached, "biggest_loser": -1800.01}, 50000), 0.25)

                save_strategy_acceptance({
                    "min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 60, "goal": "min_trades",
                    **boxes,
                })
                self.assertIn("A trade count of 60. That is the goal on the scale.", goal_line())
                self.assertIn("A trade count of 60. That is the goal on the scale.", _chat_system("15 min strategy developer"))
                self.assertEqual(result_scale({"net_profit": 1000, "max_drawdown": 100, "trades": 30, "days": 20}, 50000), 0.5)
                self.assertEqual(result_scale({"net_profit": 1000, "max_drawdown": 100, "trades": 60, "days": 20}, 50000), 1.0)
                with self.assertRaises(ValueError):
                    save_strategy_acceptance({
                        "min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 60, "goal": "sharpe",
                    })
                self.assertEqual(strategy_acceptance()["goal"], "min_trades")
        finally:
            bookmod.BOOK_PATH = original


class ShelfTests(unittest.TestCase):
    def test_the_library_keeps_only_the_most_profitable_run(self):
        from futuresfund.library import _library_rows

        rows = [
            {"id": "a", "contract": "ES1!", "title": "Pullback", "script": "Pullback", "net_profit": 32000, "max_drawdown": 900, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2},
            {"id": "b", "contract": "ES1!", "title": "Pullback", "script": "Pullback", "net_profit": 3200, "max_drawdown": 350, "trades": 12, "days": 20, "profit_ratio": 1.2},
            {"id": "c", "contract": "ES1!", "title": "Pullback", "script": "Pullback", "net_profit": 20000, "max_drawdown": 50, "trades": 3, "days": 20, "profit_ratio": 1.2},
            {"id": "d", "contract": "ES1!", "title": "Breakout", "script": "Breakout", "net_profit": 25000, "max_drawdown": 400, "trades": 120, "days": 260, "win_rate": 62, "profit_factor": 1.3, "profit_ratio": 1.2},
            {"id": "e", "contract": "ES1!", "title": "Loss", "script": "Loss", "net_profit": -100, "max_drawdown": 20, "trades": 20, "days": 20, "profit_ratio": 1.2},
        ]
        shown = _library_rows(rows)
        self.assertEqual([row["id"] for row in shown], ["a", "d"])
        self.assertEqual(shown[0]["kind"], "Most profitable")
        self.assertTrue(all(row["primary"] for row in shown))

    def test_a_run_outside_the_pace_or_past_the_drawdown_leaves_the_library(self):
        from futuresfund.library import _library_rows

        rows = [
            {"id": "a", "contract": "ES1!", "title": "Pullback", "net_profit": 25000, "max_drawdown": 900, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2},
            {"id": "b", "contract": "ES1!", "title": "Pullback", "net_profit": 3200, "max_drawdown": 350, "trades": 12, "days": 20, "profit_ratio": 1.2},
            {"id": "c", "contract": "ES1!", "title": "Pullback", "net_profit": 2000, "max_drawdown": 2600, "trades": 40, "days": 20, "profit_ratio": 1.2},  # a series of losses goes lower than $2,500 before $300,000
            {"id": "d", "contract": "ES1!", "title": "Breakout", "net_profit": 21000, "max_drawdown": 400, "trades": 120, "days": 260, "win_rate": 61, "profit_factor": 1.2, "profit_ratio": 1.2},
            {"id": "e", "contract": "ES1!", "title": "Held", "net_profit": 118180, "max_drawdown": 11100, "trades": 2, "days": 1291, "profit_ratio": 1.2},
            {"id": "f", "contract": "NQ1!", "title": "Held", "net_profit": 22000, "max_drawdown": 500, "trades": 140, "days": 260, "win_rate": 62, "profit_factor": 1.3, "profit_ratio": 1.2},
            {"id": "h", "contract": "ES1!", "title": "Fast", "net_profit": 80000, "max_drawdown": 1000, "trades": 4000, "days": 260, "profit_ratio": 1.2},
        ]
        shown = _library_rows(rows)
        self.assertEqual([row["id"] for row in shown], ["a", "f", "d"])
        self.assertTrue(all(row["kind"] == "Most profitable" for row in shown))

    def test_a_profit_near_50000_stays_ahead_of_a_larger_profit_with_a_worse_drawdown(self):
        from futuresfund.library import _library_rows
        from futuresfund.objective import firm_ratio

        near = {"id": "near", "contract": "ES1!", "title": "Drift", "script": "Drift", "net_profit": 50000, "max_drawdown": 22000, "trades": 20, "days": 20, "profit_ratio": 1.2}
        richer = {"id": "rich", "contract": "ES1!", "title": "Drift", "script": "Drift", "net_profit": 90000, "max_drawdown": 70000, "trades": 20, "days": 20, "profit_ratio": 1.2}
        lower = {"id": "low", "contract": "ES1!", "title": "Drift", "script": "Drift", "net_profit": 40000, "max_drawdown": 22000, "trades": 20, "days": 20, "profit_ratio": 1.2}
        better = {"id": "better", "contract": "GC1!", "title": "Gold", "script": "Gold", "net_profit": 80000, "max_drawdown": 22000, "trades": 20, "days": 20, "profit_ratio": 1.2}
        short = {"id": "short", "contract": "NQ1!", "title": "Short", "script": "Short", "net_profit": 39000, "max_drawdown": 22000, "trades": 20, "days": 20, "profit_ratio": 1.2}
        self.assertTrue(firm_ratio(near))
        self.assertTrue(firm_ratio(lower))
        self.assertTrue(firm_ratio(better))
        self.assertFalse(firm_ratio(richer))
        self.assertFalse(firm_ratio(short))
        self.assertTrue(firm_ratio({"net_profit": 50000, "max_drawdown": 27500, "trades": 20, "days": 20}))
        self.assertTrue(firm_ratio({"net_profit": 80000, "max_drawdown": 44000, "trades": 20, "days": 20}))
        self.assertFalse(firm_ratio({"net_profit": 80000, "max_drawdown": 44001, "trades": 20, "days": 20}))
        self.assertFalse(firm_ratio({"net_profit": 50000, "max_drawdown": 30000, "trades": 20, "days": 20}))
        alive = {"id": "alive", "contract": "ES1!", "title": "Alive", "script": "Alive", "net_profit": 50000, "max_drawdown": 1500, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2}
        shown = _library_rows([near, richer, lower, better, short, alive])
        self.assertEqual([row["id"] for row in shown], ["alive"])

    def test_the_shelf_keeps_one_version_over_20000(self):
        from futuresfund.library import shelf_rows

        rows = [
            {"id": "ema", "script": "EMA SMA Pivot Filter", "net_profit": 152130.0, "max_drawdown": 2000.0, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2},
            {"id": "ema-small", "script": "EMA SMA Pivot Filter", "net_profit": 37.5, "max_drawdown": 837.5, "profit_ratio": 1.2},
            {"id": "pivot-v4", "script": "Engine4_NewPivotTrend_v4", "net_profit": 83200.0, "max_drawdown": 1800.0, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2},
            {"id": "pivot-v11", "script": "Engine4_NewPivotTrend_v11", "net_profit": 39787.5, "max_drawdown": 8912.5, "profit_ratio": 1.2},
            {"id": "shift", "script": "POC RSI Momentum Shift v1", "net_profit": 27450.0, "max_drawdown": 2000.0, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2},
            {"id": "rsi", "script": "POC RSI Momentum v1", "net_profit": 66200.0, "max_drawdown": 2000.0, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2},
            {"id": "rsi-v3", "script": "POC RSI Momentum v3", "net_profit": 20437.5, "max_drawdown": 14250.0, "profit_ratio": 1.12},
            {"id": "ten", "script": "Ten Thousand Band", "net_profit": 12000.0, "max_drawdown": 4000.0, "trades": 200, "days": 260, "profit_ratio": 1.2},
            {"id": "fast", "script": "Fast Scalp", "net_profit": 80000.0, "max_drawdown": 4000.0, "trades": 4000, "days": 260, "profit_ratio": 1.2},
            {"id": "quiet", "script": "RSI MACD Reversal v5", "net_profit": 5037.5, "max_drawdown": 5050.0, "profit_ratio": 1.2},
        ]
        shown = shelf_rows(rows)
        self.assertEqual([row["id"] for row in shown], ["ema", "pivot-v4", "rsi", "shift"])

    def test_a_single_trade_loss_over_1800_is_left_off_the_shelf(self):
        from futuresfund.library import shelf_rows

        kept = {
            "id": "kept", "script": "Steady", "net_profit": 22000.0, "max_drawdown": 2000.0,
            "trades": 200, "days": 260, "profit_ratio": 1.2, "profit_factor": 1.2, "win_rate": 65, "biggest_loser": -1800.0,
        }
        huge = {
            "id": "huge", "script": "Blowup", "net_profit": 40000.0, "max_drawdown": 2000.0,
            "trades": 200, "days": 260, "profit_ratio": 1.4, "profit_factor": 1.4, "win_rate": 70, "biggest_loser": -1800.01,
        }
        shown = shelf_rows([kept, huge])
        self.assertEqual([row["id"] for row in shown], ["kept"])

    def test_the_library_needs_profit_a_factor_a_win_rate_and_a_hundred_trades(self):
        import tempfile
        from pathlib import Path

        from futuresfund import book as bookmod
        from futuresfund.book import save_strategy_acceptance
        from futuresfund.library import library_ready, shelf_rows

        ready = {
            "id": "ready", "script": "Ready", "net_profit": 25000.0, "max_drawdown": 800.0,
            "trades": 120, "days": 260, "win_rate": 65, "profit_factor": 1.2, "profit_ratio": 1.2,
        }
        original = bookmod.BOOK_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bookmod.BOOK_PATH = Path(tmp) / "book.json"
                self.assertTrue(library_ready(ready))
                self.assertTrue(library_ready({**ready, "net_profit": 20000.0}))
                self.assertFalse(library_ready({**ready, "max_drawdown": 2500.0}))
                self.assertFalse(library_ready({**ready, "trades": 99}))
                save_strategy_acceptance({
                    "min_profit": 20000, "profit_factor": 1.1, "win_rate": 53, "min_trades": 100,
                    "drawdown_pct": 5,
                    "use_drawdown": True, "use_trades": True, "use_profit": True,
                    "use_factor": True, "use_win_rate": True,
                })
                self.assertTrue(library_ready(ready))
                self.assertFalse(library_ready({**ready, "net_profit": 20000.0}))
                self.assertFalse(library_ready({**ready, "profit_factor": 1.1}))
                self.assertFalse(library_ready({**ready, "win_rate": 53}))
                self.assertTrue(library_ready({**ready, "win_rate": 53.1}))
                self.assertFalse(library_ready({**ready, "trades": 99}))
                self.assertEqual([row["id"] for row in shelf_rows([
                    ready,
                    {**ready, "id": "thin", "script": "Thin", "profit_factor": 1.1},
                    {**ready, "id": "even", "script": "Even", "win_rate": 53},
                    {**ready, "id": "small", "script": "Small", "net_profit": 19999.0},
                    {**ready, "id": "few", "script": "Few", "trades": 99},
                ])], ["ready"])
        finally:
            bookmod.BOOK_PATH = original

    def test_the_saved_win_rate_is_the_acceptance_line(self):
        import tempfile
        from pathlib import Path

        from futuresfund import book as bookmod
        from futuresfund.book import save_strategy_acceptance
        from futuresfund.library import library_ready

        original = bookmod.BOOK_PATH
        row = {
            "net_profit": 25000.0, "max_drawdown": 800.0, "trades": 120, "days": 260,
            "win_rate": 54, "profit_factor": 1.2, "profit_ratio": 1.2,
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bookmod.BOOK_PATH = Path(tmp) / "book.json"
                save_strategy_acceptance({
                    "min_profit": 20000, "profit_factor": 1.1, "win_rate": 53, "min_trades": 100,
                    "use_drawdown": True, "use_trades": True, "use_profit": True,
                    "use_factor": True, "use_win_rate": True,
                })
                self.assertTrue(library_ready(row))
                self.assertFalse(library_ready({**row, "win_rate": 53}))
                save_strategy_acceptance({
                    "min_profit": 20000, "profit_factor": 1.1, "win_rate": 70, "min_trades": 100,
                    "use_drawdown": True, "use_trades": True, "use_profit": True,
                    "use_factor": True, "use_win_rate": True,
                })
                self.assertFalse(library_ready(row))
        finally:
            bookmod.BOOK_PATH = original

    def test_a_saved_acceptance_line_reaches_the_guide_the_agents_and_the_email(self):
        import tempfile
        from pathlib import Path

        from futuresfund import book as bookmod
        from futuresfund.book import firm_record, save_strategy_acceptance
        from futuresfund.learn import _result_facts
        from futuresfund.library import library_ready
        from futuresfund.llm import _chat_system
        from futuresfund.objective import acceptance_line, describe

        original = bookmod.BOOK_PATH
        row = {
            "net_profit": 15000.0, "max_drawdown": 800.0, "trades": 70, "days": 260,
            "win_rate": 55.0, "profit_factor": 1.3, "profit_ratio": 1.2,
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bookmod.BOOK_PATH = Path(tmp) / "book.json"
                save_strategy_acceptance({
                    "min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 60,
                    "use_drawdown": True, "use_trades": True, "use_profit": True,
                    "use_factor": True, "use_win_rate": True,
                })
                for text in (
                    acceptance_line(),
                    _chat_system("Chart researcher"),
                    describe(),
                    firm_record(),
                    _result_facts({"title": "Sample"}, Path("sample.pine"), row, "ES", 1),
                ):
                    self.assertIn("above $14,000", text)
                    self.assertIn("above 1.1", text)
                    self.assertIn("above 48 percent", text)
                    self.assertIn("at least 60 trades", text)
                self.assertTrue(library_ready(row))
                self.assertFalse(library_ready({**row, "trades": 59}))
                save_strategy_acceptance({
                    "min_profit": 20000, "profit_factor": 1.4, "win_rate": 70, "min_trades": 80,
                    "use_drawdown": True, "use_trades": True, "use_profit": True,
                    "use_factor": True, "use_win_rate": True,
                })
                self.assertIn("above $20,000", _chat_system("Chart researcher"))
                self.assertIn("above 1.4", describe())
                self.assertIn("above 70 percent", firm_record())
                self.assertIn("at least 80 trades", acceptance_line())
                self.assertFalse(library_ready(row))
        finally:
            bookmod.BOOK_PATH = original

    def test_the_same_name_stays_on_each_chart_and_three_a_week_files_when_the_factor_is_strong(self):
        from futuresfund.library import shelf_rows
        from futuresfund.objective import library_pace

        nasdaq_15 = {
            "id": "nq-15", "script": "Engine4_NewPivotTrend", "contract": "NQ1!", "timeframe": "15m",
            "net_profit": 44445.0, "max_drawdown": 2000.0, "trades": 488, "days": 260, "win_rate": 65, "profit_factor": 1.386, "profit_ratio": 1.2,
        }
        nasdaq_2 = {
            "id": "nq-2", "script": "Engine4_NewPivotTrend_v10", "contract": "NQ1!", "timeframe": "2m",
            "net_profit": 109605.0, "max_drawdown": 2200.0, "trades": 583, "days": 260, "win_rate": 65, "profit_factor": 1.5, "profit_ratio": 1.2,
        }
        smaller = {
            "id": "nq-2-small", "script": "Engine4_NewPivotTrend_v4", "contract": "NQ1!", "timeframe": "2m",
            "net_profit": 50000.0, "max_drawdown": 12000.0,
        }
        shown = shelf_rows([nasdaq_15, nasdaq_2, smaller])
        self.assertEqual({row["id"] for row in shown}, {"nq-15", "nq-2"})
        week = {"net_profit": 25000.0, "max_drawdown": 12000.0, "trades": 156, "days": 260, "profit_factor": 1.4}
        strong = {**week, "trades": 156, "profit_factor": 3.1}
        crowded = {**week, "trades": 2000, "profit_factor": 3.1}
        three_trades = {"net_profit": 45000.0, "max_drawdown": 16000.0, "trades": 3, "days": 5, "profit_factor": 3.2}
        two_trades = {"net_profit": 45000.0, "max_drawdown": 16000.0, "trades": 2, "days": 5, "profit_factor": 4.0}
        self.assertTrue(library_pace(nasdaq_15))
        self.assertTrue(library_pace(week))
        self.assertTrue(library_pace(strong))
        self.assertFalse(library_pace(crowded))
        self.assertTrue(library_pace(three_trades))
        self.assertFalse(library_pace(two_trades))

    def test_the_stand_in_is_the_lowest_drawdown_rule_that_hit_the_target(self):
        from futuresfund.discuss import choose_standin

        approved = [
            {"id": "rich", "backtest": {"net_profit": 6000, "max_drawdown": 1200, "trades": 20}},
            {"id": "steady", "backtest": {"net_profit": 3100, "max_drawdown": 450, "trades": 11}},
            {"id": "short", "backtest": {"net_profit": 900, "max_drawdown": 60, "trades": 14}},
        ]
        self.assertEqual(choose_standin(approved, False)["id"], "steady")
        self.assertIsNone(choose_standin(approved, True))


class LeaderTests(unittest.TestCase):
    def _note(self, folder: Path, name: str, best: dict, charts: dict | None = None) -> Path:
        payload = {"file": f"{name}.pine", "title": name.replace("_", " ").title(), "best": best}
        if charts is not None:
            payload["instruments"] = {"ES": {"best": best, "charts": {tf: {"best": row} for tf, row in charts.items()}}}
        path = folder / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_leader_hit_the_target_inside_the_drawdown_at_pace(self):
        from futuresfund.strategy import _leader_row

        base = {"runner": "backtrader", "engine": "backtrader", "timeframe": "5m", "days": 20}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            steady = self._note(
                folder, "steady_pullback", {**base, "net_profit": 3200, "max_drawdown": 400, "trades": 18},
                charts={"5m": {"net_profit": 3200, "max_drawdown": 400, "trades": 18}, "2m": {"net_profit": -300, "max_drawdown": 500, "trades": 7}},
            )
            short = self._note(folder, "short_of_target", {**base, "net_profit": 2900, "max_drawdown": 90, "trades": 30})
            few = self._note(folder, "few_trades", {**base, "net_profit": 17378, "max_drawdown": 111, "trades": 2})
            busy = self._note(folder, "busy", {**base, "net_profit": 3900, "max_drawdown": 200, "trades": 90})
            flat = self._note(folder, "flat", {**base, "net_profit": 3900, "max_drawdown": 20, "trades": 0})
            wide = self._note(folder, "wide", {**base, "net_profit": 9000, "max_drawdown": 2600, "trades": 40})
            deep = self._note(folder, "deep", {**base, "net_profit": 9000, "max_drawdown": 35000.01, "trades": 40})
            learned = self._note(folder, "year_band", {**base, "net_profit": 53895.0, "max_drawdown": 29523.5, "trades": 400, "days": 260})
            crowded = self._note(folder, "crowded", {**base, "net_profit": 53895.0, "max_drawdown": 29523.5, "trades": 1335, "days": 260})
            listed = folder / "creation_lessons.json"
            listed.write_text(json.dumps([{"name": "x"}]), encoding="utf-8")
            row = _leader_row(steady)
            self.assertIsNotNone(row)
            self.assertEqual(row["green"], 1)
            self.assertEqual(row["charts"], "1 of 2 charts finished green")
            self.assertEqual(row["trades_per_day"], 0.9)
            self.assertIsNone(_leader_row(short))
            self.assertIsNone(_leader_row(few))
            self.assertIsNone(_leader_row(busy))
            self.assertIsNone(_leader_row(flat))
            self.assertIsNotNone(_leader_row(wide))
            self.assertIsNotNone(_leader_row(learned))
            self.assertIsNone(_leader_row(crowded))
            self.assertIsNone(_leader_row(deep))
            self.assertIsNone(_leader_row(listed))

    def test_the_rerank_moves_a_study_best_to_the_profitable_run_with_the_lower_drawdown(self):
        from futuresfund.learn import _rerank_study

        trials = [
            {"net_profit": 124, "max_drawdown": 98, "trades": 9, "days": 20},
            {"net_profit": 4100, "max_drawdown": 150, "trades": 2, "days": 20},
            {"net_profit": 5200, "max_drawdown": 2300, "trades": 18, "days": 20},
        ]
        data = {"best": trials[1], "instruments": {"QO": {"best": trials[1], "charts": {"15m": {"best": trials[1], "trials": trials}}}}}
        moved = _rerank_study(data)
        self.assertGreater(moved, 0)
        self.assertEqual(data["best"]["net_profit"], 5200)
        self.assertEqual(data["instruments"]["QO"]["charts"]["15m"]["best"]["net_profit"], 5200)

    def test_the_rerank_keeps_the_lower_drawdown_when_older_rows_omit_the_chart(self):
        from futuresfund.learn import _rerank_study

        trials = [
            {"net_profit": 4100, "max_drawdown": 150, "trades": 2},
            {"net_profit": 3300, "max_drawdown": 900, "trades": 30},
        ]
        data = {"best": trials[0], "instruments": {"QO": {"best": trials[0], "charts": {"15m": {"best": trials[0], "trials": trials}}}}}
        _rerank_study(data)
        self.assertEqual(data["best"]["net_profit"], 3300)


class CreationTests(unittest.TestCase):
    def test_exemplars_hit_the_target_and_are_ordered_by_the_lowest_drawdown(self):
        from futuresfund.exemplars import exemplars

        base = {"runner": "backtrader", "engine": "backtrader", "timeframe": "5m", "days": 20}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name, profit, drawdown, trades in (("rich", 6000, 800, 18), ("steady", 3300, 300, 16), ("short", 900, 50, 16), ("few", 4000, 30, 2)):
                (folder / f"{name}.json").write_text(json.dumps({
                    "file": f"{name}.pine", "title": name,
                    "best": {**base, "net_profit": profit, "max_drawdown": drawdown, "trades": trades},
                    "instruments": {"ES": {"charts": {"5m": {"best": {**base, "net_profit": profit, "max_drawdown": drawdown, "trades": trades}}}}},
                }), encoding="utf-8")
            rows = exemplars(5, folder)
        self.assertEqual([row["title"] for row in rows], ["steady", "rich"])

    def test_the_creation_target_is_the_headquarters_target_inside_the_headquarters_drawdown_at_pace(self):
        from futuresfund.exemplars import drawdown_cap, meets_target, profit_target, target_text
        from futuresfund.objective import research_drawdown_cap
        from futuresfund.objective import profit_target as hq_target

        self.assertEqual(drawdown_cap(), research_drawdown_cap(50000))
        self.assertEqual(profit_target(), hq_target())
        self.assertTrue(meets_target({"net_profit": hq_target(), "max_drawdown": drawdown_cap(), "trades": 18, "days": 20}))
        self.assertFalse(meets_target({"net_profit": hq_target(), "max_drawdown": drawdown_cap() + 1, "trades": 18, "days": 20}))
        self.assertFalse(meets_target({"net_profit": hq_target(), "max_drawdown": 10, "trades": 8, "days": 20}))
        self.assertFalse(meets_target({"net_profit": hq_target() - 1, "max_drawdown": 10, "trades": 40, "days": 20}))
        text = target_text()
        self.assertIn("profit target $3,000", text)
        self.assertIn("one-year drawdown no higher than $35,000", text)
        self.assertIn("set on headquarters", text)
        self.assertIn("130 to 600 trades a year", text)
        self.assertIn("Trade count only breaks a tie", text)

    def test_the_checker_runs_the_script_on_real_bars_before_engine_four(self):
        from futuresfund.strategy_team import pine_problems, runtime_problems

        bars = []
        price = 5000.0
        for index in range(120):
            price += 1.5 if index % 7 else -4.0
            bars.append({"t": 1_750_000_000 + index * 300, "o": price, "h": price + 2, "l": price - 2, "c": price + 0.5, "v": 100})
        broken = (
            "//@version=5\nstrategy(\"Broken Session\", overlay=true)\n"
            "sessionStart = input.session(\"0930-1600\", title=\"Session\")\nlen = input.int(20, minval=5, maxval=100)\n"
            "fast = ta.ema(close, len)\n"
            "if time >= sessionStart and ta.crossover(close, fast)\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 5, limit=close + 10)\n"
            "inSession = not na(time(timeframe.period, \"1800-1645:12345\", \"America/New_York\"))\n"
            "if not inSession\n    strategy.close_all(comment=\"Session\")\n"
        )
        good = (
            "//@version=5\nstrategy(\"Clean Session\", overlay=true)\nlen = input.int(20, minval=5, maxval=100)\n"
            "fast = ta.ema(close, len)\ninSession = not na(time(timeframe.period, \"0930-1600\", \"America/New_York\"))\n"
            "if inSession and ta.crossover(close, fast)\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 5, limit=close + 10)\n"
            "flatSession = not na(time(timeframe.period, \"1800-1645:12345\", \"America/New_York\"))\n"
            "if not flatSession\n    strategy.close_all(comment=\"Session\")\n"
        )
        self.assertEqual(pine_problems(broken), [])
        problems = runtime_problems(broken, bars)
        self.assertEqual(len(problems), 1)
        self.assertIn("dry run", problems[0])
        self.assertEqual(runtime_problems(good, bars), [])
        self.assertEqual(runtime_problems(good, bars[:10]), [])

    def test_promotion_needs_the_drawdown_inside_twenty_percent_on_every_chart(self):
        from futuresfund.objective import research_drawdown_cap
        from futuresfund.strategy_team import _chart_inside

        cap = research_drawdown_cap(50000)
        self.assertTrue(_chart_inside({"net_profit": -120, "max_drawdown": 400, "trades": 6, "timeframe": "2m"}))
        self.assertTrue(_chart_inside({"net_profit": 3000, "max_drawdown": cap, "trades": 2, "timeframe": "2m"}))
        self.assertFalse(_chart_inside({"net_profit": 3000, "max_drawdown": cap + 1, "trades": 30, "timeframe": "2m"}))
        self.assertFalse(_chart_inside({"net_profit": None, "max_drawdown": None, "trades": 0, "error": "stopped"}))


class LossCapTests(unittest.TestCase):
    def test_a_loss_past_1500_is_rejected_and_900_trades_cannot_be_saved(self):
        from datetime import datetime

        from futuresfund.objective import consistent_spans, too_many_to_save, trade_loss_ok, trade_spans

        self.assertTrue(trade_loss_ok({"biggest_loser": -1500.0}))
        self.assertFalse(trade_loss_ok({"biggest_loser": -1500.01}))
        self.assertFalse(too_many_to_save({"trades": 900}))
        self.assertTrue(too_many_to_save({"trades": 901}))
        end = datetime(2026, 10, 15, 16, 0)
        trades = [
            {"exit_time": "2026-10-14 10:00", "pnl": 100.0},
            {"exit_time": "2026-09-20 10:00", "pnl": 50.0},
            {"exit_time": "2026-08-01 10:00", "pnl": 40.0},
            {"exit_time": "2026-01-15 10:00", "pnl": 30.0},
        ]
        self.assertTrue(consistent_spans(trade_spans(trades, end)))
        losing_week = trades + [{"exit_time": "2026-10-15 09:00", "pnl": -200.0}]
        self.assertFalse(consistent_spans(trade_spans(losing_week, end)))


if __name__ == "__main__":
    unittest.main()
