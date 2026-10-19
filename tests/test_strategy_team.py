"""The creation team turns measured changes into one new Pine script."""

import json
import tempfile
import unittest
from pathlib import Path

from futuresfund.strategy_team import (
    baseline_ready,
    build_cards,
    ensure_risk_management,
    extract_pine,
    finished_chart_studies,
    measured_cards,
    pine_problems,
    repair_pine,
    script_name,
)


class StrategyTeamTests(unittest.TestCase):
    def test_a_measured_change_becomes_a_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learning"
            notes = root / "notes"
            learning.mkdir()
            notes.mkdir()
            (learning / "ema_pullback.pine").write_text("//@version=5\nstrategy(\"EMA\")\n", encoding="utf-8")
            (learning / "created_other.pine").write_text("//@version=5\n", encoding="utf-8")
            (notes / "ema_pullback.json").write_text(json.dumps({
                "title": "EMA Pullback",
                "instruments": {
                    "ES": {
                        "trials": [
                            {"change": "Original", "net_profit": 1, "max_drawdown": 2, "trades": 3},
                            {
                                "change": "cooldown_bars 5 to 10",
                                "impact": "cooldown_bars made the book steadier.",
                                "net_profit": 4,
                                "max_drawdown": 2,
                                "trades": 3,
                            },
                        ]
                    }
                },
            }), encoding="utf-8")
            cards = build_cards(learning, notes)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "EMA Pullback")
        self.assertEqual(measured_cards(cards)[0]["changes"][0]["change"], "cooldown_bars 5 to 10")

    def test_a_script_needs_a_version_line_an_entry_and_an_exit(self):
        self.assertEqual(pine_problems("strategy.entry(\"L\", strategy.long)"), [
            "The version line is missing.",
            "strategy() is missing.",
            "An exit is missing.",
            "The trade is not closed at the session end. Close the open trade with strategy.close_all when in_session is false.",
            "A protective stop is missing. Every entry needs strategy.exit with stop= or loss= so the drawdown is capped.",
        ])
        unprotected = "//@version=5\nstrategy(\"Night\")\nstrategy.entry(\"L\", strategy.long)\nstrategy.close(\"L\")\n"
        self.assertEqual(
            pine_problems(unprotected),
            [
                "The trade is not closed at the session end. Close the open trade with strategy.close_all when in_session is false.",
                "A protective stop is missing. Every entry needs strategy.exit with stop= or loss= so the drawdown is capped.",
            ],
        )
        source = (
            "//@version=5\nstrategy(\"Night\")\nstrategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 10, limit=close + 20)\n"
            "inSession = not na(time(timeframe.period, \"1800-1645:12345\", \"America/New_York\"))\n"
            "if not inSession\n    strategy.close_all(comment=\"Session\")\n"
        )
        self.assertEqual(pine_problems(source), [])
        nonsense = (
            '//@version=5\nstrategy("Night")\n'
            'pv = input.float(20, "Point Value ($)", group="Risk")\n'
            'stop_dollars = input.float(0.0002, "Stop ($)", minval=50, step=25, group="Risk")\n'
            'tp_dollars = input.float(10847.1007, "Take Profit ($)", minval=50, step=50, group="Risk")\n'
            'strategy.entry("Long", strategy.long)\n'
            'strategy.exit("XL", "Long", stop=close - stop_dollars / pv, limit=close + tp_dollars / pv)\n'
        )
        found = pine_problems(nonsense)
        self.assertTrue(any("one tick" in item for item in found))
        self.assertTrue(any("session" in item for item in found))
        sensible = nonsense.replace("0.0002", "100").replace("10847.1007", "1200")
        sensible += (
            "\ninSession = not na(time(timeframe.period, \"1800-1645:12345\", \"America/New_York\"))\n"
            "if not inSession\n    strategy.close_all(comment=\"Session\")\n"
        )
        self.assertEqual(pine_problems(sensible), [])
        reply = "From the EMA card.\n```pine\n" + source + "```"
        self.assertTrue(extract_pine(reply).startswith("//@version=5"))

    def test_maxmax_and_a_valuewhen_assignment_are_repaired(self):
        broken = "\n".join([
            "//@version=5",
            'strategy("POC RSI Momentum v1", overlay=true)',
            'rsi_len = input.int(14, "RSI Length", minval=2, maxmax=5.0)',
            "pivot_high = ta.pivothigh(high, 5, 5)",
            "pivot_low = ta.pivotlow(low, 5, 5)",
            "breakout = close > ta.valuewhen(pivot_high = pivot_high, high, 1)",
            "pullback = close < ta.valuewhen(pivot_low = pivot_low, low, 1)",
            'strategy.entry("Long", strategy.long)',
            'strategy.exit("XL", from_entry="Long", stop=close - 1, limit=close + 2)',
        ])
        self.assertTrue(any("maxmax" in item for item in pine_problems(broken)))
        self.assertTrue(any("valuewhen" in item for item in pine_problems(broken)))
        fixed = repair_pine(broken)
        self.assertIn('maxval=5.0', fixed)
        self.assertNotIn("maxmax", fixed)
        self.assertIn("ta.valuewhen(not na(pivot_high), high, 1)", fixed)
        self.assertIn("ta.valuewhen(not na(pivot_low), low, 1)", fixed)
        fixed += (
            "\ninSession = not na(time(timeframe.period, \"1800-1645:12345\", \"America/New_York\"))\n"
            "if not inSession\n    strategy.close_all(comment=\"Session\")\n"
        )
        self.assertEqual(pine_problems(fixed), [])

    def test_a_script_is_rewritten_onto_the_shared_risk_block(self):
        from futuresfund.pine_runtime import compile_pine

        source = "\n".join([
            "//@version=5",
            'strategy("Engine4_VWMA_NightDelta_v2", overlay=true, initial_capital=25000)',
            'vwma_len = input.int(20, "VWMA Length", minval=1, maxval=100)',
            'tp_dollars = input.float(1500, "Take Profit ($)", minval=100, maxval=2000)',
            'stop_dollars = input.float(1000, "Stop Loss ($)", minval=100, maxval=1800)',
            "// Point Value for NQ (20), ES (100), GC (12.5) - Defaulting to NQ logic for safety",
            'pv = input.float(20, "Point Value", minval=1, maxval=100)',
            "vwap = ta.vwma(close, vwma_len)",
            'before1645 = not na(time(timeframe.period, "1800-1645", "America/New_York"))',
            "long_stop = close - stop_dollars / pv",
            "long_tp = close + tp_dollars / pv",
            'if before1645',
            '    strategy.entry("Long", strategy.long)',
            '    strategy.exit("Long_Exit", "Long", stop=long_stop, limit=long_tp)',
            "if not before1645",
            '    strategy.close_all(comment="Session")',
            "",
        ])
        fixed = ensure_risk_management(source)
        self.assertIn("// ===== RISK =====", fixed)
        self.assertNotIn("// ===== RISK Management=====", fixed)
        self.assertIn('pv                = input.float(50, "Point Value ($)")', fixed)
        self.assertIn('stop_dollars      = input.int(250, "Stop ($)", minval=250, maxval=1500)', fixed)
        self.assertIn('tp_dollars        = input.int(500, "Take Profit ($)", minval=250, maxval=2000)', fixed)
        self.assertNotIn("stop_pct", fixed)
        self.assertNotIn("tp_pct", fixed)
        self.assertIn("initial_capital=50000", fixed)
        self.assertNotIn("initial_capital=25000", fixed)
        self.assertIn('use_trail         = input.bool(false, "Enable Trailing Stop")', fixed)
        self.assertIn('trail_min_dollars = input.int(1000, "Trail: Min Profit ($)", minval=0, maxval=2000)', fixed)
        self.assertNotIn("strategy.initial_capital *", fixed)
        self.assertNotIn("syminfo.pointvalue", fixed)
        self.assertNotIn("maxval=1800", fixed)
        self.assertNotIn("minval=100, maxval=2000", fixed)
        self.assertNotIn("stop_cash", fixed)
        self.assertNotIn("tp_cash", fixed)
        self.assertNotIn("ES (100)", fixed)
        self.assertEqual(fixed.count("stop_dollars"), 5)
        self.assertIn("long_stop = close - stop_dollars / pv", fixed)
        self.assertIn("long_limit = close + tp_dollars / pv", fixed)
        self.assertIn("short_stop = close + stop_dollars / pv", fixed)
        self.assertIn("short_limit = close - tp_dollars / pv", fixed)
        self.assertIn("if strategy.position_size != 0 and not na(stop_dollars)", fixed)
        self.assertIn("if strategy.openprofit <= -stop_dollars", fixed)
        self.assertIn('strategy.close_all(comment="Open P&L > SL Risk")', fixed)
        self.assertIn('strategy.exit("Long_Exit", "Long", stop=long_stop, limit=long_limit)', fixed)
        self.assertNotIn("limit=long_tp", fixed)
        self.assertEqual(fixed.count('input.float(50, "Point Value ($)")'), 1)
        self.assertEqual(ensure_risk_management(fixed), fixed)
        compile_pine(fixed)
        self.assertEqual(pine_problems(fixed), [])
        atr = fixed.replace(
            'stop_dollars      = input.int(250, "Stop ($)", minval=250, maxval=1500)',
            'atr_mult_sl = input.float(5, "SL Multiplier", minval=1.0, maxval=5.0)\n'
            'atr_mult_tp = input.float(2.5, "TP Multiplier", minval=1.0, maxval=10.0)',
        )
        self.assertTrue(any("not accepted" in problem for problem in pine_problems(atr)))
        pasted = "\n".join([
            'atr_len = input.int(14, "ATR Length", minval=5, maxval=50)',
            'stop_mult = input.float(3, "Stop Mult", minval=1.0, maxval=10.0)',
            'tp_mult = input.float(4, "TP Mult", minval=1.0, maxval=10.0)',
            "stop_price = close - (atr * stop_mult)",
        ])
        from futuresfund.strategy_team import atr_exit
        self.assertIn("atr * stop_mult", atr_exit(pasted))

    def test_a_script_that_already_stops_on_dollars_is_not_given_a_second_block(self):
        source = "\n".join([
            "//@version=5",
            'strategy("Held", overlay=true, initial_capital=25000)',
            'pv = input.float(20, "Point Value ($)")',
            'stop_pct = input.float(1, "Stop (% of account)", minval=0.1, maxval=3)',
            "stop_dollars = strategy.initial_capital * stop_pct / 100",
            'stop_dollars = input.int(750, "Stop ($)", minval=250, maxval=1500)',
            'tp_dollars = input.int(1250, "Take Profit ($)", minval=250, maxval=2000)',
            "if strategy.position_size != 0 and not na(stop_dollars)",
            "    if strategy.openprofit <= -stop_dollars",
            '        strategy.close_all(comment="Open P&L > SL Risk")',
            "if in_session",
            '    strategy.entry("L", strategy.long)',
            '    strategy.exit("X", "L", stop=close - stop_dollars / pv, limit=close + tp_dollars / pv)',
            "",
        ])
        fixed = ensure_risk_management(source, 50)
        self.assertEqual(fixed.count('comment="Open P&L > SL Risk"'), 1)
        self.assertEqual(fixed.count('input.int(250, "Stop ($)"'), 0)
        self.assertIn('stop_dollars = input.int(750, "Stop ($)", minval=250, maxval=1500)', fixed)
        self.assertIn('tp_dollars = input.int(1250, "Take Profit ($)", minval=250, maxval=2000)', fixed)
        self.assertNotIn("stop_pct", fixed)
        self.assertNotIn("strategy.initial_capital *", fixed)
        self.assertIn("initial_capital=50000", fixed)
        self.assertIn('pv = input.float(50, "Point Value ($)")', fixed)
        self.assertEqual(fixed.count('input.float(50, "Point Value ($)")'), 1)
        self.assertIn("long_stop = close - stop_dollars / pv", fixed)
        self.assertIn("short_limit = close - tp_dollars / pv", fixed)
        self.assertIn('strategy.exit("X", "L", stop=long_stop, limit=long_limit)', fixed)
        self.assertNotIn("stop=close - stop_dollars / pv", fixed)
        self.assertEqual(ensure_risk_management(fixed, 50), fixed)
        short = "\n".join([
            "//@version=5",
            'strategy("Held short", overlay=true, initial_capital=50000)',
            'pv = input.float(20, "Point Value ($)")',
            'stop_dollars = input.int(500, "Stop ($)", minval=250, maxval=1500)',
            'tp_dollars = input.int(1000, "Take Profit ($)", minval=250, maxval=2000)',
            "if strategy.position_size != 0 and not na(stop_dollars)",
            "    if strategy.openprofit <= -stop_dollars",
            '        strategy.close_all(comment="Open P&L > SL Risk")',
            'strategy.exit("ExitShort", "Short", stop=close + 4, limit=close - 8)',
            "",
        ])
        wired = ensure_risk_management(short, 20)
        self.assertIn('stop_dollars = input.int(500, "Stop ($)", minval=250, maxval=1500)', wired)
        self.assertIn('strategy.exit("ExitShort", "Short", stop=short_stop, limit=short_limit)', wired)
        self.assertEqual(wired.count('comment="Open P&L > SL Risk"'), 1)
        self.assertEqual(ensure_risk_management(wired, 20), wired)

    def test_an_es_backtest_uses_50_and_the_session_inputs(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from futuresfund.backtrader_engine import Bar
        from futuresfund.pine_runtime import run_pine_live
        from futuresfund.risk_style import merge_risk
        from futuresfund.strategy_team import prepare_backtest

        source = "\n".join([
            "//@version=5",
            'strategy("Night", overlay=true, initial_capital=50000, process_orders_on_close=true)',
            'pv = input.float(20, "Point Value ($)")',
            'before1645 = not na(time(timeframe.period, "1800-1645:12345", "America/New_York"))',
            "if before1645 and pv == 50",
            '    strategy.entry("Long", strategy.long)',
            '    strategy.exit("XL", from_entry="Long", stop=close - 10, limit=close + 10)',
            "if not before1645",
            '    strategy.close_all(comment="Session")',
            "",
        ])
        es = prepare_backtest(source, "ES")
        nq = prepare_backtest(source, "NQ")
        self.assertIn('use_session   = input.bool(true, "Use Session Filter")', es)
        self.assertIn('sess_start_hr = input.int(18, "Session Start Hour (ET)")', es)
        self.assertIn('sess_start_mn = input.int(0,  "Session Start Minute")', es)
        self.assertIn('sess_end_hr   = input.int(16,  "Session End Hour (ET)")', es)
        self.assertIn('sess_end_mn   = input.int(0,  "Session End Minute")', es)
        self.assertNotIn(":12345", es)
        self.assertNotIn("before1645", es)
        self.assertIn('input.float(50, "Point Value ($)")', es)
        self.assertIn('input.float(20, "Point Value ($)")', nq)
        self.assertIn('input.float(100, "Point Value ($)")', prepare_backtest(source, "GC"))
        self.assertEqual(prepare_backtest(es, "ES"), es)
        et = ZoneInfo("America/New_York")

        def bars_at(*hours):
            found = []
            for hour, minute in hours:
                moment = datetime(2026, 10, 9, hour, minute, tzinfo=et)
                found.append(Bar(int(moment.timestamp()), 5000, 5002, 4998, 5001, 10))
            return found

        friday = run_pine_live(es, bars_at((12, 0), (12, 5), (12, 10)), merge_risk({}, "ES"), root="ES", timeframe="5m")
        self.assertIsNotNone(friday["open"])
        shut = run_pine_live(es, bars_at((16, 30), (16, 35), (16, 40)), merge_risk({}, "ES"), root="ES", timeframe="5m")
        self.assertIsNone(shut["open"])
        self.assertEqual(shut["trades"], [])
        nq_run = run_pine_live(nq, bars_at((12, 0), (12, 5), (12, 10)), merge_risk({}, "NQ"), root="NQ", timeframe="5m")
        self.assertIsNone(nq_run["open"])

    def test_a_first_pass_needs_a_trade_list_and_a_clean_compile(self):
        self.assertFalse(baseline_ready([{"fatal": True, "error": "compile", "trades": 0}]))
        self.assertFalse(baseline_ready([{"trades": 0, "net_profit": 0}]))
        self.assertTrue(baseline_ready([
            {"timeframe": "5m", "trades": 0, "net_profit": 0},
            {"timeframe": "2m", "trades": 4, "net_profit": 10},
        ]))

    def test_a_new_name_does_not_replace_an_existing_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "created_night.pine").write_text("//@version=5\n", encoding="utf-8")
            self.assertEqual(script_name("Night", folder), "created_night_2.pine")

    def test_a_script_is_ready_only_after_all_three_charts_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learning"
            notes = root / "notes"
            learning.mkdir()
            notes.mkdir()
            (learning / "day.pine").write_text("//@version=5\n", encoding="utf-8")
            (learning / "night.pine").write_text("//@version=5\n", encoding="utf-8")
            done = {"attempts": 1, "best": {"net_profit": 1}, "trials": [{}]}
            (notes / "day.json").write_text(json.dumps({
                "instruments": {"ES": {"charts": {"2m": done, "5m": done, "15m": done}}},
            }), encoding="utf-8")
            (notes / "night.json").write_text(json.dumps({
                "instruments": {"ES": {"charts": {"2m": done, "5m": done}}},
            }), encoding="utf-8")
            self.assertEqual(finished_chart_studies(learning, notes), ["day.pine"])

    def test_one_finished_chart_is_enough_for_that_charts_developer(self):
        from futuresfund.strategy_team import finished_on_chart

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning = root / "learning"
            notes = root / "notes"
            learning.mkdir()
            notes.mkdir()
            (learning / "day.pine").write_text("//@version=5\n", encoding="utf-8")
            done = {"attempts": 1, "best": {"net_profit": 1}, "trials": [{}]}
            (notes / "day.json").write_text(json.dumps({
                "instruments": {"ES": {"charts": {"15m": done}}},
            }), encoding="utf-8")
            self.assertEqual(finished_on_chart("15m", learning, notes), ["day.pine"])
            self.assertEqual(finished_on_chart("2m", learning, notes), [])

    def test_a_chart_with_a_pending_script_does_not_ask_for_a_new_one(self):
        from unittest import mock

        from futuresfund.learn import chart_needs_a_script, clear_holds, hold_script

        with tempfile.TemporaryDirectory() as tmp:
            learning = Path(tmp)
            (learning / "waiting.pine").write_text("//@version=5\nstrategy(\"Waiting\")\n", encoding="utf-8")
            with mock.patch("futuresfund.learn.research_instruments", lambda: ["ES"]):
                clear_holds()
                self.assertFalse(chart_needs_a_script("15m", learning))
                hold_script(learning / "waiting.pine", "15m")
                self.assertTrue(chart_needs_a_script("15m", learning))
                clear_holds()

    def test_the_creation_search_stops_when_the_result_is_profitable(self):
        from futuresfund.learn import _search

        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            profit = 25 if calls["n"] >= 3 else -10
            return {"net_profit": profit, "max_drawdown": 4, "trades": 2, "runner": "backtrader", "engine": "backtrader"}

        trials = _search({"length": 10}, execute, 50000, 20, None, None, None, until_profitable=True)
        self.assertGreaterEqual(calls["n"], 3)
        self.assertLess(len(trials), 20)
        self.assertGreater(trials[-1]["net_profit"], 0)

    def test_a_held_strategy_team_does_not_write_or_develop_a_new_script(self):
        import threading
        from unittest.mock import patch

        from futuresfund import strategy_team
        from futuresfund.board import Board

        board = Board()
        board.set_hold_new(True)
        pause = threading.Event()
        desk = {"strategy_developer": "2min strategy developer", "label": "2-minute"}
        with patch.object(strategy_team, "handoff", side_effect=AssertionError("wrote a script")), patch.object(
            strategy_team, "develop_one", side_effect=AssertionError("developed a script")
        ):
            writer = threading.Thread(target=strategy_team.run_chart_creation, args=(board, pause, desk), daemon=True)
            engine = threading.Thread(target=strategy_team.run_creation_engine, args=(board, pause), daemon=True)
            writer.start()
            engine.start()
            writer.join(0.4)
            engine.join(0.4)
            board.cancel.set()
            writer.join(3)
            engine.join(3)
        self.assertFalse(writer.is_alive())
        self.assertFalse(engine.is_alive())
        texts = [item["text"] for item in board.snapshot()["messages"]]
        self.assertTrue(any("not writing a new strategy" in text for text in texts))
        self.assertTrue(any("not developing a new script" in text for text in texts))


