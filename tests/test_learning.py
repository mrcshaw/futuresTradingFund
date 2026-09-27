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

    def test_a_passing_adjustment_ends_the_study(self):
        from futuresfund.learn import study_parameters

        def execute(params):
            profit = 100 if params["length"] >= 14 else -50
            return {"net_profit": profit, "max_drawdown": 400, "trades": 5, "win_rate": 40}

        trials = study_parameters({"length": 10}, execute)
        self.assertTrue(trials[-1]["passed"])
        self.assertLess(len(trials), 200)


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
            (learning / "test_helper.py").write_text("print('skip')", encoding="utf-8")
            jobs = learn.queue(learning)
            self.assertEqual([path.name for path in jobs], ["ema_test.pine"])
            previous = learn.LEARNED
            learn.LEARNED = learned
            try:
                learn._file_away(jobs[0], None)
            finally:
                learn.LEARNED = previous
            self.assertFalse((learning / "ema_test.pine").exists())
            self.assertTrue((learned / "ema_test.pine").exists())
            self.assertTrue((learning / "test_helper.py").exists())
