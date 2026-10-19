"""The learning queue adjusts parameters, writes the chief executive report, and does not email without a password."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class CandidateTests(unittest.TestCase):
    def test_adjustments_keep_the_original_and_stop_at_two_hundred(self):
        from futuresfund.learn import candidates

        base = {f"length_{index}": 10 + index for index in range(40)}
        base["point_value"] = 50
        plans = candidates(base, limit=200)
        self.assertLessEqual(len(plans), 200)
        self.assertEqual(plans[0]["length_0"], 10)
        self.assertTrue(all(plan["point_value"] == 50 for plan in plans))
        self.assertGreater(len(plans), 1)

    def test_a_passing_result_does_not_stop_the_remaining_attempts(self):
        from futuresfund.learn import study_parameters

        def execute(params):
            return {"net_profit": 3000 + params["length"], "max_drawdown": 400, "trades": 5, "win_rate": 40}

        trials = study_parameters({"length": 10}, execute, limit=15)
        self.assertGreater(len(trials), 1)
        self.assertTrue(all(row["passed"] for row in trials))

    def test_the_researcher_note_is_a_table_with_a_winner_and_a_loser(self):
        from futuresfund.learn import adjustment_notes

        origin = {"params": {"length": 10}, "net_profit": -100, "max_drawdown": 200, "trades": 4, "passed": False}
        winner = {"params": {"length": 14}, "net_profit": 50, "max_drawdown": 80, "trades": 6, "passed": True}
        loser = {"params": {"length": 20}, "net_profit": -400, "max_drawdown": 500, "trades": 9, "passed": False}
        quant, _indicator = adjustment_notes([origin, winner, loser])
        self.assertIn("Most profitable change", quant)
        self.assertIn("A change that made money", quant)
        self.assertIn("A change that lost money", quant)
        self.assertIn("length 10 to 14", quant)
        self.assertIn("$50.00", quant)
        self.assertIn("-$400.00", quant)

    def test_research_guidelines_say_what_we_want(self):
        from futuresfund.learn import research_guidelines

        text = research_guidelines()
        self.assertIn("Limit risk", text)
        self.assertIn("Then raise profit", text)
        self.assertNotIn("What we want", text)
        self.assertNotIn("What we do not want", text)
        self.assertNotIn("What to change", text)
        self.assertIn("Change one input at a time.", text)
        self.assertIn("Every trade has a dollar stop.", text)
        self.assertIn("No ATR stop.", text)
        self.assertIn("The take profit stays larger than the stop", text)
        self.assertNotIn("Keep a change", text)
        self.assertNotIn("Drop a change", text)
        self.assertEqual(text.count("Limit risk"), 1)
        self.assertEqual(text.count("Then raise profit"), 1)

    def test_each_attempt_changes_one_input(self):
        from futuresfund.learn import next_plan

        origin = {"fast": 10, "slow": 20, "zone": 1.5, "use_session": True}
        trials = [{"params": dict(origin), "net_profit": 10, "max_drawdown": 5, "trades": 2}]
        plan = next_plan(origin, trials)
        changed = [key for key in origin if plan[key] != origin[key]]
        self.assertEqual(len(changed), 1)

    def test_a_steadier_change_continues_the_same_variable(self):
        from futuresfund.learn import next_plan

        trials = [
            {"params": {"length": 10, "zone": 1.5}, "net_profit": 10, "max_drawdown": 20, "trades": 4},
            {"params": {"length": 14, "zone": 1.5}, "net_profit": 40, "max_drawdown": 18, "trades": 4},
        ]
        plan = next_plan({"length": 10, "zone": 1.5}, trials)
        self.assertEqual(plan["length"], 14)
        self.assertEqual(plan["zone"], 3.0)

    def test_a_profit_gain_with_a_higher_drawdown_changes_a_different_input(self):
        from futuresfund.learn import next_plan

        trials = [
            {"params": {"length": 10, "stop_pts": 20}, "net_profit": 100, "max_drawdown": 50, "trades": 20},
            {"params": {"length": 14, "stop_pts": 20}, "net_profit": 180, "max_drawdown": 90, "trades": 20},
        ]
        plan = next_plan({"length": 10, "stop_pts": 20}, trials)
        self.assertEqual(plan["length"], 14)
        self.assertEqual(plan["stop_pts"], 40)

    def test_a_step_away_from_the_goal_reverses_that_input(self):
        from futuresfund.learn import next_plan

        trials = [
            {"params": {"length": 10, "stop_pts": 20}, "net_profit": 1000, "max_drawdown": 200, "trades": 20},
            {"params": {"length": 14, "stop_pts": 20}, "net_profit": 970, "max_drawdown": 200, "trades": 20},
        ]
        plan = next_plan({"length": 10, "stop_pts": 20}, trials)
        self.assertEqual(plan["length"], 10)
        self.assertEqual(plan["stop_pts"], 40)

    def test_a_listed_choice_is_an_input_the_search_can_change(self):
        from futuresfund.learn import next_plan

        origin = {"stop_mode": "Fixed $", "length": 10}
        trials = [{"params": dict(origin), "net_profit": 10, "max_drawdown": 5, "trades": 4}]
        plan = next_plan(origin, trials, choices={"stop_mode": ["Fixed $", "ATR", "Entry Candle"]})
        self.assertEqual(plan["length"], 10)
        self.assertNotEqual(plan["stop_mode"], "Fixed $")

    def test_a_literal_stop_and_target_become_inputs(self):
        from futuresfund.learn import promote_fixed_exits

        source = "\n".join([
            "//@version=5",
            'strategy("Example", overlay=true)',
            'strategy.exit("XL", "Long", stop=close - 200, limit=close + 300)',
            'strategy.exit("XS", "Short", stop=close + 200, limit=close - 300)',
        ])
        fixed = promote_fixed_exits(source)
        self.assertIn('stop_points = input.float(200', fixed)
        self.assertIn('take_profit_points = input.float(300', fixed)
        self.assertIn("stop=close - stop_points", fixed)
        self.assertIn("limit=close - take_profit_points", fixed)
        self.assertEqual(promote_fixed_exits(fixed), fixed)

    def test_a_search_does_not_email_until_the_chart_finishes(self):
        import threading
        from unittest import mock

        from futuresfund.learn import _search

        sent = []

        class Board:
            cancel = threading.Event()

            def post(self, *args, **kwargs):
                return None

            def log(self, *args, **kwargs):
                return None

        def execute(params):
            return {"net_profit": float(params["length"]), "max_drawdown": 100, "trades": 20, "days": 20}

        with mock.patch("futuresfund.learn._send", lambda board, title, report: sent.append(title)):
            trials = _search(
                {"length": 10},
                execute,
                50000,
                12,
                Board(),
                None,
                None,
                {"title": "Sample"},
                extras=False,
            )
        self.assertEqual(len(trials), 12)
        self.assertEqual(sent, [])

    def test_the_search_finishes_its_attempts_after_profitable_runs(self):
        from futuresfund.learn import SUCCESS_COUNT, _search

        def execute(params):
            return {"net_profit": 500.0 + float(params["length"]), "max_drawdown": 1000.0, "trades": 20, "days": 20}

        trials = _search({"length": 10}, execute, 50000, 40, None, None, None, extras=False)
        self.assertGreater(len(trials), SUCCESS_COUNT)
        self.assertFalse(getattr(trials, "success_stop", False))

    def test_a_profit_at_least_1_5_times_the_drawdown_does_not_end_the_attempts(self):
        from futuresfund.learn import SUCCESS_COUNT, _search

        def execute(params):
            return {
                "net_profit": 53895.0 + abs(float(params["length"])),
                "max_drawdown": 29523.5,
                "trades": 400,
                "days": 260,
            }

        trials = _search({"length": 10}, execute, 50000, 40, None, None, None, extras=False)
        self.assertGreater(len(trials), SUCCESS_COUNT)
        self.assertFalse(getattr(trials, "success_stop", False))

    def test_a_profitable_run_under_a_1_5_ratio_does_not_count_when_drawdown_is_large(self):
        from futuresfund.learn import _search

        def execute(params):
            profit = 20000.0 + abs(float(params["length"]))
            return {"net_profit": profit, "max_drawdown": profit / 1.2, "trades": 400, "days": 260}

        trials = _search({"length": 10}, execute, 50000, 3, None, None, None, extras=False)
        self.assertFalse(getattr(trials, "success_stop", False))
        self.assertLessEqual(len(trials), 3)

    def test_losses_do_not_count_toward_the_ten_successes(self):
        from futuresfund.learn import _search

        def execute(params):
            return {"net_profit": -50.0 - float(params["length"]), "max_drawdown": 100.0, "trades": 20, "days": 20}

        trials = _search({"length": 10}, execute, 50000, 4, None, None, None, extras=False)
        self.assertEqual(len(trials), 4)
        self.assertFalse(getattr(trials, "success_stop", False))

    def test_a_script_with_more_than_900_trades_is_skipped(self):
        from futuresfund.learn import _search

        def execute(params):
            return {"net_profit": 100.0, "max_drawdown": 10.0, "trades": 901, "days": 20}

        trials = _search({"length": 10}, execute, 50000, 4, None, None, None, extras=False)
        self.assertTrue(getattr(trials, "skipped", False))
        self.assertEqual(len(trials), 0)

    def test_the_stop_keeps_moving_while_it_approaches_the_goal(self):
        from futuresfund.learn import next_plan
        from futuresfund.objective import scale_move

        origin = {"stop_dollars": 500.0, "length": 10}
        first_row = {"params": dict(origin), "net_profit": -149580.0, "max_drawdown": 185091.0, "trades": 5640, "days": 260}
        wider_row = {"params": {"stop_dollars": 1000.0, "length": 10}, "net_profit": -120000.0, "max_drawdown": 160000.0, "trades": 5400, "days": 260}
        first = next_plan(origin, [first_row])
        self.assertEqual(first["stop_dollars"], 750.0)
        self.assertEqual(first["length"], 10)
        self.assertEqual(scale_move(first_row, wider_row, 50000), "closer")
        further = next_plan(origin, [first_row, wider_row])
        self.assertGreater(further["stop_dollars"], 1000.0)
        self.assertEqual(further["length"], 10)

    def test_a_wider_stop_that_loses_more_than_1800_on_one_trade_is_reversed(self):
        from futuresfund.learn import next_plan

        origin = {"stop_mult": 3.125, "length": 10}
        plan = next_plan(origin, [
            {
                "params": {"stop_mult": 3.125, "length": 10},
                "net_profit": 77480.0, "max_drawdown": 22845.0, "trades": 148, "days": 260,
                "biggest_loser": -8195.0,
            },
            {
                "params": {"stop_mult": 3.75, "length": 10},
                "net_profit": 89430.0, "max_drawdown": 29725.0, "trades": 148, "days": 260,
                "biggest_loser": -10235.0,
            },
        ])
        self.assertLess(plan["stop_mult"], 3.75)
        self.assertEqual(plan["length"], 10)

    def test_holding_the_strategy_team_does_not_stop_a_chart_backtest(self):
        from futuresfund.board import Board
        from futuresfund.learn import _search

        calls = []

        def execute(params):
            calls.append(params)
            return {"net_profit": -10.0, "max_drawdown": 20.0, "trades": 4, "days": 20}

        board = Board()
        board.set_hold_new(True)
        trials = _search({"length": 10}, execute, 50000, 1, board, None, None)
        self.assertFalse(getattr(trials, "held", False))
        self.assertEqual(len(calls), 1)
        self.assertTrue(board.snapshot()["hold_new"])

    def test_a_stop_that_moves_away_from_the_goal_is_reversed(self):
        from futuresfund.learn import next_plan

        origin = {"stop_dollars": 500.0, "tp_dollars": 3000.0, "length": 10}
        better = {"stop_dollars": 500.0, "tp_dollars": 3000.0, "length": 10}
        worse = {"stop_dollars": 800.0, "tp_dollars": 4800.0, "length": 10}
        plan = next_plan(origin, [
            {"params": better, "net_profit": -100000.0, "max_drawdown": 120000.0, "trades": 5000, "days": 260},
            {"params": worse, "net_profit": -140000.0, "max_drawdown": 160000.0, "trades": 5000, "days": 260},
        ])
        # The ladder still measures the next rung. The best pair is kept when the indicators start.
        self.assertEqual(plan["stop_dollars"], 1000.0)
        self.assertEqual(plan["tp_dollars"], 2000.0)
        self.assertEqual(plan["length"], 10)

    def test_a_flat_stop_moves_to_the_target_then_the_script_then_back(self):
        from futuresfund.learn import next_plan

        origin = {"stop_dollars": 500.0, "tp_dollars": 3000.0, "length": 10}
        same = {"net_profit": -50000.0, "max_drawdown": 60000.0, "trades": 4000, "days": 260}
        held = {"stop_dollars": 1000.0, "tp_dollars": 6000.0, "length": 10}
        rows = [
            {"params": dict(origin), **same},
            {"params": dict(held), **same},
        ]
        flat_stop = next_plan(origin, rows)
        self.assertEqual(flat_stop["stop_dollars"], 1250.0)
        self.assertEqual(flat_stop["tp_dollars"], 2000.0)
        self.assertEqual(flat_stop["length"], 10)
        capped = next_plan(origin, rows + [{"params": dict(flat_stop), **same}])
        self.assertEqual(capped["stop_dollars"], 1500.0)
        self.assertEqual(capped["tp_dollars"], 2000.0)
        self.assertEqual(capped["length"], 10)

    def test_a_flat_profit_takes_a_step_twice_as_large(self):
        from futuresfund.learn import next_plan

        plan = next_plan({"length": 10, "zone": 1.5}, [
            {"params": {"length": 10, "zone": 1.5}, "net_profit": 100.0, "max_drawdown": 50.0, "trades": 20},
            {"params": {"length": 15, "zone": 1.5}, "net_profit": 100.0, "max_drawdown": 50.0, "trades": 20},
        ])
        self.assertEqual(plan["length"], 15)
        self.assertNotEqual(plan["zone"], 1.5)

    def test_a_profit_with_a_large_drawdown_cuts_the_trade_count(self):
        from futuresfund.learn import next_plan

        origin = {"stop_dollars": 500.0, "cooldown_bars": 3}
        plan = next_plan(origin, [{
            "params": dict(origin), "net_profit": 3500.0, "max_drawdown": 20827.0, "trades": 810, "days": 1291,
        }])
        self.assertEqual(plan["stop_dollars"], 1000.0)
        self.assertEqual(plan["cooldown_bars"], 3)

    def test_a_fixed_pivot_length_becomes_an_input(self):
        from futuresfund.learn import promote_fixed_pivots

        source = "\n".join([
            "//@version=5",
            'strategy("Pivot", overlay=true)',
            "ph = ta.pivothigh(high, 3, 3)",
            "pl = ta.pivotlow(low, 3, 3)",
        ])
        fixed = promote_fixed_pivots(source)
        self.assertIn('pivot_left = input.int(3, "Pivot Left"', fixed)
        self.assertIn('pivot_right = input.int(3, "Pivot Right"', fixed)
        self.assertIn("ta.pivothigh(high, pivot_left, pivot_right)", fixed)
        self.assertIn("ta.pivotlow(low, pivot_left, pivot_right)", fixed)
        self.assertEqual(promote_fixed_pivots(fixed), fixed)
        bare = 'pivotHigh = ta.pivothigh(20, 2)\npivotLow = ta.pivotlow(20, 2)\n'
        opened = promote_fixed_pivots(bare)
        self.assertIn("ta.pivothigh(pivot_left, pivot_right)", opened)
        self.assertIn("input.int(20", opened)
        self.assertIn("input.int(2", opened)
        named = "ph = ta.pivothigh(high, pivot_left, pivot_right)\npivot_left = input.int(5)\n"
        self.assertEqual(promote_fixed_pivots(named), named)

    def test_a_decrease_that_loses_money_is_reversed(self):
        from futuresfund.learn import next_plan

        origin = {"length": 10}
        trials = [
            {"params": {"length": 10}, "net_profit": 100, "max_drawdown": 50, "trades": 2},
            {"params": {"length": 8}, "net_profit": 40, "max_drawdown": 90, "trades": 2},
        ]
        plan = next_plan(origin, trials)
        self.assertGreater(plan["length"], 10)

    def test_a_drawdown_above_twenty_percent_of_the_account_does_not_pass(self):
        from futuresfund.learn import _passed

        crossed = {"net_profit": 4000, "max_drawdown": 35000.01, "trades": 4, "stop_reason": ""}
        inside = {"net_profit": 4000, "max_drawdown": 35000, "trades": 4, "stop_reason": "Trailing DD breached", "breached": True}
        short = {"net_profit": 500, "max_drawdown": 400, "trades": 4, "stop_reason": ""}
        kept = {"net_profit": 3000, "max_drawdown": 800, "trades": 4, "stop_reason": "Profit target reached"}
        self.assertFalse(_passed(crossed, 50000))
        self.assertTrue(_passed(inside, 50000))
        self.assertFalse(_passed(short, 50000))
        self.assertTrue(_passed(kept, 50000))

    def test_a_script_uses_two_hundred_attempts_when_each_result_changes(self):
        from futuresfund.learn import study_parameters

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"net_profit": calls["n"], "max_drawdown": 100, "trades": 20}

        trials = study_parameters({"length": 10}, execute, limit=200)
        self.assertEqual(len(trials), 200)
        self.assertTrue(all(not row["passed"] for row in trials))

    def test_a_flat_start_keeps_searching_until_a_later_setting_trades(self):
        from futuresfund.learn import _search

        def execute(params):
            length = int(params["length"])
            if length > 5:
                return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}
            return {"net_profit": float(length), "max_drawdown": 10, "trades": length}

        trials = _search({"length": 10}, execute, 50000, 12, None, None, None, extras=False)
        self.assertGreater(len(trials), 1)
        self.assertTrue(any(row.get("trades") for row in trials))
        self.assertFalse(any(row.get("skipped") for row in trials))

    def test_a_script_that_never_trades_is_left_only_after_the_settings_are_exhausted(self):
        from futuresfund.learn import _search

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}

        trials = _search({"length": 10, "zone": 1.5, "fast": 5}, execute, 50000, 200, None, None, None, extras=False)
        self.assertEqual(len(trials), 1)
        self.assertGreater(calls["n"], 4)
        self.assertTrue(trials[0]["skipped"])
        self.assertIn("no trades", trials[0]["note"])

    def test_an_unreachable_volume_and_cooldown_entry_is_opened(self):
        from futuresfund.entry_rules import repair_dead_entry

        source = "\n".join([
            "volume_profile = ta.highest(volume, 20)",
            "volume_condition = volume > volume_profile * volume_threshold",
            "var int lastEntryBar = na",
            "if long_condition or short_condition",
            "    lastEntryBar := bar_index",
            "if long_condition and bar_index > lastEntryBar + cooldown_bars",
            '    strategy.entry("Long", strategy.long)',
            '    strategy.exit("XL", "Long", stop=stopLoss, limit=takeProfit)',
            "",
        ])
        fixed = repair_dead_entry(source)
        self.assertIn("ta.highest(volume[1], 20)", fixed)
        self.assertNotIn("lastEntryBar := bar_index\nif long_condition", fixed)
        self.assertIn("na(lastEntryBar) or bar_index > lastEntryBar + cooldown_bars", fixed)
        self.assertIn('strategy.entry("Long", strategy.long)\n    lastEntryBar := bar_index', fixed)
        self.assertEqual(repair_dead_entry(fixed), fixed)

    def test_the_search_keeps_the_best_result_and_uses_an_untried_setting(self):
        from futuresfund.learn import _from_best, _search

        bounds = {"length": (5, 30)}
        trials = [
            {"params": {"length": 10}, "net_profit": 100.0, "max_drawdown": 400.0, "trades": 4},
            {"params": {"length": 11}, "net_profit": 100.0, "max_drawdown": 900.0, "trades": 4},
        ]
        seen = {__import__("json").dumps(row["params"], sort_keys=True) for row in trials}
        plan = _from_best(trials, bounds, seen)
        self.assertEqual(plan["length"], 9)

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"net_profit": float(params["length"]), "max_drawdown": 100, "trades": int(params["length"])}

        found = _search({"length": 10}, execute, 50000, 8, None, None, None, {"inputs": [{"name": "length", "min": 5, "max": 30}]}, extras=False)
        self.assertEqual(len(found), 8)
        self.assertGreaterEqual(found.engine_runs, 8)

    def test_the_same_result_is_not_counted_as_an_attempt(self):
        from futuresfund.learn import study_parameters

        def execute(params):
            return {"net_profit": -10, "max_drawdown": 100, "trades": 1}

        trials = study_parameters({"length": 10}, execute, limit=200)
        self.assertEqual(len(trials), 1)


class ReportShapeTests(unittest.TestCase):
    def test_a_result_under_the_target_is_not_described_as_filed(self):
        from unittest import mock

        from futuresfund.learn import _result_facts
        from futuresfund.mailer import write_result

        best = {
            "net_profit": 744.0,
            "max_drawdown": 5626.0,
            "trades": 71,
            "timeframe": "15m",
            "contract": "GC1!",
            "days": 21,
            "engine": "backtrader",
            "params": {"tp_dollars": 500.0},
        }
        with mock.patch("futuresfund.library.shelf_for", return_value=[]):
            facts = _result_facts({"title": "Smart Trail NO CONDITIONS ES Evening"}, Path("smart.pine"), best, "QO", 200)
        self.assertIn("On the strategy library: no", facts)
        self.assertIn("was not filed", facts)
        self.assertIn("not used to accept or reject a run", facts)
        self.assertIn("Trades a week: 16.9", facts)
        self.assertIn("strategy library page", facts)
        self.assertIn("went lower than $2,500", facts)
        self.assertNotIn("Meets the profit target", facts)
        self.assertNotIn("Filed in the", facts)
        self.assertNotIn("1. The bet", facts)

        def writer(prompt):
            self.assertIn("Profit 744.0", prompt)
            self.assertIn("On the strategy library: no", prompt)
            return "Filed in the Gold strategy library (GC1!). 15m profit 744.0, drawdown 5626.0, trades 71."

        sent = write_result(facts, writer)
        self.assertNotIn("Filed in the", sent)
        self.assertIn("was not filed", sent)

    def test_the_portfolio_manager_writes_a_new_note_when_it_stays_honest(self):
        from futuresfund.mailer import write_result

        facts = "\n".join([
            "Strategy: Pullback.",
            "Chart tested: 5-minute ES (ES1!).",
            "Profit 4200. Drawdown 800. Trades 30.",
            "Trades a day: 1.5 over 20 trading days.",
            "Meets the profit target: yes.",
            "Inside the trailing drawdown: yes.",
            "Inside the pace: yes.",
            "On the strategy library: yes.",
            "This result is on the ES strategy library.",
        ])
        sent = write_result(facts, lambda prompt: "Pullback on the 5-minute ES chart made $4,200 with an $800 drawdown in 30 trades, 1.5 a day. It is on the ES strategy library.")
        self.assertIn("$4,200", sent)
        self.assertNotIn("1. The bet", sent)

    def test_the_report_goes_to_the_fund_address_and_does_not_wait_on_smtp(self):
        from unittest import mock

        from futuresfund import mailer
        from futuresfund.mailer import report_recipients, send_report

        os.environ.pop("REPORT_TO", None)
        os.environ.pop("SMTP_PASSWORD", None)
        self.assertEqual(report_recipients(), ["thefutureoffuturestrading@gmail.com"])
        with TemporaryDirectory() as tmp, mock.patch.object(mailer, "MAIL_LOG", Path(tmp) / "_sent.json"):
            result = send_report("A report", subject="Futures fund research report: Test")
            self.assertFalse(result["sent"])
            self.assertIn("thefutureoffuturestrading@gmail.com", result["reason"])
            logged = mailer.mail_log()
        self.assertEqual(len(logged), 1)
        self.assertFalse(logged[0]["sent"])
        self.assertIn("Test", logged[0]["subject"])

    def test_a_trade_email_summarizes_the_close_and_leaves_the_sender_off(self):
        from futuresfund.mailer import report_recipients, trade_email

        previous_user = os.environ.get("SMTP_USER")
        previous_to = os.environ.get("REPORT_TO")
        os.environ["SMTP_USER"] = "rece2005@gmail.com"
        os.environ["REPORT_TO"] = "rece2005@gmail.com,thefutureoffuturestrading@gmail.com"
        self.assertEqual(report_recipients(), ["thefutureoffuturestrading@gmail.com"])
        subject, body = trade_email(
            {"account": "APEX-441587-44", "instrument": "ES1!", "qty": 1, "price": 7731.0, "strategy": "RSI 3-System Strategy"},
            {"action": "close", "side": "BUY", "target": 0},
            {"sent": True, "dry_run": False},
            -1,
            {
                "trades": [{"account": "APEX-441587-44", "instrument": "ES1!", "side": "short", "qty": 1, "entry": 7749.25, "exit": 7731.0, "pnl": 912.5}],
                "positions": {},
                "account_books": {"APEX-441587-44": {"size": 50000, "realized": 912.5, "peak": 50912.5}},
                "strategy": {"account_size": 50000},
                "paper": {"realized": 912.5, "peak": 50912.5},
            },
        )
        self.assertEqual(subject, "Trade closed: BUY 1 ES1!")
        self.assertIn("Closed BUY 1 ES1!", body)
        self.assertIn("ES1!", body)
        self.assertIn("912.5", body)
        self.assertIn("flat", body)
        self.assertNotIn("rece2005@gmail.com", body)
        if previous_user is None:
            os.environ.pop("SMTP_USER", None)
        else:
            os.environ["SMTP_USER"] = previous_user
        if previous_to is None:
            os.environ.pop("REPORT_TO", None)
        else:
            os.environ["REPORT_TO"] = previous_to


class FirmRatioMailTests(unittest.TestCase):
    def test_the_portfolio_manager_emails_a_near_ratio_and_files_it(self):
        from unittest import mock

        from futuresfund.board import Board
        from futuresfund.learn import _offer_library

        sent = []
        filed = []

        def fake_record(entry):
            filed.append(dict(entry))
            return True

        def fake_send(body, subject="Futures desk report"):
            sent.append((subject, body))
            return {"sent": True, "to": "thefutureoffuturestrading@gmail.com"}

        class Immediate:
            def submit(self, agent, message, work, **kwargs):
                self.agent = agent
                work()

        board = Board()
        row = {
            "engine": "backtrader",
            "runner": "backtrader",
            "net_profit": 50000.0,
            "max_drawdown": 2000.0,
            "trades": 150,
            "days": 260,
            "win_rate": 65,
            "profit_factor": 1.4,
            "instrument": "ES",
            "timeframe": "5m",
            "params": {"length": 10},
            "profit_ratio": 1.2,
        }
        crew = Immediate()
        with TemporaryDirectory() as tmp, mock.patch("futuresfund.learn.NOTES", Path(tmp)), mock.patch(
            "futuresfund.library.record_profitable", fake_record
        ), mock.patch("futuresfund.library.record_untested", lambda entry, **kwargs: False), mock.patch(
            "futuresfund.mailer.send_report", fake_send
        ), mock.patch("futuresfund.library.shelf_for", lambda title, contract="": list(filed)), mock.patch(
            "futuresfund.crew.get_crew", lambda _board: crew
        ):
            _offer_library(board, {"title": "Overnight Drift"}, Path("overnight_drift.pine"), row)
            self.assertEqual(crew.agent, "Portfolio Manager")
            self.assertEqual(sent[0][0], "Futures fund library: Overnight Drift")
            self.assertIn("Profit 50000.0", sent[0][1])
            self.assertIn("Drawdown 2000.0", sent[0][1])
            self.assertIn("On the strategy library: yes", sent[0][1])
            self.assertEqual(filed[0]["net_profit"], 50000.0)
            posted = [item["text"] for item in board.snapshot()["messages"] if item["author"] == "Portfolio Manager"]
            self.assertTrue(any("thefutureoffuturestrading@gmail.com" in text for text in posted))

            sent.clear()
            filed.clear()
            _offer_library(board, {"title": "Buffered"}, Path("buffered.pine"), dict(row, net_profit=40000.0))
            self.assertEqual(sent[0][0], "Futures fund library: Buffered")
            self.assertIn("Profit 40000.0", sent[0][1])

            sent.clear()
            filed.clear()
            _offer_library(board, {"title": "Larger"}, Path("larger.pine"), dict(row, net_profit=80000.0))
            self.assertEqual(len(sent), 1)
            self.assertIn("Profit 80000.0", sent[0][1])

            sent.clear()
            filed.clear()
            _offer_library(board, {"title": "Small"}, Path("small.pine"), dict(row, net_profit=39000.0))
            self.assertEqual(sent, [])
            self.assertEqual(filed[0]["net_profit"], 39000.0)

            filed.clear()
            _offer_library(board, {"title": "Thin"}, Path("thin.pine"), dict(row, trades=2))
            self.assertEqual(filed, [])
            self.assertEqual(sent, [])

            filed.clear()
            sent.clear()
            _offer_library(board, {"title": "Low Ratio"}, Path("low_ratio.pine"), dict(row, profit_ratio=1.12))
            self.assertEqual(sent, [])


class RatioLibraryTests(unittest.TestCase):
    def test_the_measured_day_shift_run_is_listed_and_emailed_once(self):
        from unittest import mock

        from futuresfund import library
        from futuresfund.board import Board
        from futuresfund.learn import _offer_library

        measured = {
            "engine": "backtrader",
            "runner": "backtrader",
            "net_profit": 53895.0,
            "max_drawdown": 2000.0,
            "trades": 400,
            "days": 260,
            "win_rate": 65,
            "profit_factor": 1.4,
            "instrument": "GC",
            "timeframe": "2m",
            "params": {"stop_dollars": 1500.0, "tp_dollars": 9000.0},
            "profit_ratio": 1.2,
        }
        sent = []

        def fake_send(body, subject="Futures desk report"):
            sent.append((subject, body))
            return {"sent": True, "to": "thefutureoffuturestrading@gmail.com"}

        class Immediate:
            def submit(self, agent, message, work, **kwargs):
                work()

        with TemporaryDirectory() as tmp, mock.patch.object(library, "_NOTES", Path(tmp)), mock.patch(
            "futuresfund.learn.NOTES", Path(tmp)
        ), mock.patch("futuresfund.library.record_profitable", lambda entry: True), mock.patch(
            "futuresfund.mailer.send_report", fake_send
        ), mock.patch("futuresfund.crew.get_crew", lambda _board: Immediate()):
            board = Board()
            crowded = dict(measured, trades=1335)
            _offer_library(board, {"title": "Delta Volume BOS Day Shift"}, Path("delta_volume_bos_day.pine"), crowded)
            self.assertEqual(library.search_library("GC1!")["untested"], [])
            self.assertEqual(sent, [])
            _offer_library(board, {"title": "Delta Volume BOS Day Shift"}, Path("delta_volume_bos_day.pine"), measured)
            listed = library.search_library("GC1!")["untested"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["script"], "Delta Volume BOS Day Shift")
            self.assertEqual(listed[0]["net_profit"], 53895.0)
            self.assertEqual(listed[0]["max_drawdown"], 2000.0)
            self.assertEqual(listed[0]["trades"], 400)
            self.assertFalse(listed[0]["full_dataset"])
            self.assertGreaterEqual(listed[0]["profit_to_drawdown"], 1.5)
            self.assertEqual(listed[0]["kind"], "Not tested on the full dataset")
            self.assertEqual(sent[-1][0], "Futures fund library: Delta Volume BOS Day Shift")
            self.assertIn("has not been tested on the full dataset", sent[-1][1])
            self.assertIn("Profit 53895.0", sent[-1][1])
            self.assertIn("Drawdown 2000.0", sent[-1][1])
            self.assertIn("Trades 400", sent[-1][1])

            sent.clear()
            _offer_library(board, {"title": "Delta Volume BOS Day Shift"}, Path("delta_volume_bos_day.pine"), measured)
            self.assertEqual(library.search_library("GC1!")["untested"][0]["net_profit"], 53895.0)
            self.assertFalse(any("has not been tested on the full dataset" in body for _subject, body in sent))

            _offer_library(
                board,
                {"title": "Delta Volume BOS Day Shift"},
                Path("delta_volume_bos_day.pine"),
                dict(measured, net_profit=30000.0),
            )
            self.assertEqual(library.search_library("GC1!")["untested"][0]["net_profit"], 53895.0)

            _offer_library(
                board,
                {"title": "Thin"},
                Path("thin.pine"),
                dict(measured, net_profit=90000.0, max_drawdown=1000.0, trades=2),
            )
            self.assertEqual(len(library.search_library("GC1!")["untested"]), 1)

    def test_a_one_year_shelf_run_waits_even_when_the_ratio_is_under_1_5(self):
        from unittest import mock

        from futuresfund import library
        from futuresfund.board import Board
        from futuresfund.learn import _offer_library

        row = {
            "engine": "backtrader",
            "runner": "backtrader",
            "net_profit": 300000.0,
            "max_drawdown": 250000.0,
            "trades": 400,
            "days": 260,
            "instrument": "NQ",
            "timeframe": "15m",
            "win_rate": 65,
            "profit_factor": 1.3,
            "sharpe": 1.2,
            "profit_ratio": 1.4,
        }
        sent = []

        def fake_send(body, subject="Futures desk report"):
            sent.append((subject, body))
            return {"sent": True}

        with TemporaryDirectory() as tmp, mock.patch.object(library, "_NOTES", Path(tmp)), mock.patch(
            "futuresfund.learn.NOTES", Path(tmp)
        ), mock.patch("futuresfund.library.record_profitable", lambda entry: True), mock.patch(
            "futuresfund.mailer.send_report", fake_send
        ):
            _offer_library(Board(), {"title": "Pivot Trend"}, Path("pivot_trend.pine"), row)
            listed = library.search_library("NQ1!")["untested"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["net_profit"], 300000.0)
            self.assertEqual(listed[0]["trades"], 400)
            self.assertEqual(listed[0]["kind"], "Not tested on the full dataset")
            self.assertFalse(listed[0]["full_dataset"])
            self.assertEqual(listed[0]["window"], "one year")
            self.assertEqual(sent, [])

            long_span = dict(row, days=1291, net_profit=88000.0)
            _offer_library(Board(), {"title": "Already Long"}, Path("already_long.pine"), long_span)
            self.assertEqual(len(library.search_library("NQ1!")["untested"]), 1)

    def test_a_one_year_62_percent_run_waits_and_stays_off_the_profitable_shelf(self):
        from unittest import mock

        from futuresfund import library
        from futuresfund.board import Board
        from futuresfund.learn import _offer_library

        row = {
            "engine": "backtrader",
            "runner": "backtrader",
            "net_profit": 57885.0,
            "max_drawdown": 18580.0,
            "trades": 375,
            "days": 260,
            "win_rate": 62.93,
            "profit_factor": 1.295,
            "profit_ratio": 0.763,
            "biggest_loser": -5855.0,
            "biggest_winner": 3480.0,
            "sharpe": 2.107,
            "instrument": "NQ",
            "timeframe": "15m",
            "params": {"tpMult": 1.0, "stopMult": 1.5},
        }
        with TemporaryDirectory() as tmp, mock.patch.object(library, "_NOTES", Path(tmp)), mock.patch(
            "futuresfund.learn.NOTES", Path(tmp)
        ), mock.patch("futuresfund.mailer.send_report", lambda *args, **kwargs: {"sent": False}):
            _offer_library(Board(), {"title": "POC RSI Momentum v20"}, Path("poc_rsi_momentum_v20.pine"), row)
            self.assertEqual(library.search_library("NQ1!")["untested"], [])
            self.assertEqual(library.search_library("NQ1!")["strategies"], [])

            held = dict(row, biggest_loser=-1500.0)
            _offer_library(Board(), {"title": "POC RSI Momentum v20"}, Path("poc_rsi_momentum_v20.pine"), held)
            listed = library.search_library("NQ1!")["untested"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["win_rate"], 62.93)
            self.assertEqual(listed[0]["net_profit"], 57885.0)
            self.assertEqual(listed[0]["kind"], "Not tested on the full dataset")
            self.assertEqual(library.search_library("NQ1!")["strategies"], [])

            full = dict(row, days=1300, max_drawdown=2000.0, profit_ratio=1.2, biggest_loser=-400.0)
            _offer_library(Board(), {"title": "POC RSI Momentum v20"}, Path("poc_rsi_momentum_v20.pine"), full)
            strategies = library.search_library("NQ1!")["strategies"]
            self.assertEqual(len(strategies), 1)
            self.assertEqual(strategies[0]["days"], 1300)
            self.assertEqual(len(library.search_library("NQ1!")["untested"]), 1)

    def test_an_accepted_run_stays_when_the_library_numbers_change(self):
        import tempfile
        from unittest import mock

        from futuresfund import book as bookmod
        from futuresfund import library
        from futuresfund.board import Board
        from futuresfund.book import save_strategy_acceptance
        from futuresfund.learn import _offer_library

        original = bookmod.BOOK_PATH
        row = {
            "engine": "backtrader",
            "runner": "backtrader",
            "net_profit": 15000.0,
            "max_drawdown": 800.0,
            "trades": 80,
            "days": 260,
            "win_rate": 50.0,
            "profit_factor": 1.3,
            "profit_ratio": 0.8,
            "biggest_loser": -400.0,
            "instrument": "ES",
            "timeframe": "15m",
            "params": {"stop": 10},
        }
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(library, "_NOTES", Path(tmp)), mock.patch(
                "futuresfund.learn.NOTES", Path(tmp)
            ), mock.patch("futuresfund.library.record_profitable", lambda entry: False):
                bookmod.BOOK_PATH = Path(tmp) / "book.json"
                save_strategy_acceptance({
                    "min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 60,
                })
                _offer_library(Board(), {"title": "Kept Run"}, Path("kept_run.pine"), row)
                listed = library.search_library("ES1!")["untested"]
                self.assertEqual([item["net_profit"] for item in listed], [15000.0])
                save_strategy_acceptance({
                    "min_profit": 40000, "profit_factor": 2.0, "win_rate": 70, "min_trades": 200,
                })
                listed = library.search_library("ES1!")["untested"]
                self.assertEqual([item["title"] for item in listed], ["Kept Run"])
                _offer_library(Board(), {"title": "Too Late"}, Path("too_late.pine"), dict(row, net_profit=18000.0, trades=90))
                self.assertEqual([item["title"] for item in library.search_library("ES1!")["untested"]], ["Kept Run"])
        finally:
            bookmod.BOOK_PATH = original


class QueueTests(unittest.TestCase):
    def test_a_finished_script_leaves_the_learning_folder(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            learned = root / "learnedStrategies"
            learning.mkdir()
            (learning / "ema_test.pine").write_text("strategy(\"EMA test\")", encoding="utf-8")
            (learning / "ema_test.py").write_text("class Strategy: pass", encoding="utf-8")
            (learning / "test_helper.py").write_text("print('skip')", encoding="utf-8")
            jobs = learn.queue(learning)
            self.assertEqual([path.name for path in jobs], ["ema_test.pine"])
            previous = learn.LEARNED
            learn.LEARNED = learned
            try:
                learn._file_away(jobs[0], learning / "ema_test.py")
            finally:
                learn.LEARNED = previous
            self.assertFalse((learning / "ema_test.pine").exists())
            self.assertTrue((learned / "ema_test.pine").exists())
            self.assertTrue((learning / "ema_test.py").exists())
            self.assertFalse((learned / "ema_test.py").exists())
            self.assertTrue((learning / "test_helper.py").exists())

    def test_a_script_that_finished_every_chart_is_filed_without_another_study(self):
        import json

        from futuresfund import learn

        class Board:
            def post(self, author, text, kind="log", channel=None):
                self.text = text

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            learned = root / "learnedStrategies"
            notes = root / "notes"
            learning.mkdir()
            notes.mkdir()
            script = learning / "ema_done.pine"
            script.write_text("strategy(\"EMA done\")", encoding="utf-8")
            covered = {
                "instruments": {
                    name: {"attempts": 1, "trials": [{"trades": 10, "net_profit": 1, "max_drawdown": 1}]}
                    for name in ("ES", "QO", "NQ")
                }
            }
            (notes / "ema_done.json").write_text(json.dumps(covered), encoding="utf-8")
            previous = (learn.LEARNING, learn.LEARNED, learn.NOTES)
            learn.LEARNING, learn.LEARNED, learn.NOTES = learning, learned, notes
            board = Board()
            try:
                learn.file_completed_scripts(board)
            finally:
                learn.LEARNING, learn.LEARNED, learn.NOTES = previous
            self.assertFalse(script.exists())
            self.assertTrue((learned / "ema_done.pine").exists())
            self.assertIn("Filed ema_done.pine", board.text)

    def test_a_second_copy_of_a_finished_chart_is_not_opened_again(self):
        import json

        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            notes = folder / "notes"
            notes.mkdir()
            script = "//@version=5\nstrategy(\"Day\")\n"
            first = folder / "Day shift safe.txt"
            second = folder / "Day shift  safe.txt"
            first.write_text(script, encoding="utf-8")
            second.write_text(script, encoding="utf-8")
            payload = {
                "instruments": {
                    "ES": {
                        "charts": {
                            "2m": {
                                "attempts": 1,
                                "trials": [{"net_profit": 10, "trades": 20, "max_drawdown": 5}],
                                "best": {"net_profit": 10, "trades": 20, "max_drawdown": 5},
                            }
                        }
                    }
                }
            }
            (notes / f"{first.stem.replace(' ', '_')}.json").write_text(json.dumps(payload), encoding="utf-8")
            previous = learn.NOTES
            learn.NOTES = notes
            try:
                self.assertTrue(learn._copy_finished(second, "2m", "ES"))
                self.assertFalse(learn._copy_finished(second, "15m", "ES"))
                self.assertFalse(learn._copy_finished(first, "2m", "ES"))
                self.assertNotIn("ES", learn._open_roots(second, "2m"))
                self.assertIn("ES", learn._open_roots(second, "15m"))
            finally:
                learn.NOTES = previous

    def test_a_chart_engine_leaves_a_loss_after_forty_results(self):
        from futuresfund.learn import ATTEMPT_LIMIT, LOSS_LOOK, _search

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"net_profit": -100.0 - calls["n"], "max_drawdown": 80.0, "trades": 20, "days": 260}

        trials = _search(
            {"length": 10},
            execute,
            50000,
            ATTEMPT_LIMIT,
            None,
            None,
            None,
            extras=False,
            developer="15 min chart developer",
        )
        self.assertTrue(getattr(trials, "loss_stop", False))
        self.assertEqual(len(trials), LOSS_LOOK)
        self.assertLess(len(trials), ATTEMPT_LIMIT)

    def test_a_created_script_is_scrapped_after_twenty_attempts_with_no_profit(self):
        from futuresfund.learn import ATTEMPT_LIMIT, CREATED_PROFIT_ATTEMPTS, _search

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"net_profit": -50.0 - calls["n"], "max_drawdown": 80.0, "trades": 12, "days": 260}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = root / "createdStrategies"
            aside = root / "cannotRunStrategies"
            created.mkdir()
            path = created / "created_new.pine"
            path.write_text('//@version=5\nstrategy("New")\n', encoding="utf-8")
            from futuresfund import learn

            previous = (learn.CANNOT_RUN, learn.CREATED)
            learn.CANNOT_RUN = aside
            learn.CREATED = created
            try:
                trials = _search(
                    {"length": 10},
                    execute,
                    50000,
                    ATTEMPT_LIMIT,
                    None,
                    path,
                    None,
                    extras=False,
                    developer="Creation tester",
                )
                self.assertTrue(getattr(trials, "scrapped", False))
                self.assertEqual(len(trials), CREATED_PROFIT_ATTEMPTS)
                self.assertFalse(path.exists())
                self.assertTrue(any(aside.glob("created_new*.pine")))
            finally:
                learn.CANNOT_RUN, learn.CREATED = previous

    def test_a_chart_engine_keeps_going_when_the_result_clears_acceptance(self):
        from unittest.mock import patch

        from futuresfund.learn import _search

        def execute(params):
            return {
                "net_profit": 20000.0 + float(params["length"]),
                "max_drawdown": 4000.0,
                "trades": 150,
                "days": 260,
                "win_rate": 60.0,
                "profit_factor": 1.5,
                "biggest_loser": -400.0,
                "window": "one year",
            }

        limits = {"min_profit": 14000.0, "profit_factor": 1.1, "win_rate": 48.0, "min_trades": 100, "use_profit": True}
        with patch("futuresfund.book.strategy_acceptance", return_value=limits):
            trials = _search(
                {"length": 10},
                execute,
                50000,
                6,
                None,
                None,
                None,
                extras=False,
                developer="5 min chart developer",
            )
        self.assertFalse(getattr(trials, "acceptance_stop", False))
        self.assertGreater(len(trials), 1)

    def test_a_chart_engine_leaves_after_three_identical_results(self):
        from futuresfund.learn import _search

        calls = []

        def execute(params):
            calls.append(dict(params))
            return {"net_profit": -100.0, "max_drawdown": 50.0, "trades": 12, "days": 20}

        trials = _search(
            {"length": 10},
            execute,
            50000,
            20,
            None,
            None,
            None,
            extras=False,
            developer="15 min chart developer",
        )
        self.assertTrue(getattr(trials, "repeat_stop", False))
        self.assertEqual(len(calls), 4)

    def test_a_text_file_that_holds_a_pine_script_joins_the_queue(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            learning = Path(tmp)
            (learning / "delta.txt").write_text("//@version=5\nstrategy(\"Delta\")\n", encoding="utf-8")
            (learning / "licensed.txt").write_text("// Mozilla\n\n//@version=5\nstrategy(\"Licensed\")\n", encoding="utf-8")
            (learning / "note.txt").write_text("This folder note is not a script.\n", encoding="utf-8")
            (learning / "real.pine").write_text("strategy(\"Real\")\n", encoding="utf-8")
            self.assertEqual(
                [path.name for path in learn.queue(learning)],
                ["delta.txt", "licensed.txt", "real.pine"],
            )

    def test_a_created_script_waits_behind_the_other_learning_scripts(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            learning = Path(tmp)
            (learning / "created_new.pine").write_text("//@version=5\nstrategy(\"Created\")\n", encoding="utf-8")
            (learning / "Day shift.txt").write_text("//@version=5\nstrategy(\"Day\")\n", encoding="utf-8")
            names = [path.name for path in learn.queue(learning)]
            self.assertEqual(names, ["Day shift.txt", "created_new.pine"])

    def test_an_empty_learning_folder_studies_created_strategies(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            created = root / "createdStrategies"
            learning.mkdir()
            created.mkdir()
            (created / "created_bos.pine").write_text('strategy("Created")\n', encoding="utf-8")
            (created / "created_busy.pine").write_text('strategy("Busy")\n', encoding="utf-8")
            previous_learning = learn.LEARNING
            previous_created = learn.CREATED
            previous_notes = learn.NOTES
            notes = root / "researchNotes"
            notes.mkdir()
            learn.LEARNING = learning
            learn.CREATED = created
            learn.NOTES = notes
            learn.mark_developing("created_busy.pine")
            try:
                empty = root / "other"
                empty.mkdir()
                self.assertEqual(learn.queue(empty), [])
                self.assertEqual(learn.queue(), [])
                (notes / "created_bos.json").write_text(
                    '{"best": {"net_profit": 900, "trades": 10, "max_drawdown": 200}}',
                    encoding="utf-8",
                )
                self.assertEqual([path.name for path in learn.queue()], ["created_bos.pine"])
                (learning / "kept.pine").write_text('strategy("Kept")\n', encoding="utf-8")
                self.assertEqual([path.name for path in learn.queue()], ["kept.pine"])
                self.assertEqual(learn.queue(learning), [learning / "kept.pine"])
            finally:
                learn.clear_developing("created_busy.pine")
                learn.LEARNING = previous_learning
                learn.CREATED = previous_created
                learn.NOTES = previous_notes

    def test_a_finished_timeframe_studies_created_strategies(self):
        from unittest import mock

        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            created = root / "createdStrategies"
            notes = root / "researchNotes"
            learning.mkdir()
            created.mkdir()
            notes.mkdir()
            (learning / "kept.pine").write_text('strategy("Kept")\n', encoding="utf-8")
            (created / "created_next.pine").write_text('strategy("Next")\n', encoding="utf-8")
            done = {"attempts": 1, "best": {"net_profit": 10, "trades": 5}, "trials": [{"trades": 5, "net_profit": 10}]}
            (notes / "kept.json").write_text(
                '{"instruments": {"ES": {"charts": {"5m": ' + __import__("json").dumps(done) + '}}}}',
                encoding="utf-8",
            )
            previous = (learn.LEARNING, learn.CREATED, learn.NOTES)
            learn.LEARNING, learn.CREATED, learn.NOTES = learning, created, notes
            try:
                with mock.patch("futuresfund.learn.research_instruments", lambda: ["ES"]):
                    self.assertEqual(learn.jobs_for_chart("5m"), [])
                    (notes / "created_next.json").write_text(
                        '{"instruments": {"NQ": {"best": {"net_profit": 700, "trades": 9, "max_drawdown": 150}, "charts": {"2m": {"attempts": 1, "best": {"net_profit": 700, "trades": 9}, "trials": [{"trades": 9, "net_profit": 700}]}}}}}',
                        encoding="utf-8",
                    )
                    self.assertEqual([path.name for path in learn.jobs_for_chart("5m")], ["created_next.pine"])
                    self.assertEqual([path.name for path in learn.jobs_for_chart("2m")], ["kept.pine"])
                    self.assertFalse(learn.chart_needs_a_script("5m"))
                    counts = {row["timeframe"]: row for row in learn.chart_folder_counts()}
                    self.assertEqual(counts["5m"]["learning_done"], 1)
                    self.assertEqual(counts["5m"]["learning_total"], 1)
                    self.assertEqual(counts["5m"]["created_done"], 0)
                    self.assertEqual(counts["5m"]["created_total"], 1)
                    self.assertEqual(counts["2m"]["learning_done"], 0)
                    self.assertEqual(counts["2m"]["learning_open"], 1)
                    self.assertEqual(counts["5m"]["learning_open"], 0)
                    zero = {"attempts": 4, "trials": [{"trades": 0, "net_profit": 0}]}
                    (notes / "kept.json").write_text(
                        '{"instruments": {"ES": {"charts": {"5m": ' + __import__("json").dumps(zero) + '}}}}',
                        encoding="utf-8",
                    )
                    self.assertEqual([path.name for path in learn.jobs_for_chart("5m")], ["kept.pine"])
                    recounted = {row["timeframe"]: row for row in learn.chart_folder_counts()}
                    self.assertEqual(recounted["5m"]["learning_done"], 1)
                    self.assertEqual(recounted["5m"]["learning_open"], 1)
            finally:
                learn.LEARNING, learn.CREATED, learn.NOTES = previous

    def test_a_chart_that_ran_one_instrument_counts_and_stays_in_the_queue(self):
        import json
        from unittest import mock

        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            notes = root / "researchNotes"
            learning.mkdir()
            notes.mkdir()
            (learning / "ran.pine").write_text('strategy("Ran")\n', encoding="utf-8")
            (learning / "waiting.pine").write_text('strategy("Waiting")\n', encoding="utf-8")
            run = {"attempts": 2, "trials": [{"trades": 8, "net_profit": 400}], "best": {"trades": 8, "net_profit": 400}}
            (notes / "ran.json").write_text(json.dumps({
                "instruments": {"NQ": {"charts": {"2m": run}}},
            }), encoding="utf-8")
            previous = (learn.LEARNING, learn.NOTES)
            learn.LEARNING, learn.NOTES = learning, notes
            try:
                with mock.patch("futuresfund.learn.research_instruments", lambda: ["ES", "NQ"]):
                    counts = {row["timeframe"]: row for row in learn.chart_folder_counts()}
                    self.assertEqual(counts["2m"]["learning_total"], 2)
                    self.assertEqual(counts["2m"]["learning_done"], 1)
                    self.assertEqual(counts["2m"]["learning_open"], 2)
                    names = [path.name for path in learn.jobs_for_chart("2m")]
                    self.assertEqual(names, ["ran.pine", "waiting.pine"])
            finally:
                learn.LEARNING, learn.NOTES = previous

    def test_a_script_that_cannot_run_leaves_the_learning_folder(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            aside = root / "cannotRunStrategies"
            learning.mkdir()
            script = learning / "supertrend_ai_strategy.pine"
            script.write_text('strategy("SuperTrend AI Strategy")\n', encoding="utf-8")
            (learning / "kept.pine").write_text('strategy("Kept")\n', encoding="utf-8")
            previous_learning = learn.LEARNING
            previous_aside = learn.CANNOT_RUN
            learn.LEARNING = learning
            learn.CANNOT_RUN = aside
            try:
                parked = learn.set_aside(script, "The clustering loop holds the engine.")
            finally:
                learn.LEARNING = previous_learning
                learn.CANNOT_RUN = previous_aside
            self.assertEqual(parked.name, "supertrend_ai_strategy.pine")
            self.assertFalse(script.exists())
            self.assertTrue(parked.is_file())
            self.assertIn("holds the engine", (aside / "supertrend_ai_strategy.reason.txt").read_text(encoding="utf-8"))
            self.assertEqual([path.name for path in learn.queue(learning)], ["kept.pine"])

    def test_twenty_zero_runs_move_the_script_to_cannot_run(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            aside = root / "cannotRunStrategies"
            learning.mkdir()
            script = learning / "es_delta_bos_rsi.pine"
            script.write_text('strategy("ES Delta BOS RSI")\n', encoding="utf-8")
            previous_learning = learn.LEARNING
            previous_aside = learn.CANNOT_RUN
            learn.LEARNING = learning
            learn.CANNOT_RUN = aside
            learn._flat_runs.clear()

            def execute(_params):
                return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}

            try:
                for _ in range(learn.FLAT_RUNS - 1):
                    trials = learn._search(
                        {"length": 10}, execute, 50000, 30, None, script, None, extras=False,
                        developer="2min chart developer",
                    )
                    self.assertFalse(getattr(trials, "flat_stop", False))
                    self.assertTrue(script.is_file())
                trials = learn._search(
                    {"length": 10}, execute, 50000, 30, None, script, None, extras=False,
                    developer="2min chart developer",
                )
                self.assertTrue(trials.flat_stop)
                self.assertFalse(script.is_file())
                parked = aside / "es_delta_bos_rsi.pine"
                self.assertTrue(parked.is_file())
                reason = (aside / "es_delta_bos_rsi.reason.txt").read_text(encoding="utf-8")
                self.assertIn("no trades", reason)
                self.assertIn("no profit", reason)
                self.assertIn("20", reason)
                self.assertEqual(learn.queue(learning), [])
            finally:
                learn._flat_runs.clear()
                learn.LEARNING = previous_learning
                learn.CANNOT_RUN = previous_aside

    def test_a_zero_trade_chart_stays_pending_until_twenty_runs_move_it(self):
        from futuresfund import learn

        notes = {
            "instruments": {
                "ES": {
                    "charts": {
                        "5m": {
                            "trials": [{"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}],
                            "best": {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0},
                        }
                    }
                }
            }
        }
        self.assertFalse(learn._chart_finished(notes, "ES", "5m"))
        self.assertEqual(learn.pending_for_chart(notes, "5m", ["ES"]), ["ES"])

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            aside = root / "cannotRunStrategies"
            learning.mkdir()
            script = learning / "created_es_delta_bos_5m.pine"
            script.write_text('strategy("ES_Delta_BOS_5m")\n', encoding="utf-8")
            previous_learning = learn.LEARNING
            previous_aside = learn.CANNOT_RUN
            learn.LEARNING = learning
            learn.CANNOT_RUN = aside
            learn._flat_runs.clear()
            learn._flat_runs[learn._flat_key(script)] = learn.FLAT_RUNS - 1

            def execute(_params):
                return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}

            try:
                trials = learn._search(
                    {"length": 10}, execute, 50000, 30, None, script, None, extras=False,
                    developer="5 min chart developer",
                )
                self.assertTrue(trials.flat_stop)
                self.assertFalse(script.is_file())
                self.assertTrue((aside / "created_es_delta_bos_5m.pine").is_file())
                self.assertEqual(learn.queue(learning), [])
                reason = (aside / "created_es_delta_bos_5m.reason.txt").read_text(encoding="utf-8")
                self.assertIn("created_es_delta_bos_5m.pine", reason)
                self.assertIn("no trades", reason)
                self.assertIn("no profit", reason)
            finally:
                learn._flat_runs.clear()
                learn.LEARNING = previous_learning
                learn.CANNOT_RUN = previous_aside

    def test_a_trade_clears_the_zero_run_streak(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            aside = root / "cannotRunStrategies"
            learning.mkdir()
            script = learning / "es_delta_bos_rsi.pine"
            script.write_text('strategy("ES Delta BOS RSI")\n', encoding="utf-8")
            previous_learning = learn.LEARNING
            previous_aside = learn.CANNOT_RUN
            learn.LEARNING = learning
            learn.CANNOT_RUN = aside
            learn._flat_runs.clear()

            def zero(_params):
                return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}

            def trade(_params):
                return {"net_profit": 25.0, "max_drawdown": 10.0, "trades": 4}

            try:
                for _ in range(10):
                    learn._search(
                        {"length": 10}, zero, 50000, 30, None, script, None, extras=False,
                        developer="2min chart developer",
                    )
                learn._search(
                    {"length": 10}, trade, 50000, 30, None, script, None, extras=False,
                    developer="2min chart developer",
                )
                for _ in range(learn.FLAT_RUNS - 1):
                    learn._search(
                        {"length": 10}, zero, 50000, 30, None, script, None, extras=False,
                        developer="2min chart developer",
                    )
                self.assertTrue(script.is_file())
                self.assertFalse(aside.exists() and any(aside.glob("*.pine")))
            finally:
                learn._flat_runs.clear()
                learn.LEARNING = previous_learning
                learn.CANNOT_RUN = previous_aside

    def test_one_search_stops_at_twenty_zero_runs(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learningStrategies"
            aside = root / "cannotRunStrategies"
            learning.mkdir()
            script = learning / "flat.pine"
            script.write_text("", encoding="utf-8")
            previous_learning = learn.LEARNING
            previous_aside = learn.CANNOT_RUN
            learn.LEARNING = learning
            learn.CANNOT_RUN = aside
            learn._flat_runs.clear()
            calls = {"n": 0}

            def execute(_params):
                calls["n"] += 1
                return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}

            try:
                trials = learn._search(
                    {"length": 10, "zone": 1.5, "fast": 5, "slow": 8},
                    execute,
                    50000,
                    40,
                    None,
                    script,
                    None,
                    extras=False,
                    developer="Creation tester",
                )
                self.assertTrue(trials.flat_stop)
                self.assertEqual(calls["n"], learn.FLAT_RUNS)
                self.assertFalse(script.is_file())
                self.assertTrue((aside / "flat.pine").is_file())
            finally:
                learn._flat_runs.clear()
                learn.LEARNING = previous_learning
                learn.CANNOT_RUN = previous_aside


class ChartCountTests(unittest.TestCase):
    def test_a_finished_attempt_counts_on_that_chart_before_the_search_ends(self):
        from futuresfund import learn

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            script = folder / "sample.pine"
            script.write_text("//@version=5\nstrategy(\"Sample\")\n", encoding="utf-8")
            previous = learn.NOTES
            learn.NOTES = folder
            learn._view_cache.clear()
            try:
                learn._remember_chart_attempt(script, {"title": "Sample"}, {
                    "timeframe": "2m",
                    "instrument": "NQ",
                    "net_profit": 100,
                    "max_drawdown": 50,
                    "trades": 4,
                }, 1)
                learn._remember_chart_attempt(script, {"title": "Sample"}, {
                    "timeframe": "5m",
                    "instrument": "ES",
                    "net_profit": 20,
                    "max_drawdown": 10,
                    "trades": 2,
                }, 1)
                learn._view_cache.clear()
                self.assertTrue(learn._ran_on_chart(script, "2m"))
                self.assertTrue(learn._ran_on_chart(script, "5m"))
                self.assertFalse(learn._ran_on_chart(script, "15m"))
                notes = learn._read_notes(script)
                self.assertEqual(notes["instruments"]["NQ"]["charts"]["2m"]["attempts"], 1)
                self.assertIn("5m", notes["instruments"]["ES"]["charts"])
            finally:
                learn.NOTES = previous
                learn._view_cache.clear()
