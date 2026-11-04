"""The engine takes the trades TradingView's Strategy Tester takes on the same chart."""

import shutil
import unittest
from unittest import mock
from pathlib import Path

from futuresfund.backtrader_engine import Bar, script_trades
from futuresfund.risk_style import declared_inputs, merge_risk
from futuresfund.tv_checks import _contract_for

ROOT = Path(__file__).resolve().parent.parent
MNQ_EXPORT = ROOT / "EMA_Structure_RSI_Pullback_Night_NMQ_2min_CME_MINI_MNQZ2026_2026-10-28_e3886.csv"
MNQ_MINUTES = ROOT / "chartData" / "mnq" / "glbx-mdp3-20251016-20261015.ohlcv-1m.csv"

ONE_TRADE = (
    '//@version=5\n'
    'strategy("One trade", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1)\n'
    'if bar_index == 1\n'
    '    strategy.entry("Long", strategy.long)\n'
    'if bar_index == 3\n'
    '    strategy.close_all(comment="Done")\n'
)


def _bars(closes, start=1_788_307_200):
    return [Bar(start + k * 300, close, close + 1, close - 1, close, 10) for k, close in enumerate(closes)]


class RiskInputTests(unittest.TestCase):
    def test_a_stop_the_script_declares_keeps_its_own_default(self):
        source = 'stop_dollars = input.int(500, "Stop ($)")\ntp_dollars = input.int(500, "Take Profit ($)")\n'
        merged = merge_risk(None, "MNQ", source)
        self.assertNotIn("stop_dollars", merged)
        self.assertNotIn("tp_dollars", merged)
        self.assertEqual(merged["pv"], 2.0)

    def test_an_attempt_still_moves_a_declared_stop(self):
        source = 'stop_dollars = input.int(500, "Stop ($)")\n'
        self.assertEqual(merge_risk({"stop_dollars": 600}, "ES", source)["stop_dollars"], 600)

    def test_a_script_without_the_input_gets_the_desk_default(self):
        self.assertEqual(merge_risk(None, "ES", "x = 1\n")["stop_dollars"], 700.0)
        self.assertEqual(merge_risk(None, "ES")["stop_dollars"], 700.0)

    def test_declared_inputs_reads_typed_and_untyped_declarations(self):
        source = 'pv = input.float(2, "PV")\nint cooldown = input.int(6, "Cooldown")\nlen = input(14)\nx = 3\n'
        self.assertEqual(declared_inputs(source), {"pv", "cooldown", "len"})

    def test_an_input_named_by_its_label_reaches_the_script(self):
        from futuresfund.backtrader_engine import input_names
        from futuresfund.strategy import _apply_inputs

        source = 'ema55_len = input.int(55, "55 EMA Length", minval=1)\nstop_dollars = input.int(250, title="Stop ($)")\n'
        named = input_names(source, {"55 EMA Length": 110, "Stop ($)": 500, "pv": 2})
        self.assertEqual(named, {"ema55_len": 110, "stop_dollars": 500, "pv": 2})
        self.assertIn("input.int(110,", _apply_inputs(source, named))


class LibraryCopyTests(unittest.TestCase):
    def test_a_dollar_stop_row_copies_as_a_percent_of_the_account(self):
        from futuresfund.library import _dollars_as_percent

        source = (
            'strategy("S", initial_capital=25000)\n'
            'stop_pct = input.float(0.1, "Stop (% of account)", minval=0.1, maxval=3)\n'
            'tp_pct = input.float(0.2, "Take Profit (% of account)", minval=0.1, maxval=4)\n'
            'stop_dollars = strategy.initial_capital * stop_pct / 100\n'
        )
        self.assertEqual(_dollars_as_percent(source, {"stop_dollars": 750, "tp_dollars": 1000}), {"stop_dollars": 750, "tp_dollars": 1000, "stop_pct": 3.0, "tp_pct": 4.0})

    def test_a_script_with_dollar_inputs_keeps_them(self):
        from futuresfund.library import _dollars_as_percent

        source = 'stop_dollars = input.float(500, "Stop ($)")\n'
        self.assertEqual(_dollars_as_percent(source, {"stop_dollars": 750}), {"stop_dollars": 750})


class ContractNameTests(unittest.TestCase):
    def test_a_dated_contract_export_names_the_expiration(self):
        self.assertEqual(_contract_for("EMA_Night_CME_MINI_MNQZ2026_2026-10-28_e3886"), ("MNQ", "MNQZ6"))
        self.assertEqual(_contract_for("X_CME_MINI_ESH2027_2026-10-28_ab"), ("ES", "ESH7"))

    def test_a_continuous_export_is_not_a_contract(self):
        self.assertIsNone(_contract_for("POC_Confluence_CME_MINI_ES1!_2026-10-02_ea063"))


