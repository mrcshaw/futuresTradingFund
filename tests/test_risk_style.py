"""Dollar risk on a backtest, and a suggestion the risk manager can add to the guide."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from futuresfund.backtrader_engine import Bar
from futuresfund.pine_runtime import compile_pine, run_pine, run_pine_live
from futuresfund.risk_style import apply, deny, merge_risk, note_risk, suggestions


def _bars(closes, start=1_788_307_200):
    bars = []
    for k, close in enumerate(closes):
        open_ = closes[k - 1] if k else close
        bars.append(Bar(start + k * 120, open_, max(open_, close) + 0.5, min(open_, close) - 0.5, close, 10))
    return bars


HEADER = (
    '//@version=5\n'
    'strategy("Risk", overlay=true, initial_capital=25000, default_qty_type=strategy.fixed, default_qty_value=1,\n'
    '  commission_type=strategy.commission.cash_per_contract, commission_value=5.50, slippage=1, pyramiding=0,\n'
    '  process_orders_on_close=true)\n'
)

RISK_BLOCK = (
    'pv = input.float(50, "Point Value ($)")\n'
    'stop_dollars = input.float(1000, "Stop ($)")\n'
    'tp_dollars = input.float(1500, "Take Profit ($)")\n'
    'use_trail = input.bool(false, "Enable Trailing Stop")\n'
    'trail_pct = input.float(50, "Trail: Keep % of Peak Profit")\n'
    'trail_step_amt = input.float(500, "Trail: Tighten every $X profit")\n'
    'trail_step_pct = input.float(50, "Trail: Tighten by X% per step")\n'
    'trail_min_dollars = input.float(1000, "Trail: Min Profit Before Trailing ($)")\n'
)


class RiskStyleTests(unittest.TestCase):
    def test_the_risk_block_compiles_and_the_dollar_stop_closes_the_trade(self):
        source = HEADER + RISK_BLOCK + 'if bar_index == 1\n    strategy.entry("Long", strategy.long)\n'
        compile_pine(source)
        held = run_pine(source, _bars([100, 100, 100, 100]), merge_risk({}, "ES"), root="ES", timeframe="2m")
        self.assertEqual(held, [])
        # The desk stop is $700 and the target $1,500: 14 and 30 ES points from the fill.
        stopped = run_pine(source, _bars([100, 100, 70, 70]), merge_risk({}, "ES"), root="ES", timeframe="2m")
        self.assertEqual(len(stopped), 1)
        self.assertGreater(stopped[0]["exit_price"], 85)
        self.assertLess(stopped[0]["exit_price"], 87)
        self.assertEqual(stopped[0]["pnl"], -700.0)
        targeted = run_pine(source, _bars([100, 100, 140, 140]), merge_risk({}, "ES"), root="ES", timeframe="2m")
        self.assertEqual(len(targeted), 1)
        self.assertGreater(targeted[0]["exit_price"], 129)
        self.assertLess(targeted[0]["exit_price"], 131)

    def test_a_script_without_the_block_still_gets_the_dollar_stop(self):
        source = HEADER + 'if bar_index == 1\n    strategy.entry("Long", strategy.long)\n'
        # $700 at $20 a point is 35 NQ points.
        stopped = run_pine(source, _bars([100, 100, 40, 40]), merge_risk({}, "NQ"), root="NQ", timeframe="2m")
        self.assertEqual(len(stopped), 1)
        self.assertGreater(stopped[0]["exit_price"], 63)
        self.assertLess(stopped[0]["exit_price"], 66)
        self.assertEqual(stopped[0]["pnl"], -700.0)

    def test_an_atr_stop_cannot_lose_more_than_the_dollar_stop(self):
        source = (
            '//@version=5\n'
            'strategy("Wide", overlay=true, initial_capital=50000, default_qty_type=strategy.fixed, default_qty_value=1)\n'
            'if bar_index == 1\n'
            '    strategy.entry("Long", strategy.long)\n'
            'strategy.exit("Long", "Long", stop=close - 40, limit=close + 40)\n'
        )
        closes = [100, 100] + [100 - k for k in range(1, 36)]
        bars = []
        previous = closes[0]
        for k, close in enumerate(closes):
            bars.append(Bar(1_788_307_200 + k * 120, previous, max(previous, close) + 0.25, min(previous, close) - 0.25, close, 10))
            previous = close
        stopped = run_pine(source, bars, merge_risk({}, "ES"), root="ES", timeframe="2m")
        self.assertEqual(len(stopped), 1)
        self.assertGreaterEqual(stopped[0]["exit_price"], 85.5)
        self.assertLessEqual(stopped[0]["exit_price"], 86.5)
        self.assertGreater(stopped[0]["pnl"], -900)
        self.assertLess(stopped[0]["pnl"], -600)

    def test_the_pending_stop_stays_at_the_dollar_stop_after_the_script_widens_it(self):
        source = (
            '//@version=5\n'
            'strategy("Wide", overlay=true, initial_capital=50000, default_qty_type=strategy.fixed, default_qty_value=1)\n'
            'if bar_index == 1\n'
            '    strategy.entry("Long", strategy.long)\n'
            'strategy.exit("Long", "Long", stop=close - 40, limit=close + 40)\n'
        )
        state = run_pine_live(source, _bars([100, 100, 100, 100, 100]), merge_risk({}, "ES"), root="ES", timeframe="2m")
        pending = state["pending"]["exit"]
        self.assertIsNotNone(pending)
        self.assertGreater(pending["stop"], 85)
        self.assertLess(pending["stop"], 87)

    def test_a_stop_above_700_is_capped_and_the_target_has_no_maximum(self):
        from futuresfund.risk_style import merge_risk

        merged = merge_risk({"stop_dollars": 2200, "tp_dollars": 5000}, "ES")
        self.assertEqual(merged["stop_dollars"], 700)
        self.assertEqual(merged["tp_dollars"], 5000)
        kept = merge_risk({"stop_dollars": 600, "tp_dollars": 1200}, "NQ")
        self.assertEqual(kept["stop_dollars"], 600)
        self.assertEqual(kept["tp_dollars"], 1200)
        # A percent stop is a share of the $50,000 account, capped at 1.4 percent. The target has no cap.
        typed = merge_risk({"stop_pct": 1.5, "tp_pct": 4}, "ES")
        self.assertEqual(typed["stop_dollars"], 700)
        self.assertEqual(typed["tp_dollars"], 2000)
        capped = merge_risk({"stop_pct": 8, "tp_pct": 9}, "ES")
        self.assertEqual(capped["stop_dollars"], 700)
        self.assertEqual(capped["tp_dollars"], 4500)

    def test_the_most_common_problem_in_the_last_hour_is_the_suggestion(self):
        loss = {
            "net_profit": -4200,
            "max_drawdown": 5100,
            "trades": 40,
            "days": 260,
            "biggest_loser": -2400,
            "timeframe": "5m",
            "instrument": "ES",
        }
        often = {
            "net_profit": -800,
            "max_drawdown": 700,
            "trades": 900,
            "days": 260,
            "biggest_loser": -200,
            "timeframe": "2m",
            "instrument": "NQ",
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            guide = root / "guide.md"
            store = root / "risk_suggestions.json"
            guide.write_text("Read the script before changing a number.\n", encoding="utf-8")
            started = 1_700_000_000
            with mock.patch("futuresfund.risk_style.GUIDE_PATH", guide), mock.patch("futuresfund.risk_style.SUGGESTIONS_PATH", store):
                self.assertIn("1 of 1", note_risk(loss, "sample.pine", "Sample", now=started))
                self.assertIsNone(note_risk(often, "often.pine", "Often", now=started + 10))
                message = note_risk(often, "often-two.pine", "Often Two", now=started + 3601)
                self.assertIn("tighten the entry", message)
                self.assertIn("2 of 2", message)
                pending = [item for item in suggestions() if item["status"] == "pending"]
                self.assertEqual([item["id"] for item in pending], ["loss-past-stop", "losing-and-too-many-trades"])
                self.assertEqual(pending[1]["stats"]["issue_runs"], 2)
                self.assertEqual(pending[1]["stats"]["hour_runs"], 2)

    def test_apply_writes_the_guide_and_deny_leaves_it(self):
        loss = {
            "net_profit": -4200,
            "max_drawdown": 5100,
            "trades": 40,
            "days": 260,
            "biggest_loser": -2400,
        }
        often = {
            "net_profit": -800,
            "max_drawdown": 700,
            "trades": 900,
            "days": 260,
            "biggest_loser": -200,
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            guide = root / "guide.md"
            store = root / "risk_suggestions.json"
            guide.write_text("Read the script before changing a number.\n", encoding="utf-8")
            started = 1_700_000_000
            with mock.patch("futuresfund.risk_style.GUIDE_PATH", guide), mock.patch("futuresfund.risk_style.SUGGESTIONS_PATH", store):
                note_risk(often, "often.pine", "Often", now=started)
                denied = deny(suggestions()[0]["id"], now=started + 10)
                self.assertEqual(denied["status"], "denied")
                self.assertNotIn("tighten the entry", guide.read_text(encoding="utf-8"))
                self.assertIsNone(note_risk(often, "often-two.pine", "Often Two", now=started + 20))
                self.assertFalse(any(item["status"] == "pending" for item in suggestions()))
                message = note_risk(loss, "sample.pine", "Sample", now=started + 4000)
                self.assertIn("Do not raise the stop to cover a fill", message)
                self.assertIn("-2400", message)
                saved = apply(next(item["id"] for item in suggestions() if item["status"] == "pending"))
            self.assertEqual(saved["status"], "applied")
            text = guide.read_text(encoding="utf-8")
            self.assertIn("Risk adjustments", text)
            self.assertIn("Do not raise the stop to cover a fill", text)
            self.assertNotIn("tighten the entry", text)

    def test_a_backtest_older_than_an_hour_drops_out_of_the_count(self):
        loss = {
            "net_profit": -4200,
            "max_drawdown": 5100,
            "trades": 40,
            "days": 260,
            "biggest_loser": -2400,
        }
        often = {
            "net_profit": -800,
            "max_drawdown": 700,
            "trades": 900,
            "days": 260,
            "biggest_loser": -200,
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            guide = root / "how_to_research.md"
            store = root / "risk_suggestions.json"
            guide.write_text("Read the script before changing a number.\n", encoding="utf-8")
            started = 1_700_000_000
            with mock.patch("futuresfund.risk_style.GUIDE_PATH", guide), mock.patch("futuresfund.risk_style.SUGGESTIONS_PATH", store):
                note_risk(loss, "old.pine", "Old", now=started)
                message = note_risk(often, "new.pine", "New", now=started + 7200)
                self.assertIn("tighten the entry", message)
                rows = suggestions(now=started + 7200)
            self.assertEqual(rows[0]["stats"]["hour_runs"], 1)
            self.assertEqual(rows[0]["stats"]["issue_runs"], 1)
