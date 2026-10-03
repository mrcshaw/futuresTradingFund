"""A missing entry rule is added once and then used by the engine."""

import tempfile
import unittest
from pathlib import Path

from futuresfund.backtrader_engine import Bar
from futuresfund.entry_rules import RULES, covered, save_rule


class EntryRuleTests(unittest.TestCase):
    def setUp(self):
        self._rules = RULES
        self.folder = tempfile.TemporaryDirectory()
        import futuresfund.entry_rules as entry_rules

        entry_rules.RULES = Path(self.folder.name)

    def tearDown(self):
        import futuresfund.entry_rules as entry_rules

        entry_rules.RULES = self._rules
        self.folder.cleanup()

    def test_a_simple_signal_is_already_on_the_engine(self):
        source = 'strategy("Plain Cross", overlay=true)\nbool long_sig = close > open\nbool short_sig = close < open\n'
        self.assertTrue(covered(source))

    def test_an_unknown_condition_needs_a_rule(self):
        source = 'strategy("Secret Oscillator", overlay=true)\nbool long_sig = mystery == 1\nbool short_sig = mystery == 2\n'
        self.assertFalse(covered(source))

    def test_a_saved_rule_is_what_the_engine_runs(self):
        from futuresfund.script_backtest import run_source

        source = (
            'strategy("Secret Oscillator", overlay=true, slippage=1, commission_value=5.50)\n'
            'stop_dollars = input.float(50, "Stop ($)")\n'
            'tp_dollars = input.float(50, "Take Profit ($)")\n'
            'pv = input.float(50, "Point Value ($)")\n'
            "bool long_sig = mystery == 1\n"
            "bool short_sig = mystery == 2\n"
        )
        bars = []
        price = 100.0
        for index in range(40):
            price += 3 if index % 2 == 0 else -4
            bars.append(Bar(1_700_000_000 + index * 120, price - 1, price + 2, price - 2, price, 10))
        save_rule(
            "Secret Oscillator",
            "def signals(bars, values):\n    return [(index % 4 == 0, False) for index, bar in enumerate(bars)]\n",
            bars,
        )
        trades = [trade for trade in run_source(source, bars, None) if trade.get("exit_time")]
        self.assertGreater(len(trades), 0)

    def test_a_rule_cannot_import(self):
        bars = [Bar(1_700_000_000, 1, 2, 0, 1.5, 1)]
        with self.assertRaises(ValueError):
            save_rule("Secret Oscillator", "import os\ndef signals(bars, values):\n    return [(True, False)]\n", bars)

    def test_the_ema_crossover_script_does_not_need_a_new_rule(self):
        folder = Path(__file__).resolve().parents[1] / "futuresfund" / "learningStrategies"
        source = (folder / "ema_5_50_200.pine").read_text(encoding="utf-8")
        self.assertTrue(covered(source))