@unittest.skipUnless(shutil.which("node"), "PineTS needs Node")
class CostTests(unittest.TestCase):
    def test_no_commission_or_slippage_when_the_script_sets_none(self):
        trades = script_trades(ONE_TRADE, _bars([100, 100, 100, 110, 110]), None, "ES", "5m", as_written=True)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_price"], 100)
        self.assertEqual(trades[0]["exit_price"], 110)
        self.assertEqual(trades[0]["pnl"], 500.0)

    def test_a_stop_the_close_already_crossed_fills_on_that_close(self):
        # Orders fill on the close. The stop moved above the close at bar 3 is already crossed for a long.
        source = (
            '//@version=5\n'
            'strategy("Crossed", overlay=true, process_orders_on_close=true, default_qty_type=strategy.fixed, default_qty_value=1)\n'
            'if bar_index == 1\n'
            '    strategy.entry("Long", strategy.long)\n'
            'if bar_index == 3\n'
            '    strategy.exit("XL", "Long", stop=close + 1)\n'
        )
        bars = _bars([100, 100, 104, 106, 90, 90])
        trades = script_trades(source, bars, None, "ES", "5m", as_written=True)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_time"], bars[3].when.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(trades[0]["exit_price"], 106)

    BRACKET = (
        '//@version=5\n'
        'strategy("Bracket", overlay=true, process_orders_on_close=true, default_qty_type=strategy.fixed, default_qty_value=1)\n'
        'if bar_index == 1\n'
        '    strategy.entry("E", {side})\n'
        '    strategy.exit("X", "E", stop={stop}, limit={limit})\n'
    )

    def _bracket(self, side, stop, limit, exit_bar):
        bars = [Bar(1_788_307_200, 100, 101, 99, 100, 10), Bar(1_788_307_500, 100, 101, 99, 100, 10)]
        bars += [Bar(1_788_307_800, *exit_bar, 10), Bar(1_788_308_100, 100, 101, 99, 100, 10)]
        source = self.BRACKET.format(side=side, stop=stop, limit=limit)
        trades = script_trades(source, bars, None, "ES", "5m", as_written=True)
        self.assertEqual(len(trades), 1)
        return trades[0]

    def test_a_bar_that_opens_past_the_stop_fills_the_stop_at_the_open(self):
        # The bar then reaches the target as well. TradingView still stops out at the open.
        trade = self._bracket("strategy.short", "close + 1", "close - 2", (102, 108, 97, 99))
        self.assertEqual(trade["exit_price"], 102)
        self.assertEqual(trade["pnl"], -100.0)

    def test_an_open_halfway_between_the_high_and_low_goes_to_the_low_first(self):
        trade = self._bracket("strategy.long", "close - 1", "close + 2", (100, 103, 97, 100))
        self.assertEqual(trade["exit_price"], 99)
        trade = self._bracket("strategy.short", "close + 1", "close - 2", (100, 103, 97, 100))
        self.assertEqual(trade["exit_price"], 98)

    def test_the_script_s_own_commission_and_slippage_are_charged(self):
        source = ONE_TRADE.replace(
            "default_qty_value=1)",
            "default_qty_value=1, commission_type=strategy.commission.cash_per_contract, commission_value=5.5, slippage=1)",
        )
        trades = script_trades(source, _bars([100, 100, 100, 110, 110]), None, "ES", "5m", as_written=True)
        self.assertEqual(len(trades), 1)
        # One tick against each fill is 0.5 points, $25, and $11 of commission.
        self.assertEqual(trades[0]["pnl"], 464.0)