class CreationPipelineTests(unittest.TestCase):
    """The team is trained on the finished studies and judged on drawdown, not profit alone."""

    def test_the_target_needs_the_profit_target_the_pace_and_the_drawdown_inside_the_trail(self):
        from futuresfund.exemplars import drawdown_cap, meets_target, profit_target

        cap = drawdown_cap()
        target = profit_target()
        self.assertGreater(cap, 0)
        self.assertGreater(target, 0)
        good = {"net_profit": target, "max_drawdown": cap, "trades": 24, "days": 20}
        self.assertTrue(meets_target(good))
        self.assertFalse(meets_target({**good, "max_drawdown": cap + 1}))
        self.assertTrue(meets_target({**good, "trades": 40}))
        self.assertFalse(meets_target({**good, "trades": 60}))
        self.assertFalse(meets_target({**good, "trades": 61}))
        self.assertFalse(meets_target({**good, "trades": 2}))
        self.assertFalse(meets_target({**good, "trades": 0}))
        self.assertFalse(meets_target({**good, "net_profit": target - 1}))
        self.assertFalse(meets_target({**good, "breached": True}))
        self.assertFalse(meets_target({**good, "error": "down"}))

    def test_the_creation_search_stops_at_the_target_not_at_the_first_profit(self):
        from futuresfund.exemplars import drawdown_cap, meets_target
        from futuresfund.learn import _search

        cap = drawdown_cap()
        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"net_profit": 500 + calls["n"], "max_drawdown": cap * 3, "trades": 20, "days": 10, "runner": "backtrader", "engine": "backtrader"}
            return {"net_profit": 3 * cap, "max_drawdown": cap * 0.5, "trades": 20, "days": 10, "runner": "backtrader", "engine": "backtrader"}

        trials = _search({"length": 10}, execute, 50000, 20, None, None, None, accept=meets_target)
        self.assertEqual(len(trials), 3)
        self.assertTrue(meets_target(trials[-1]))
        self.assertFalse(meets_target(trials[0]))

    def test_exemplars_are_green_on_two_charts_inside_the_limit_and_cautions_are_not(self):
        from futuresfund import exemplars

        def chart(profit, drawdown, trades=12):
            return {"best": {"net_profit": profit, "max_drawdown": drawdown, "trades": trades, "days": 8, "engine": "backtrader", "params": {"len": 20}}, "trials": []}

        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp)
            (notes / "steady.json").write_text(json.dumps({
                "title": "Steady Pullback",
                "file": "steady.pine",
                "instruments": {"ES": {"charts": {"2m": chart(3000, 900), "5m": chart(2500, 700), "15m": chart(1800, 600)}}},
            }), encoding="utf-8")
            (notes / "wild.json").write_text(json.dumps({
                "title": "Wild Breakout",
                "file": "wild.pine",
                "instruments": {"NQ": {"charts": {"2m": chart(20000, 40000, 300), "5m": chart(-4000, 6000, 80)}}},
            }), encoding="utf-8")
            (notes / "thin.json").write_text(json.dumps({
                "title": "Thin",
                "file": "thin.pine",
                "instruments": {"ES": {"charts": {"5m": chart(900, 100, 2)}}},
            }), encoding="utf-8")
            top = exemplars.exemplars(5, notes)
            self.assertEqual([row["title"] for row in top], ["Steady Pullback"])
            self.assertEqual(top[0]["safe_green"], 3)
            bad = exemplars.cautions(5, notes)
            self.assertEqual([row["title"] for row in bad], ["Wild Breakout"])
            text = exemplars.brief(notes=notes)
        self.assertIn("Creation target", text)
        self.assertIn("Steady Pullback", text)
        self.assertIn("What has not worked", text)
        self.assertIn("Wild Breakout", text)
        self.assertIn("smaller drawdown beats a larger profit", text)

    def test_the_entry_excerpt_keeps_the_conditions_the_entry_reads(self):
        from futuresfund.exemplars import entry_excerpt

        source = (
            "//@version=5\nstrategy(\"X\")\nlen = input.int(20)\nfast = ta.ema(close, len)\n"
            "trend_up = close > fast\ntrigger = ta.crossover(close, fast)\nnoise = ta.rsi(close, 14)\n"
            "if trend_up and trigger\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 10, limit=close + 20)\n"
        )
        excerpt = entry_excerpt(source)
        self.assertIn("trend_up = close > fast", excerpt)
        self.assertIn("trigger = ta.crossover(close, fast)", excerpt)
        self.assertIn("strategy.exit", excerpt)
        self.assertNotIn("noise", excerpt)
        self.assertNotIn("input.int", excerpt)

    def test_the_prompt_carries_the_target_the_libraries_the_cards_and_the_lessons(self):
        from futuresfund.strategy_team import prompt_for

        cards = [{
            "file": "steady.pine", "title": "Steady Pullback", "folder": "learnedStrategies",
            "best": {"net_profit": 2500, "max_drawdown": 700, "trades": 12},
            "changes": [{"change": "cooldown_bars 5 to 10", "impact": "cooldown_bars made the book steadier.", "net_profit": 2500, "max_drawdown": 700, "trades": 12}],
        }]
        lessons = [{"title": "Created EMA Band", "net_profit": -300, "max_drawdown": 1900, "trades": 9, "accepted": False, "built_from": "Steady Pullback, ta.ema", "indicators": ["ta.ema"]}]
        text = prompt_for(cards, lessons)
        self.assertIn("Creation target", text)
        self.assertIn("Strategy library", text)
        self.assertIn("Indicator library", text)
        self.assertIn("Card: Steady Pullback (steady.pine) best profit 2500, drawdown 700, trades 12", text)
        self.assertIn("cooldown_bars 5 to 10", text)
        self.assertNotIn("Created EMA Band", text)
        self.assertNotIn("Lessons from scripts this creation team", text)
        self.assertIn("Not tested on the full dataset", text)
        self.assertIn("75 points", text)
        self.assertIn("reverses the move when one trade loses more than 3 percent of the account", text)
        self.assertIn("more than 900 trades", text)
        self.assertIn("last 7 days", text)
        self.assertIn("atr * stop_mult", text)
        self.assertNotIn("tighter of that ATR distance", text)
        self.assertIn("strategy.exit", text)
        self.assertIn("// Built from:", text)
        self.assertIn("from scratch", text)
        self.assertIn("Engine 4", text)
        self.assertIn("no trail_ arguments", text)
        self.assertIn('use_session = input.bool(true, "Use Session Filter")', text)
        self.assertIn('sess_end_mn = input.int(0, "Session End Minute")', text)
        self.assertIn("On ES the point value is 50. On NQ it is 20. On GC it is 100.", text)
        self.assertNotIn("1800-1645:12345", text)
        self.assertNotIn("Friday 16:45 until Sunday 18:00", text)
        self.assertIn("strategy.close_all(comment=\"Session\")", text)

    def test_cards_come_from_the_learned_folder_too_and_steadier_changes_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning, learned, notes = root / "learning", root / "learned", root / "notes"
            for folder in (learning, learned, notes):
                folder.mkdir()
            (learning / "new.pine").write_text("//@version=5\n", encoding="utf-8")
            (learned / "done.pine").write_text("//@version=5\n", encoding="utf-8")
            (notes / "done.json").write_text(json.dumps({
                "title": "Done",
                "best": {"net_profit": 4000, "max_drawdown": 800, "trades": 20},
                "instruments": {"ES": {"trials": [
                    {"change": "Starting settings", "net_profit": 1, "max_drawdown": 2, "trades": 3},
                    {"change": "len 10 to 20", "impact": "len made the book jumpier.", "net_profit": 900, "max_drawdown": 1500, "trades": 10},
                    {"change": "cooldown 5 to 9", "impact": "cooldown made the book steadier.", "net_profit": 1200, "max_drawdown": 400, "trades": 9},
                ]}},
            }), encoding="utf-8")
            cards = build_cards(learning, notes, learned)
        self.assertEqual([card["file"] for card in cards], ["done.pine", "new.pine"])
        self.assertEqual(cards[0]["changes"][0]["change"], "cooldown 5 to 9")
        self.assertEqual(cards[0]["best"]["net_profit"], 4000)

    def test_a_change_that_loses_more_than_1800_on_one_trade_is_not_a_training_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learning, learned, notes = root / "learning", root / "learned", root / "notes"
            for folder in (learning, learned, notes):
                folder.mkdir()
            (learned / "wide.pine").write_text("//@version=5\n", encoding="utf-8")
            (notes / "wide.json").write_text(json.dumps({
                "title": "Wide Stop",
                "best": {"net_profit": 6712.5, "max_drawdown": 5787.5, "trades": 144},
                "instruments": {"NQ": {"trials": [
                    {"change": "stop_mult 3.125 to 3.75", "net_profit": 89430.0, "max_drawdown": 29725.0, "trades": 148, "biggest_loser": -10235.0},
                    {"change": "tp_mult 2 to 3", "net_profit": 4200.0, "max_drawdown": 1800.0, "trades": 140, "biggest_loser": -900.0},
                ]}},
            }), encoding="utf-8")
            cards = build_cards(learning, notes, learned)
        changes = [item["change"] for item in cards[0]["changes"]]
        self.assertIn("tp_mult 2 to 3", changes)
        self.assertNotIn("stop_mult 3.125 to 3.75", changes)

    def test_a_promoted_script_stays_in_created_strategies_with_the_accepted_settings(self):
        from futuresfund.strategy_team import promote

        source = (
            "//@version=5\nstrategy(\"Band Pullback\")\nlen = input.int(20, minval=5, maxval=200)\n"
            "stop_pts = input.float(8.0, minval=1, maxval=50)\nstrategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - stop_pts)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            created = Path(tmp)
            name = promote(None, "created_band_pullback.pine", source, {"len": 34, "stop_pts": 12.5}, created)
            self.assertEqual(name, "created_band_pullback.pine")
            filed = (created / name).read_text(encoding="utf-8")
            self.assertIn("len = input.int(34,", filed)
            self.assertIn("stop_pts = input.float(12.5,", filed)
            again = promote(None, "created_band_pullback.pine", source, {"len": 40}, created)
            self.assertEqual(again, "created_band_pullback.pine")
            self.assertEqual(len(list(created.glob("*.pine"))), 1)
            self.assertIn("len = input.int(40,", (created / name).read_text(encoding="utf-8"))

    def test_a_round_writes_tests_confirms_promotes_and_records_the_lesson(self):
        from unittest import mock

        from futuresfund import strategy_team
        from futuresfund.board import Board

        script = (
            "//@version=5\n// Built from: Steady Pullback, ta.ema\n"
            "strategy(\"Band Pullback Test\", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1)\n"
            "len = input.int(20, minval=5, maxval=200)\nstop_pts = input.float(8.0, minval=1, maxval=50)\n"
            "fast = ta.ema(close, len)\nif close > fast and ta.crossover(close, fast)\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - stop_pts, limit=close + stop_pts * 2)\n"
        )
        calls = []

        def fake_engine(text, frames, facts, board, instrument="ES", developer="", timeframe="2m", slot=None, full=False):
            def execute(params):
                calls.append((timeframe, dict(params), full))
                base = {"runner": "backtrader", "engine": "backtrader", "timeframe": timeframe, "instrument": "ES", "contract": "ES1!", "days": 10}
                if timeframe == "5m":
                    if sum(1 for chart, *_ in calls if chart == "5m") == 1:
                        return {**base, "net_profit": 2400, "max_drawdown": 600, "trades": 15}
                    return {**base, "net_profit": 3300, "max_drawdown": 600, "trades": 15}
                return {**base, "net_profit": 1500, "max_drawdown": 500, "trades": 10}
            return execute

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created, learning, lessons = root / "createdStrategies", root / "learning", root / "creation_lessons.json"
            with mock.patch.object(strategy_team, "CREATED", created), \
                    mock.patch.object(strategy_team, "LEARNING", learning), \
                    mock.patch.object(strategy_team, "LESSONS_PATH", lessons), \
                    mock.patch.object(strategy_team, "_ask", lambda board, agent, activity, prompt: "```pine\n" + script + "```"), \
                    mock.patch("futuresfund.learn._load_frames", lambda root="ES": {"2m": [], "5m": [], "15m": []}), \
                    mock.patch("futuresfund.learn._engine_execute", fake_engine), \
                    mock.patch("futuresfund.learn._document", lambda *args, **kwargs: None), \
                    mock.patch("futuresfund.learn.file_indicators", lambda *args, **kwargs: []), \
                    mock.patch("futuresfund.backtrader_engine._bars_from", lambda bars: []), \
                    mock.patch("futuresfund.entry_rules.ensure_entry_rule", lambda *args, **kwargs: True):
                board = Board()
                outcome = strategy_team.create_once(board, None, [], "steady.pine")
                self.assertEqual(outcome, "done")
                self.assertTrue((created / "created_band_pullback_test.pine").is_file())
                self.assertEqual(list(learning.glob("*.pine")), [])
                rows = strategy_team.creation_lessons()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["accepted"], rows[0].get("verdict"))
            self.assertEqual(rows[0]["promoted"], "created_band_pullback_test.pine")
            self.assertEqual(rows[0]["green_charts"], 3)
            self.assertEqual(rows[0]["net_profit"], 3300)  # the first 5m run made 2400, under the $3,000 target
            self.assertEqual([chart for chart, _, _ in calls], ["5m", "5m", "2m", "15m"])
            self.assertTrue(all(flag for _, _, flag in calls))
            # The confirmation charts ran the accepted settings.
            self.assertEqual(calls[2][1], calls[1][1])
        posts = [m["text"] for m in board.snapshot()["messages"] if m["author"] == "Creation tester"]
        self.assertTrue(any("stays in createdStrategies as created_band_pullback_test.pine" in text for text in posts))
        self.assertTrue(any("Built from" in m["text"] or "Recorded" in m["text"] for m in board.snapshot()["messages"] if m["author"] == "Lesson writer"))

    def test_handoff_files_the_new_script_for_the_chart_engine(self):
        from unittest import mock

        from futuresfund import strategy_team
        from futuresfund.board import Board

        script = (
            "//@version=5\n// Built from: Created EMA Band, ta.ema\n"
            "strategy(\"Screen Test\", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1)\n"
            "len = input.int(20, minval=5, maxval=200)\n"
            "if close > ta.ema(close, len)\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 8, limit=close + 16)\n"
        )
        desk = {
            "timeframe": "5m", "label": "5-minute", "developer": "5 min chart developer",
            "strategy_developer": "5 min strategy developer", "researcher": "Chart researcher", "engine": 2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(strategy_team, "CREATED", root / "created"), \
                    mock.patch.object(strategy_team, "LEARNING", root / "learning"), \
                    mock.patch.object(strategy_team, "LESSONS_PATH", root / "creation_lessons.json"), \
                    mock.patch.object(strategy_team, "QUEUE_PATH", root / "queue.json"), \
                    mock.patch.object(strategy_team, "_ask", lambda *args, **kwargs: "```pine\n" + script + "```"), \
                    mock.patch("futuresfund.learn.file_indicators", lambda *args, **kwargs: []):
                outcome = strategy_team.handoff(Board(), [], "Created EMA Band", desk)
                queued = list((root / "created").glob("*.pine"))
                filed = list((root / "learning").glob("*.pine")) if (root / "learning").is_dir() else []
            self.assertEqual(outcome, "queued")
            self.assertEqual([path.name for path in queued], ["created_screen_test.pine"])
            self.assertEqual(filed, [])
            self.assertEqual(strategy_team._read_queue(root / "queue.json"), ["created_screen_test.pine"])

    def test_a_developer_writes_from_scratch_from_the_creation_lessons(self):
        import threading
        from unittest import mock

        from futuresfund import strategy_team
        from futuresfund.board import Board

        calls = []

        def handoff(board, cards, source_name, desk, library_note=""):
            calls.append(source_name)
            board.cancel.set()
            return "queued"

        desk = {
            "timeframe": "2m", "label": "2-minute", "developer": "2min chart developer",
            "strategy_developer": "2min strategy developer", "researcher": "Chart researcher", "engine": 1,
        }
        pause = threading.Event()
        with mock.patch("futuresfund.learn.chart_needs_a_script", return_value=True), \
                mock.patch.object(strategy_team, "build_cards", return_value=[]), \
                mock.patch.object(strategy_team, "write_cards", lambda cards: None), \
                mock.patch.object(strategy_team, "creation_lessons", return_value=[{"title": "Created EMA Band"}]), \
                mock.patch.object(strategy_team, "new_library_lines", return_value=("", [])), \
                mock.patch.object(strategy_team, "mark_library", lambda ids: None), \
                mock.patch.object(strategy_team, "handoff", handoff):
            strategy_team.run_chart_creation(Board(), pause, desk)
        self.assertEqual(calls, ["from scratch"])

    def test_a_developer_keeps_writing_while_the_chart_still_has_scripts(self):
        import threading
        from unittest import mock

        from futuresfund import strategy_team
        from futuresfund.board import Board

        calls = []

        def handoff(board, cards, source_name, desk, library_note=""):
            calls.append((source_name, library_note))
            board.cancel.set()
            return "queued"

        desk = {
            "timeframe": "2m", "label": "2-minute", "developer": "2min chart developer",
            "strategy_developer": "2min strategy developer", "researcher": "Chart researcher", "engine": 1,
        }
        pause = threading.Event()
        with mock.patch("futuresfund.learn.chart_needs_a_script", return_value=False), \
                mock.patch.object(strategy_team, "build_cards", return_value=[]), \
                mock.patch.object(strategy_team, "write_cards", lambda cards: None), \
                mock.patch.object(strategy_team, "creation_lessons", return_value=[]), \
                mock.patch.object(strategy_team, "new_library_lines", return_value=("New strategies in the library. Learn from these results, then write a different strategy from scratch:\n- Fresh Band: profit 10, drawdown 4, trades 20 on 5m", ["fresh-1"])), \
                mock.patch.object(strategy_team, "mark_library", lambda ids: None), \
                mock.patch.object(strategy_team, "handoff", handoff):
            strategy_team.run_chart_creation(Board(), pause, desk)
        self.assertEqual(calls, [("the successful backtests", "New strategies in the library. Learn from these results, then write a different strategy from scratch:\n- Fresh Band: profit 10, drawdown 4, trades 20 on 5m")])

    def test_the_script_check_runs_on_the_creation_engine(self):
        from unittest import mock

        from futuresfund.strategy_team import CREATION_ENGINE, runtime_problems

        slots = []

        def run_script(source, bars, timeframe, inputs, cancel, instrument, label, slot=None):
            slots.append(slot)
            return {"net_profit": 1, "max_drawdown": 1, "trades": 1}

        bars = [{"t": index, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1} for index in range(60)]
        script = (
            "//@version=5\nstrategy(\"X\", overlay=true)\n"
            "if close > open\n    strategy.entry(\"L\", strategy.long)\n"
            "strategy.exit(\"XL\", from_entry=\"L\", stop=close - 4, limit=close + 8)\n"
        )
        with mock.patch("futuresfund.backtrader_engine.run_script", run_script):
            self.assertEqual(runtime_problems(script, bars, "15m"), [])
        self.assertEqual(slots, [CREATION_ENGINE])
        self.assertEqual(slots[0], 3)

    def test_a_new_library_strategy_is_shown_once(self):
        from unittest import mock

        from futuresfund import strategy_team

        strategy_team._LIBRARY_SEEN.clear()
        rows = [{"id": "fresh-1", "title": "Fresh Band", "net_profit": 10, "max_drawdown": 4, "trades": 20, "timeframe": "5m", "contract_label": "ES"}]
        with mock.patch("futuresfund.library._rows", return_value=rows), mock.patch(
            "futuresfund.library._load_untested", return_value=[]
        ):
            text, ids = strategy_team.new_library_lines()
            self.assertIn("Fresh Band", text)
            self.assertEqual(ids, ["fresh-1"])
            prompt = strategy_team.prompt_for([], library_note=text)
            self.assertIn("Fresh Band", prompt)
            self.assertIn("Engine 4", prompt)
            strategy_team.mark_library(ids)
            again, empty = strategy_team.new_library_lines()
        self.assertEqual(again, "")
        self.assertEqual(empty, [])
        strategy_team._LIBRARY_SEEN.clear()

    def test_engine_4_files_a_profitable_script_and_keeps_writing_the_queue(self):
        from unittest import mock

        from futuresfund import strategy_team
        from futuresfund.board import Board
        from futuresfund.learn import _Trials

        script = "//@version=5\nstrategy(\"Fresh\", overlay=true)\n"
        slots = []

        def fake_execute(*args, **kwargs):
            slots.append((args[7], kwargs.get("full")))
            return lambda params: {}

        def fake_search(*args, **kwargs):
            slots.append(kwargs.get("slot"))
            return _Trials([{"net_profit": 100, "max_drawdown": 50, "trades": 20, "days": 20, "params": {}}])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = root / "createdStrategies"
            created.mkdir()
            (created / "created_fresh.pine").write_text(script, encoding="utf-8")
            with mock.patch.object(strategy_team, "CREATED", created), \
                    mock.patch.object(strategy_team, "LEARNING", root / "learning"), \
                    mock.patch.object(strategy_team, "LESSONS_PATH", root / "lessons.json"), \
                    mock.patch.object(strategy_team, "QUEUE_PATH", root / "queue.json"), \
                    mock.patch("futuresfund.learn._load_frames", lambda root="ES": {"5m": []}), \
                    mock.patch("futuresfund.learn._engine_execute", fake_execute), \
                    mock.patch("futuresfund.learn._search", fake_search):
                strategy_team.enqueue_script("created_fresh.pine")
                outcome = strategy_team.develop_one(Board(), None)
                self.assertEqual(outcome, "studied")
            self.assertEqual(slots, [(3, True), 3])
            self.assertEqual(list((root / "learning").glob("*.pine")), [])
            self.assertTrue((created / "created_fresh.pine").is_file())
            self.assertEqual(strategy_team.take_queued_script(root / "queue.json"), "")

    def test_lessons_are_recorded_and_read_back_for_the_next_prompt(self):
        from futuresfund.strategy_team import creation_lessons, lesson_text, record_lesson
        from unittest import mock
        from futuresfund import strategy_team

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creation_lessons.json"
            with mock.patch.object(strategy_team, "LESSONS_PATH", path):
                record_lesson({"title": "A", "net_profit": 100, "max_drawdown": 50, "trades": 9, "accepted": True, "verdict": "met the target and was promoted as a.pine"})
                record_lesson({"title": "B", "net_profit": -5, "max_drawdown": 500, "trades": 9, "accepted": False})
                rows = creation_lessons(1)
                self.assertEqual([row["title"] for row in rows], ["B"])
                self.assertIn("did not meet the target", lesson_text(rows[0]))
                self.assertIn("promoted as a.pine", lesson_text(creation_lessons(2)[0]))

    def test_a_created_script_is_left_out_of_the_training_sources(self):
        from unittest import mock

        from futuresfund import strategy_team

        strategy_team._LIBRARY_SEEN.clear()
        rows = [
            {"id": "own-1", "title": "Own Band", "file": "created_own_band.pine", "net_profit": 10, "max_drawdown": 4, "trades": 20, "timeframe": "5m"},
            {"id": "desk-1", "title": "Desk Band", "file": "desk_band.pine", "net_profit": 12, "max_drawdown": 4, "trades": 20, "timeframe": "5m"},
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(strategy_team, "CREATED", Path(tmp)), mock.patch(
            "futuresfund.library._rows", return_value=rows
        ), mock.patch("futuresfund.library._load_untested", return_value=[]):
            text, ids = strategy_team.new_library_lines()
            self.assertNotIn("Own Band", text)
            self.assertIn("Desk Band", text)
            self.assertEqual(ids, ["desk-1"])
            card = {
                "file": "created_own_band.pine", "title": "Own Band", "best": {},
                "changes": [{"change": "len 10 to 20", "impact": "len changed the book.", "net_profit": 1, "max_drawdown": 1, "trades": 4}],
            }
            prompt = strategy_team.prompt_for([card], [{"title": "Created EMA Band", "net_profit": -300, "max_drawdown": 1900, "trades": 9, "accepted": False}])
        self.assertNotIn("Own Band", prompt)
        self.assertNotIn("Created EMA Band", prompt)
        strategy_team._LIBRARY_SEEN.clear()

    def test_only_the_three_backtest_engines_file_the_untested_list(self):
        from unittest import mock

        from futuresfund.learn import _offer_library

        row = {
            "engine": "backtrader", "runner": "backtrader", "net_profit": 53895.0,
            "max_drawdown": 2000.0, "trades": 400, "days": 260, "win_rate": 65,
            "profit_factor": 1.4, "instrument": "ES", "timeframe": "5m", "profit_ratio": 1.2,
        }
        with mock.patch("futuresfund.library.record_untested", return_value=True) as filed, mock.patch(
            "futuresfund.library.record_profitable", return_value=False
        ), mock.patch("futuresfund.library.library_ready", return_value=True):
            _offer_library(None, {"title": "Created"}, Path("created_x.pine"), row, "Creation tester")
            self.assertFalse(filed.called)
            _offer_library(None, {"title": "Studied"}, Path("studied.pine"), row, "5 min chart developer")
            self.assertEqual(filed.call_count, 1)
            _offer_library(None, {"title": "Studied"}, Path("studied.pine"), row, "15 min chart developer")
            self.assertEqual(filed.call_count, 2)

    def test_candle_notes_teach_the_creation_team_and_not_the_chart_engines(self):
        from unittest import mock

        from futuresfund import strategy_team
        from futuresfund.learn import research_guidelines

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candle_probabilities.json"
            with mock.patch.object(strategy_team, "CANDLE_NOTES", path):
                for _ in range(18):
                    strategy_team.record_candle({
                        "instrument": "NQ", "timeframe": "15m", "session": "overnight",
                        "paths": 10, "agreed": 8, "direction": "up", "confidence": 0.8, "inside_90": True,
                    })
                for _ in range(4):
                    strategy_team.record_candle({
                        "instrument": "NQ", "timeframe": "15m", "session": "overnight",
                        "paths": 10, "agreed": 8, "direction": "up", "confidence": 0.8, "inside_90": False,
                    })
                for _ in range(8):
                    strategy_team.record_candle({
                        "instrument": "NQ", "timeframe": "15m", "session": "afternoon",
                        "paths": 10, "agreed": 5, "direction": "up", "confidence": 0.2, "inside_90": False,
                    })
                for _ in range(2):
                    strategy_team.record_candle({
                        "instrument": "NQ", "timeframe": "15m", "session": "afternoon",
                        "paths": 10, "agreed": 5, "direction": "up", "confidence": 0.2, "inside_90": True,
                    })
                text = strategy_team.prompt_for([])
                guide = research_guidelines()
                from futuresfund.llm import _clip, _system

                chart = _clip(_system("15 min chart developer"), 6000)
                researcher = _clip(_system("Chart researcher"), 6000)
        self.assertIn("inside the 0.9 set on 18 of 22 bars", text)
        self.assertIn("NQ 15m, overnight, at least 8 of 10 paths up, next candle peaked", text)
        self.assertIn("inside the 0.9 set on 2 of 10 bars", text)
        self.assertIn("about half the paths each way", text)
        self.assertIn("next candle flat", text)
        self.assertIn("Do not enter on these", text)
        self.assertNotIn("Created EMA Band", text)
        self.assertNotIn("Candle notes for the chart teams", guide)
        self.assertIn("Limit risk", guide)
        self.assertIn("Candle notes for the chart teams", chart)
        self.assertIn("260 sessions", chart)
        self.assertIn("Candle notes for the chart teams", researcher)
        self.assertIn("260 sessions", researcher)
        self.assertIn("Do not invent", strategy_team.candle_lines(Path(tmp) / "missing.json"))
        self.assertEqual(strategy_team.candle_session(22, 0), "overnight")
        self.assertEqual(strategy_team.candle_session(10, 0), "morning")
        self.assertEqual(strategy_team.candle_session(14, 0), "afternoon")
        self.assertEqual(strategy_team.candle_session(17, 0), "halt")
