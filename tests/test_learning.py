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
            profit = 3000 if params["length"] >= 14 else -50
            return {"net_profit": profit, "max_drawdown": 400, "trades": 5, "win_rate": 40}

        trials = study_parameters({"length": 10}, execute, limit=40)
        self.assertEqual(len(trials), 40)
        self.assertTrue(any(row["passed"] for row in trials))

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

    def test_a_decrease_that_loses_money_is_reversed(self):
        from futuresfund.learn import next_plan

        origin = {"length": 10}
        trials = [
            {"params": {"length": 10}, "net_profit": 100, "max_drawdown": 50, "trades": 2},
            {"params": {"length": 8}, "net_profit": 40, "max_drawdown": 90, "trades": 2},
        ]
        plan = next_plan(origin, trials)
        self.assertGreater(plan["length"], 10)

    def test_the_trail_blocks_a_result_that_touched_it_before_the_target(self):
        from futuresfund.learn import _passed

        crossed = {"net_profit": 4000, "max_drawdown": 2500, "trades": 4, "stop_reason": "Trailing DD breached", "breached": True}
        short = {"net_profit": 500, "max_drawdown": 400, "trades": 4, "stop_reason": ""}
        kept = {"net_profit": 3000, "max_drawdown": 800, "trades": 4, "stop_reason": "Profit target reached"}
        self.assertFalse(_passed(crossed, 50000))
        self.assertFalse(_passed(short, 50000))
        self.assertTrue(_passed(kept, 50000))

    def test_a_script_uses_two_hundred_attempts_unless_the_goal_is_met(self):
        from futuresfund.learn import study_parameters

        def execute(params):
            return {"net_profit": -10, "max_drawdown": 100, "trades": 1}

        trials = study_parameters({"length": 10}, execute, limit=200)
        self.assertEqual(len(trials), 200)
        self.assertTrue(all(not row["passed"] for row in trials))


class ReportShapeTests(unittest.TestCase):
    def test_the_email_covers_the_eight_findings(self):
        from futuresfund.pine_report import ceo_report, pine_facts

        text = """
        strategy("EMA pullback", overlay=true, process_orders_on_close=true, calc_on_every_tick=false,
            commission_value=5.50, slippage=1, default_qty_value=1, pyramiding=0)
        stop_dollars = input.float(250, "Stop ($)")
        tp_dollars = input.float(450, "Take Profit ($)")
        pv = input.float(50, "Point Value ($)")
        sess_start_hr = input.int(8, "Session Start Hour (ET)")
        sess_end_hr = input.int(16, "Session End Hour (ET)")
        bool long_sig = true
        bool short_sig = false
        """
        facts = pine_facts(text)
        report = ceo_report(facts, {"measured": {"net_profit": 10, "max_drawdown": 100, "trades": 4, "win_rate": 50}, "passed": True, "params": {"stop_dollars": 250}}, "Raising the stop did not help.", "Length 20 was steadier.", 3)
        for heading in (
            "1. The bet",
            "2. The stop that is actually active",
            "3. The target",
            "4. The trailing stop and breakeven",
            "5. Time of day",
            "6. Costs and size that change the result",
            "7. What the backtest cannot prove",
            "8. Where this kind of rule set usually makes and loses money",
        ):
            self.assertIn(heading, report)
        self.assertIn("9:30", report)
        self.assertIn("18:00", report)
        self.assertIn("Raising the stop did not help.", report)
        self.assertIn("best version", report.lower())
        self.assertIn("must not be crossed before the $3,000 profit target", report)
        self.assertIn("Take profit on this version: $450", report)
        self.assertIn("5-minute", report)
        self.assertIn("2-minute", report)
        self.assertIn("15-minute", report)

    def test_the_report_goes_to_the_fund_address_and_does_not_wait_on_smtp(self):
        from futuresfund.mailer import report_recipients, send_report

        os.environ.pop("REPORT_TO", None)
        os.environ.pop("SMTP_PASSWORD", None)
        self.assertEqual(report_recipients(), ["thefutureoffuturestrading@gmail.com"])
        result = send_report("A report", subject="Futures fund research report: Test")
        self.assertFalse(result["sent"])
        self.assertIn("thefutureoffuturestrading@gmail.com", result["reason"])


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