@unittest.skipUnless(shutil.which("node"), "PineTS needs Node")
class ScriptErrorTests(unittest.TestCase):
    BAD_SESSION = (
        '//@version=5\n'
        'strategy("Bad session", overlay=true)\n'
        't = time(timeframe.period, "America/New_York")\n'
        'if bar_index == 1 and not na(t)\n'
        '    strategy.entry("Long", strategy.long)\n'
    )

    def test_an_error_inside_the_script_is_not_an_engine_fault(self):
        from futuresfund.backtrader_engine import execute_script

        bars = [{"t": 1_788_307_200 + k * 300, "o": 100, "h": 101, "l": 99, "c": 100, "v": 10} for k in range(100)]
        measured = execute_script(self.BAD_SESSION, bars, "5m", None, "ES", "Bad session", 1, full=True)
        self.assertTrue(measured["fatal"])
        self.assertTrue(measured["script_error"])
        self.assertIn("Invalid session", measured["error"])

    def test_the_search_sets_aside_a_script_whose_own_settings_raise(self):
        from futuresfund.learn import _search

        calls = []

        def execute(plan):
            calls.append(plan)
            return {"error": "The script stopped in PineTS: Invalid session specification", "fatal": True, "script_error": True, "net_profit": None, "max_drawdown": None, "trades": 0}

        import tempfile

        with tempfile.TemporaryDirectory() as tmp, mock.patch("futuresfund.errors.JOURNAL", Path(tmp) / "errors.json"), mock.patch(
            "futuresfund.risk_style.SUGGESTIONS_PATH", Path(tmp) / "risk_suggestions.json"
        ):
            trials = _search({"stop_dollars": 500, "ema_len": 20}, execute, 50000, 20, None, None, None)
        self.assertEqual(len(calls), 1)
        self.assertIn("Invalid session", trials[-1]["unrunnable"])
        self.assertNotIn("engine_fault", trials[-1])


@unittest.skipUnless(shutil.which("node"), "PineTS needs Node")
class PivotTests(unittest.TestCase):
    # A pivot is confirmed two bars after it. Its entry fills on that close and is closed on the next one.
    PIVOTS = (
        '//@version=5\n'
        'strategy("Pivots", overlay=true, process_orders_on_close=true, default_qty_type=strategy.fixed, default_qty_value=1)\n'
        'ph = ta.pivothigh(high, 2, 2)\n'
        'pl = ta.pivotlow(low, 2, 2)\n'
        'if strategy.position_size != 0\n'
        '    strategy.close_all()\n'
        'else if not na(ph)\n'
        '    strategy.entry("High", strategy.short)\n'
        'else if not na(pl)\n'
        '    strategy.entry("Low", strategy.long)\n'
    )

    @staticmethod
    def _walk(count):
        import random

        pick = random.Random(5)
        bars, price = [], 100
        for k in range(count):
            close = price + pick.choice((-2, -1, 0, 0, 1, 2))
            # Whole points, so equal highs and lows are common.
            bars.append(Bar(1_788_307_200 + k * 300, price, max(price, close) + pick.choice((0, 0, 1, 2)), min(price, close) - pick.choice((0, 0, 1, 2)), close, 10))
            price = close
        return bars

    @staticmethod
    def _pivot(values, bar, high):
        # A higher bar on the left or an equal or higher bar on the right rules it out. Lows mirror that.
        if bar < 4:
            return False
        middle = values[bar - 2]
        left = [values[bar - 3], values[bar - 4]]
        right = [values[bar - 1], values[bar]]
        if high:
            return all(value <= middle for value in left) and all(value < middle for value in right)
        return all(value >= middle for value in left) and all(value > middle for value in right)

    def test_each_pivot_is_found_on_the_bar_it_is_confirmed(self):
        bars = self._walk(600)
        highs, lows = [bar.h for bar in bars], [bar.l for bar in bars]
        expected, holding = [], False
        for k in range(len(bars)):
            if holding:
                holding = False
            elif self._pivot(highs, k, True):
                expected.append(("short", k))
                holding = True
            elif self._pivot(lows, k, False):
                expected.append(("long", k))
                holding = True
        when = {bar.when.strftime("%Y-%m-%d %H:%M"): k for k, bar in enumerate(bars)}
        # An entry on the last bar is still open, and only closed trades come back.
        expected = [(side, k) for side, k in expected if k < len(bars) - 1]
        trades = script_trades(self.PIVOTS, bars, None, "ES", "5m", as_written=True)
        self.assertGreater(len(expected), 50)
        self.assertEqual([(trade["side"], when[trade["entry_time"]]) for trade in trades], expected)

    def test_the_desk_copy_of_pinets_checks_one_bar_for_a_pivot(self):
        import re

        from futuresfund.pinets_engine import RUNNER

        script_trades(self.PIVOTS, self._walk(20), None, "ES", "5m", as_written=True)
        version = re.search(r"const DESK_VERSION = (\d+);", RUNNER.read_text(encoding="utf-8")).group(1)
        bundle = (ROOT / ".tools" / "pinets" / "node_modules" / "pinets" / "dist" / f"pinets.desk{version}.es.js").read_text(encoding="utf-8")
        self.assertIn("__deskPivotAt(o,s,a,c,!0)", bundle)
        self.assertIn("__deskPivotAt(o,s,a,c,!1)", bundle)
        self.assertNotIn("l=Cy(o,s,a)", bundle)


