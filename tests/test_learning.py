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
        self.assertIn("What we want", text)
        self.assertIn("What we do not want", text)
        self.assertIn("These are guidelines, not requirements.", text)
        self.assertIn("What to change", text)
        self.assertIn("Each run changes one variable.", text)
        self.assertNotIn("Keep a change", text)
        self.assertNotIn("Drop a change", text)

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
        self.assertEqual(plan["zone"], 1.5)
        self.assertGreater(plan["length"], 14)

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

    def test_a_script_uses_two_hundred_attempts_when_each_result_changes(self):
        from futuresfund.learn import study_parameters

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"net_profit": calls["n"], "max_drawdown": 100, "trades": calls["n"]}

        trials = study_parameters({"length": 10}, execute, limit=200)
        self.assertEqual(len(trials), 200)
        self.assertTrue(all(not row["passed"] for row in trials))

    def test_the_same_result_is_not_counted_as_an_attempt(self):
        from futuresfund.learn import study_parameters

        def execute(params):
            return {"net_profit": -10, "max_drawdown": 100, "trades": 1}

        trials = study_parameters({"length": 10}, execute, limit=200)
        self.assertEqual(len(trials), 1)


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
