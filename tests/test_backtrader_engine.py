"""The Backtrader engine against the TradingView trade export."""

import unittest

from futuresfund.backtrader_engine import slot_for
from futuresfund.board import Board
from futuresfund.learn import _clamp_plan, _search, _widen


class BacktraderEngineTests(unittest.TestCase):
    def test_a_script_keeps_going_after_the_first_result(self):
        from pathlib import Path

        from futuresfund.backtrader_engine import run_script

        source = (Path(__file__).resolve().parents[1] / "futuresfund" / "checkScripts" / "ema_structure_rsi_pullback_night_nmq.pine").read_text(encoding="utf-8")
        bars = []
        price = 100.0
        for index in range(80):
            price += 1 if index % 5 else -2
            bars.append({"t": 1_700_000_000 + index * 900, "o": price - 1, "h": price + 2, "l": price - 3, "c": price, "v": 20})
        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return run_script(source, bars, "15m", {"Stop ($)": 200 + calls["n"] * 25}, None)

        trials = _search({"stop_dollars": 200.0}, execute, 50000, 4, None, None, None)
        self.assertGreater(calls["n"], 1)
        self.assertFalse(trials[-1].get("fatal"))

    def test_bos_breakout_evening_takes_trades_on_the_es_chart(self):
        from pathlib import Path

        from futuresfund.backtrader_engine import run_script
        from futuresfund.charts import load_chart

        source = (Path(__file__).resolve().parents[1] / "futuresfund" / "checkScripts" / "bos_breakout_evening_tradingview.pine").read_text(encoding="utf-8")
        bars = load_chart("2m", "ES")
        self.assertGreater(len(bars), 500)
        result = run_script(source, bars, "2m", {}, None, "ES", "BOS Breakout Evening ES 2min")
        self.assertFalse(result.get("fatal"))
        self.assertGreater(result.get("trades") or 0, 0)
        self.assertIsNotNone(result.get("net_profit"))

    def test_each_chart_has_its_own_engine(self):
        from futuresfund import roster
        from futuresfund.backtrader_engine import _ENGINE_SLOTS, engine_status
        from futuresfund.engine_workers import _SLOTS

        # The one-argument form is still the ES chart; the creation engine takes anything else.
        self.assertEqual(slot_for("2m"), 0)
        self.assertEqual(slot_for("5m"), 1)
        self.assertEqual(slot_for("15m"), 2)
        self.assertEqual(slot_for("1h"), 3)
        self.assertEqual(slot_for("5m", "NQ"), 8)
        self.assertEqual(slot_for("15m", "YM"), 21)
        self.assertEqual(slot_for("2m", "ZZ"), 3)
        self.assertEqual(_ENGINE_SLOTS, 22)
        self.assertEqual(_SLOTS, 22)
        self.assertFalse(hasattr(roster, "EXTRA_CHART_ENGINES"))
        rows = engine_status()
        self.assertEqual(len(rows), 22)
        self.assertEqual([row["id"] for row in rows], list(range(1, 23)))
        creation = [row for row in rows if row["creation"]]
        self.assertEqual([row["id"] for row in creation], [4])
        self.assertEqual(creation[0]["developer"], "Creation tester")
        self.assertEqual(creation[0]["root"], "")
        charts = [row for row in rows if not row["creation"]]
        expected = [(root, timeframe) for root in roster.ENGINE_ROOTS for timeframe in ("2m", "5m", "15m")]
        self.assertEqual([(row["root"], row["timeframe"]) for row in charts], expected)
        self.assertEqual([(row["id"], row["root"], row["timeframe"]) for row in charts[:6]], [
            (1, "ES", "2m"), (2, "ES", "5m"), (3, "ES", "15m"), (5, "GC", "2m"), (6, "GC", "5m"), (7, "GC", "15m"),
        ])
        self.assertEqual(charts[-1]["id"], 22)
        developers = {"2m": "2min chart developer", "5m": "5 min chart developer", "15m": "15 min chart developer"}
        for row in charts:
            self.assertEqual(row["developer"], developers[row["timeframe"]])
            self.assertEqual(row["instrument"], row["root"])
        # Each developer owns seven engines, one per instrument.
        self.assertEqual(roster.engines_of("2min chart developer"), [1, 5, 8, 11, 14, 17, 20])
        self.assertEqual(roster.chart_engine(9)["root"], "NQ")
        self.assertIsNone(roster.chart_engine(4))

    def test_every_instrument_has_a_2m_5m_and_15m_engine_that_keeps_its_root(self):
        from futuresfund import backtrader_engine as engine
        from futuresfund.roster import ENGINE_DESKS, ENGINE_ROOTS, engine_for

        for root in ENGINE_ROOTS:
            for timeframe in ("2m", "5m", "15m"):
                desk = engine_for(root, timeframe)
                self.assertIsNotNone(desk, (root, timeframe))
                self.assertEqual(engine.slot_for(timeframe, root), desk["engine"] - 1)
        self.assertEqual(len(ENGINE_DESKS), 21)
        index = engine.slot_for("5m", "MES")
        engine._acquire_engine("run.pine", "5m", "MES", index)
        self.assertEqual(engine.engine_status()[index]["instrument"], "MES")
        engine._release_engine(index)
        # A chart engine keeps its instrument between runs; the creation engine has none until its next draft.
        self.assertEqual(engine.engine_status()[index]["instrument"], "MES")
        engine._acquire_engine("draft.pine", "15m", "NQ", engine._CREATION_SLOT)
        self.assertEqual(engine.engine_status()[engine._CREATION_SLOT]["instrument"], "NQ")
        engine._release_engine(engine._CREATION_SLOT)
        self.assertEqual(engine.engine_status()[engine._CREATION_SLOT]["instrument"], "")

    def test_a_default_outside_the_input_range_is_pulled_back_inside(self):
        from futuresfund.strategy import _legal_defaults

        source = 'ema_backcandles = input.int(35, "EMA Back Candles", minval=2, maxval=20, group="EMA")'
        self.assertIn("input.int(20,", _legal_defaults(source))
        plan = _clamp_plan({"ema_backcandles": 35}, {"ema_backcandles": (2, 20)})
        self.assertEqual(plan["ema_backcandles"], 20)

    def test_a_parenthesis_in_the_title_does_not_hide_the_input_range(self):
        from futuresfund.pine_report import pine_facts
        from futuresfund.strategy import _apply_inputs, _legal_defaults

        cooldown = 'cooldown_bars = input.int(150, "Cooldown (bars)", minval=0, maxval=100, step=1)'
        self.assertIn('input.int(100, "Cooldown (bars)"', _legal_defaults(cooldown))
        applied = _apply_inputs(
            'cooldown_bars = input.int(10, "Cooldown (bars)", minval=0, maxval=100, step=1)',
            {"cooldown_bars": 250},
        )
        self.assertIn("input.int(100,", applied)
        cap = 'daily_profit_cap = input.int(9000, "Daily Profit Cap ($)", minval=500, maxval=5000)'
        self.assertIn("input.int(5000,", _legal_defaults(cap))
        opening = 'range_bars = input.int(40, "Opening Range Bars (4h)", minval=4, maxval=32)'
        self.assertIn("input.int(32,", _legal_defaults(opening))
        facts = pine_facts(cooldown + "\n")
        self.assertEqual(facts["inputs"][0]["min"], 0)
        self.assertEqual(facts["inputs"][0]["max"], 100)

    def test_a_minimum_factor_is_kept_at_or_below_the_maximum(self):
        ordered = _clamp_plan({"minMult": 8, "maxMult": 6, "length": 22}, {})
        self.assertEqual(ordered["minMult"], 6)
        self.assertEqual(ordered["maxMult"], 6)
        lowered = _clamp_plan({"minMult": 1, "maxMult": 0}, {})
        self.assertLessEqual(lowered["minMult"], lowered["maxMult"])
        session = _clamp_plan({"sess_start_hr": 18, "sess_end_hr": 8}, {})
        self.assertEqual(session["sess_start_hr"], 18)
        self.assertEqual(session["sess_end_hr"], 8)

    def test_stop_keeps_the_researchers_from_starting_another_attempt(self):
        board = Board()
        board.set_research("running")
        board.set_activity("Chart researcher", "Studying a script")
        board.mark_stopped()
        board.set_activity("Chart researcher", "Studying the next script")
        self.assertEqual(board.snapshot()["activity"]["task"], "Stopping the researchers.")
        calls = {"n": 0}

        def execute(params):
            calls["n"] += 1
            return {"stopped": True, "runner": "backtrader", "engine": "backtrader", "net_profit": None, "max_drawdown": None, "trades": 0}

        trials = _search({"length": 10}, execute, 50000, 5, board, None, None)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(trials, [])

    def test_a_pine_search_does_not_add_a_python_indicator(self):
        plan = _widen({"length": 10}, {"length": 10}, set(), 4, extras=False)
        self.assertNotIn("added_indicator", plan)

    def test_one_finished_chart_does_not_count_as_the_whole_instrument(self):
        from futuresfund.learn import pending_for_chart, tested_instruments

        partial = {"instruments": {"ES": {"charts": {"2m": {"attempts": 1, "best": {"net_profit": 1}, "trials": [{}]}}}}}
        self.assertEqual(tested_instruments(partial), set())
        self.assertEqual(pending_for_chart(partial, "2m", ["ES"]), [])
        self.assertEqual(pending_for_chart(partial, "15m", ["ES"]), ["ES"])