@unittest.skipUnless(shutil.which("node") and MNQ_EXPORT.is_file() and MNQ_MINUTES.is_file(), "needs the MNQZ2026 export and the MNQ download")
class MnqExportTests(unittest.TestCase):
    def test_every_covered_trade_and_its_profit_match_the_tester(self):
        from futuresfund.tv_checks import check_export

        report = check_export(MNQ_EXPORT)
        self.assertNotIn("error", report)
        self.assertEqual(report["contract"], "MNQZ6")
        self.assertEqual(report["timeframe"], "5m")
        self.assertGreaterEqual(report["covered"], 49)
        self.assertEqual(report["matched"], report["covered"])
        self.assertEqual(report["extra"], 0)
        self.assertEqual(report["engine_covered_profit"], report["covered_profit"])

    def test_research_settings_take_the_same_trades_as_the_tester(self):
        from futuresfund.backtrader_engine import _bars_from, load_tv_trades
        from futuresfund.research_bars import contract_series

        source = (ROOT / "futuresfund" / "checkScripts" / "ema_structure_rsi_pullback_night_nmq.pine").read_text(encoding="utf-8")
        bars = _bars_from(contract_series("MNQZ6", "5m"))
        last = bars[-1].when.strftime("%Y-%m-%d %H:%M")
        tv = [trade for trade in load_tv_trades(MNQ_EXPORT) if trade["exit_time"] <= last]
        # This export was run before the stop was held at entry, so the desk's old exit lines are what it measured.
        moving = (
            ("long_stop", "long_stop = close - stop_dollars / pv"),
            ("long_limit", "long_limit = close + tp_dollars / pv"),
            ("short_stop", "short_stop = close + stop_dollars / pv"),
            ("short_limit", "short_limit = close - tp_dollars / pv"),
        )
        with mock.patch("futuresfund.strategy_team._EXIT_PRICE_LINES", moving), mock.patch("futuresfund.strategy_team._exit_on_entry_bar", lambda text: text):
            engine = script_trades(source, bars, None, "MNQ", "5m")
        self.assertEqual(
            [(t["entry_time"], t["exit_time"], t["exit_signal"], t["pnl"]) for t in engine],
            [(t["entry_time"], t["exit_time"], t["exit_signal"], t["pnl"]) for t in tv],
        )


if __name__ == "__main__":
    unittest.main()


class OwnDollarExitTests(unittest.TestCase):
    """A script that brings its own dollar stop and open-loss close is backtested with that exit as written."""

    def test_the_mnq_pullback_exit_is_not_rewritten(self):
        from futuresfund.backtrader_engine import script_as_run

        source = (ROOT / "futuresfund" / "checkScripts" / "ema_structure_rsi_pullback_night_nmq.pine").read_text(encoding="utf-8")
        run = script_as_run(source, None, "MNQ")
        # TradingView moves this stop with each close and sends the exit only once the position is open.
        self.assertIn("long_stop = close - stop_dollars / pv", run)
        self.assertIn("short_stop = close + stop_dollars / pv", run)
        self.assertNotIn("position_avg_price", run)
        self.assertEqual(run.count("strategy.exit("), source.count("strategy.exit("))

    @unittest.skipUnless(shutil.which("node") and (ROOT / ".tools" / "pinets").exists(), "PineTS is not installed")
    def test_the_research_run_takes_the_strategy_tester_trades(self):
        from futuresfund.backtrader_engine import _bars_from, load_tv_trades
        from futuresfund.csv_bars import parse_tradingview_csv

        chart = ROOT / "chartData" / "mnq" / "CME_MINI_MNQ1!, 5_43a1f.csv"
        if not chart.exists():
            self.skipTest("The MNQ 5-minute chart is not on this computer.")
        source = (ROOT / "futuresfund" / "checkScripts" / "ema_structure_rsi_pullback_night_nmq.pine").read_text(encoding="utf-8")
        bars = _bars_from(parse_tradingview_csv(chart.read_text(encoding="utf-8")))
        # The chart starts on 2026-09-11; three days warm the 50 EMA before the comparison.
        start, end = "2026-09-14", "2026-10-28 23:59"

        def key(trade):
            return (trade["entry_time"], trade["side"], round(float(trade["entry_price"]), 2), trade["exit_time"], round(float(trade["exit_price"]), 2), trade["exit_signal"])

        tv = {key(trade) for trade in load_tv_trades(MNQ_EXPORT) if start <= trade["entry_time"] <= end}
        for as_written in (True, False):
            trades = script_trades(source, bars, None, "MNQ", "5m", as_written=as_written)
            engine = {key(trade) for trade in trades if start <= trade["entry_time"] <= end}
            self.assertGreaterEqual(len(tv), 20)
            self.assertEqual(engine, tv, f"as_written={as_written}")
