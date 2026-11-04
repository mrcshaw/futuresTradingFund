"""A script over the trade cap is not refused: the chart researcher cuts its trades.

A run with more than 900 trades, or on a stage a pace over 600 a year, is kept (over_cap) but never passes.
The cut turns on the script's own daily limits, doubles the cooldown (widening its maxval in the run copy),
writes the desk trade limits and sets most trades a day, one step per attempt. A run under the cap ends the
cut. After TRADE_CUT_ATTEMPTS cut attempts still over the cap, the researcher asks the owner.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from futuresfund import book, learn, objective, research_rules, strategy_team, trade_cut
from futuresfund.backtrader_engine import Bar
from futuresfund.pine_runtime import compile_pine, run_pine, supported

ROOT = Path(__file__).resolve().parents[1]

HEADER = (
    '//@version=5\n'
    'strategy("Busy", overlay=true, initial_capital=50000, default_qty_type=strategy.fixed, default_qty_value=1,\n'
    '  pyramiding=0, process_orders_on_close=true)\n'
)
BODY = (
    'len = input.int(2, "Length", minval=1, maxval=50)\n'
    'up = close > close[1]\n'
    'if up and strategy.position_size == 0\n'
    '    strategy.entry("Long", strategy.long)\n'
    'if not up and strategy.position_size > 0\n'
    '    strategy.close("Long")\n'
)
COOLDOWN_BODY = (
    'cooldown_bars = input.int(5, "Cooldown Bars", minval=1, maxval=20)\n'
    'var int last_entry = -1000\n'
    'up = close > close[1]\n'
    'if up and strategy.position_size == 0 and bar_index - last_entry >= cooldown_bars\n'
    '    strategy.entry("Long", strategy.long,\n'
    '         comment="in")\n'
    '    last_entry := bar_index\n'
    'if not up and strategy.position_size > 0\n'
    '    strategy.close("Long")\n'
)

# 2026-01-05 15:00 New York (20:00 UTC), a Monday afternoon.
START = int(datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc).timestamp())


def _bars(count: int = 1500, rise: float = -0.6, start: int = START, step: int = 120) -> list:
    """Two bars up, one bar down, so a trade opens and closes every few bars.

    The 5 pm New York hour has no bars, like the futures break (January is UTC-5).
    """
    bars = []
    price = 100.0
    moment = start
    k = 0
    while len(bars) < count:
        if (datetime.fromtimestamp(moment, timezone.utc) - timedelta(hours=5)).hour == 17:
            moment += step
            continue
        close = price + (1 if k % 3 else rise) * 0.5
        bars.append(Bar(moment, price, max(price, close) + 0.25, min(price, close) - 0.25, close, 10))
        price = close
        moment += step
        k += 1
    return bars


def _default_book(test):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    patcher = mock.patch.object(book, "BOOK_PATH", Path(folder.name) / "book.json")
    patcher.start()
    test.addCleanup(patcher.stop)


def _session_day(entry_time: str) -> str:
    """The futures day of a trade: its New York entry time moved 6 hours on, so 6 pm opens the next day."""
    return str((datetime.strptime(str(entry_time), "%Y-%m-%d %H:%M") + timedelta(hours=6)).date())


class DeskBlockTests(unittest.TestCase):
    def test_the_block_is_written_once_with_its_inputs_and_every_entry_waits_on_it(self):
        text = strategy_team.ensure_trade_limits(HEADER + BODY)
        self.assertIn(strategy_team.TRADE_LIMITS_MARKER, text)
        self.assertEqual(strategy_team.ensure_trade_limits(text), text)
        for label in ("Desk Daily Loss Limit ($)", "Desk Daily Profit Limit ($)", "Desk Max Trades a Day", "Desk Cooldown (bars)"):
            self.assertEqual(text.count(f'"{label}"'), 1)
        self.assertIn("    if not desk_locked\n        strategy.entry(\"Long\", strategy.long)", text)
        self.assertIn('strategy.close_all(comment="Daily Limit")', text)
        self.assertNotIn("strategy.risk", text)
        self.assertNotIn("closedtrades.profit", text)
        # It goes in after strategy(), before the script reads desk_locked.
        self.assertLess(text.index(strategy_team.TRADE_LIMITS_MARKER), text.index("if not desk_locked"))
        self.assertGreater(text.index(strategy_team.TRADE_LIMITS_MARKER), text.index("process_orders_on_close=true)"))

    def test_a_script_with_its_own_cooldown_gets_no_desk_cooldown_and_a_split_entry_moves_whole(self):
        text = strategy_team.ensure_trade_limits(HEADER + COOLDOWN_BODY)
        self.assertNotIn("desk_cooldown_bars = input", text)
        self.assertIn("desk_cooling = false", text)
        self.assertIn('        strategy.entry("Long", strategy.long,\n             comment="in")', text)
        self.assertEqual(strategy_team.own_cooldown(text), "cooldown_bars")
        compile_pine(text)

    def test_the_block_compiles_in_the_python_gate_and_survives_the_backtest_preparation(self):
        text = strategy_team.ensure_trade_limits(HEADER + BODY)
        self.assertTrue(supported(text))
        self.assertFalse(strategy_team.compile_error(text))
        prepared = strategy_team.prepare_backtest(text, "ES")
        self.assertEqual(prepared.count(strategy_team.TRADE_LIMITS_MARKER), 1)
        self.assertEqual(strategy_team.prepare_backtest(prepared, "ES"), prepared)
        self.assertTrue(supported(prepared))

    def test_off_limits_change_nothing_and_each_limit_cuts_trades(self):
        plain = run_pine(HEADER + BODY, _bars(), {}, root="ES", timeframe="2m")
        text = strategy_team.ensure_trade_limits(HEADER + BODY)
        off = run_pine(text, _bars(), {}, root="ES", timeframe="2m")
        self.assertEqual(len(off), len(plain))
        self.assertGreater(len(off), 300)
        capped = run_pine(text, _bars(), {"desk_max_trades_day": 2}, root="ES", timeframe="2m")
        days: dict[str, int] = {}
        for trade in capped:
            day = _session_day(trade["entry_time"])
            days[day] = days.get(day, 0) + 1
        self.assertTrue(capped)
        self.assertLessEqual(max(days.values()), 2)
        cooled = run_pine(text, _bars(), {"desk_cooldown_bars": 30}, root="ES", timeframe="2m")
        self.assertLess(len(cooled), len(off) / 5)

    def test_a_daily_loss_limit_closes_the_trade_and_stops_until_the_next_6pm(self):
        text = strategy_team.ensure_trade_limits(HEADER + BODY)
        # A falling chart: every trade loses $12.50.
        free = run_pine(text, _bars(rise=-1.5), {}, root="ES", timeframe="2m")
        trades = run_pine(text, _bars(rise=-1.5), {"desk_daily_loss": 50}, root="ES", timeframe="2m")
        self.assertTrue(trades)
        self.assertLess(len(trades), len(free) / 10)
        by_day: dict[str, list] = {}
        for trade in trades:
            by_day.setdefault(_session_day(trade["entry_time"]), []).append(trade)
        # Each futures day stops once its losses reach $50: a few trades, not the hundreds an open day takes.
        for day, rows in by_day.items():
            self.assertLessEqual(len(rows), 4, day)
        # The bars run from Monday 3 pm New York through Wednesday, so three futures days start trading.
        self.assertGreaterEqual(len(by_day), 2)

    def test_a_daily_profit_limit_stops_the_day_once_it_is_reached(self):
        text = strategy_team.ensure_trade_limits(HEADER + BODY)
        free = run_pine(text, _bars(), {}, root="ES", timeframe="2m")
        rising = _bars(rise=-0.6)
        limited = run_pine(text, rising, {"desk_daily_profit": 100}, root="ES", timeframe="2m")
        self.assertLess(len(limited), len(free) / 5)


@unittest.skipUnless(shutil.which("node") and (ROOT / ".tools" / "pinets" / "node_modules").is_dir(), "PineTS is not installed here")
class PineTSParityTests(unittest.TestCase):
    def test_pinets_cuts_the_same_trades_as_the_python_gate(self):
        from futuresfund.backtrader_engine import script_trades

        text = strategy_team.ensure_trade_limits(HEADER + BODY)
        rising, falling = _bars(rise=-0.6), _bars(rise=-1.5)
        with mock.patch.dict("os.environ", {"FUTURES_PINETS_SERVER": "0"}):
            for inputs, bars in (({"desk_max_trades_day": 2}, rising), ({"desk_daily_loss": 50}, falling),
                                 ({"desk_daily_profit": 100}, rising), ({"desk_daily_profit": 300, "desk_max_trades_day": 5}, rising),
                                 ({"desk_cooldown_bars": 400, "desk_daily_loss": 100}, falling), ({"desk_cooldown_bars": 30}, rising)):
                with self.subTest(inputs=inputs):
                    pinets = script_trades(text, bars, inputs, "ES", "2m", as_written=True)
                    python = run_pine(text, bars, inputs, root="ES", timeframe="2m")
                    self.assertEqual(len(pinets), len(python))
                    self.assertAlmostEqual(sum(t["pnl"] for t in pinets), sum(t["pnl"] for t in python), places=2)


class CapRuleTests(unittest.TestCase):
    def test_the_year_cap_is_900_trades_and_a_stage_cap_is_600_a_year_at_its_pace(self):
        self.assertFalse(objective.over_trade_cap({"trades": 900, "days": 260}))
        self.assertTrue(objective.over_trade_cap({"trades": 901, "days": 260}))
        # 7 days are 5 sessions: 11 trades is 572 a year, 12 is 624.
        self.assertFalse(objective.over_trade_cap({"trades": 11, "days": 5}, 7))
        self.assertTrue(objective.over_trade_cap({"trades": 12, "days": 5}, 7))
        self.assertTrue(objective.over_trade_cap({"trades": 12}, 7))
        self.assertIn("never filed", objective.trade_cap_text({"trades": 1240, "days": 260}))
        self.assertIn("1,240 trades in a year", objective.trade_cap_text({"trades": 1240, "days": 260}))
        self.assertIn("7-day test", objective.trade_cap_text({"trades": 30, "days": 5}, 7))

    def test_only_the_chart_that_measured_the_count_starts_in_the_cut(self):
        notes = {
            "over_trade_cap": 1500,
            "over_trade_charts": {"ES|2m": 1240},
            "instruments": {"NQ": {"charts": {"5m": {"trials": [{"trades": 950}]}, "15m": {"trials": [{"trades": 300}]}}}},
        }
        self.assertEqual(objective.chart_over_cap(notes, "ES", "2m"), 1240)
        self.assertEqual(objective.chart_over_cap(notes, "ES", "15m"), 0)
        self.assertEqual(objective.chart_over_cap(notes, "NQ", "5m"), 950)
        self.assertEqual(objective.chart_over_cap(notes, "NQ", "15m"), 0)

    def test_research_never_turns_the_desk_limits_off(self):
        self.assertFalse(research_rules.is_daily_limit("desk_daily_loss"))
        self.assertFalse(research_rules.is_daily_limit("desk_daily_profit"))
        self.assertTrue(research_rules.is_daily_limit("daily_loss_limit"))
        prepared = research_rules.prepare_research({"desk_daily_loss": 500, "desk_daily_profit": 1000, "daily_loss_limit": 900})
        self.assertEqual((prepared["desk_daily_loss"], prepared["desk_daily_profit"], prepared["daily_loss_limit"]), (500, 1000, 0))

    def test_a_cooldown_maxval_is_widened_and_the_run_copy_keeps_the_wider_value(self):
        from futuresfund.backtrader_engine import script_as_run

        text = HEADER + COOLDOWN_BODY
        wider = strategy_team.widen_maxval(text, "cooldown_bars", 40)
        self.assertIn('cooldown_bars = input.int(5, "Cooldown Bars", minval=1, maxval=40)', wider)
        self.assertEqual(strategy_team.widen_maxval(wider, "cooldown_bars", 30), wider)
        bounds = learn._input_bounds(learn.pine_facts(wider))
        self.assertEqual(learn._clamp_plan({"cooldown_bars": 40}, bounds)["cooldown_bars"], 40)
        self.assertIn("cooldown_bars = input.int(40,", script_as_run(wider, {"cooldown_bars": 40}, "ES"))
        # The old copy snaps it back to 20.
        self.assertIn("cooldown_bars = input.int(20,", script_as_run(text, {"cooldown_bars": 40}, "ES"))


class CutPlanTests(unittest.TestCase):
    def test_the_cut_goes_own_limits_cooldown_desk_limits_then_trades_a_day(self):
        script = HEADER + (
            'use_daily_limits = input.bool(false, "Use Daily Limits")\n'
            'daily_loss_limit = input.float(0, "Daily Loss Limit ($)", minval=0)\n'
        ) + COOLDOWN_BODY
        params = {"use_daily_limits": False, "daily_loss_limit": 0.0, "cooldown_bars": 5, "stop_dollars": 250, "tp_dollars": 500}
        bounds = learn._input_bounds(learn.pine_facts(script))
        state = trade_cut.new_state()
        plan, text, said = trade_cut.plan_step(params, script, bounds, state, seeds={"daily_loss_limit": 0.0}, weekly_profit=115.4)
        self.assertEqual((plan["use_daily_limits"], plan["daily_loss_limit"]), (True, 500.0))
        self.assertEqual(text, script)
        self.assertIn("daily loss limit", said)
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state)
        self.assertEqual(plan["cooldown_bars"], 10)
        self.assertIn("from 5 to 10 bars", said)
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state, weekly_profit=115.4)
        self.assertIn(strategy_team.TRADE_LIMITS_MARKER, text)
        self.assertEqual((plan["desk_daily_loss"], plan["desk_daily_profit"]), (500, 500))
        self.assertNotIn("desk_cooldown_bars", plan)
        self.assertIn("$500 daily loss limit and a $500 daily profit limit", said)
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state)
        self.assertEqual(plan["desk_max_trades_day"], 3)
        # Then the steps tighten: cooldown 10 -> 20 is the input's maxval, 40 widens it in the run copy.
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state)
        self.assertEqual(plan["cooldown_bars"], 20)
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state)
        self.assertEqual(plan["desk_max_trades_day"], 2)
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state)
        self.assertEqual(plan["desk_daily_loss"], 250)
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state)
        self.assertEqual(plan["desk_daily_profit"], 250)
        plan, text, said = trade_cut.plan_step(plan, text, bounds, state)
        self.assertEqual(plan["cooldown_bars"], 40)
        self.assertIn("maxval=40", text)
        self.assertIn("widened", said)

    def test_a_script_without_a_cooldown_gets_the_desk_cooldown(self):
        script = HEADER + BODY
        state = trade_cut.new_state()
        plan, text, said = trade_cut.plan_step({"len": 2}, script, {}, state)
        self.assertIn(strategy_team.TRADE_LIMITS_MARKER, text)
        self.assertEqual(plan["desk_cooldown_bars"], trade_cut.COOLDOWN_SEED)

    def test_without_a_copy_to_write_into_only_the_inputs_move(self):
        state = trade_cut.new_state()
        self.assertIsNone(trade_cut.plan_step({"len": 2}, "", {}, state, can_rewrite=False))

    def test_tightening_a_limit_that_is_off_sets_its_seed(self):
        self.assertEqual(trade_cut.tighten_value("desk_max_trades_day", 0), 3)
        self.assertEqual(trade_cut.tighten_value("desk_max_trades_day", 3), 2)
        self.assertIsNone(trade_cut.tighten_value("desk_max_trades_day", 1))
        self.assertEqual(trade_cut.tighten_value("desk_cooldown_bars", 0), 10)
        self.assertEqual(trade_cut.tighten_value("desk_daily_loss", 0, 500), 500)
        self.assertEqual(trade_cut.tighten_value("desk_daily_loss", 500), 250)
        self.assertIsNone(trade_cut.tighten_value("desk_daily_profit", 0))
        plan = learn._tighten_plan({"desk_max_trades_day": 0, "desk_daily_loss": 0, "stop_dollars": 300, "len": 2}, set())
        self.assertEqual(plan["desk_max_trades_day"], 3)
        self.assertTrue(learn._is_tighten_name("desk_daily_loss"))
        plan = learn._tighten_plan({"desk_daily_loss": 0, "stop_dollars": 300, "len": 2}, set())
        self.assertEqual(plan["desk_daily_loss"], 600)


def _executor(script: str, results):
    def execute(params):
        execute.calls.append((dict(params), execute.source["text"]))
        return results(params, execute.source["text"])

    execute.calls = []
    execute.source = {"text": script, "stamp": 0, "sessions": None, "root": "ES", "timeframe": "2m"}
    return execute


class _Board:
    def __init__(self):
        import threading

        self.cancel = threading.Event()
        self.posts: list[tuple[str, str]] = []

    def post(self, author, text, kind="log", channel=None):
        self.posts.append((author, text))

    def log(self, *args, **kwargs):
        pass


class SearchTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)
        patcher = mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_run_over_900_is_kept_and_the_cut_ends_when_the_trades_are_under_the_cap(self):
        def results(params, text):
            capped = int(params.get("desk_max_trades_day") or 0)
            trades = 500 if capped and strategy_team.TRADE_LIMITS_MARKER in text else 1240
            return {"net_profit": 800.0 + len(execute.calls), "max_drawdown": 300.0, "trades": trades, "days": 260}

        execute = _executor(HEADER + COOLDOWN_BODY, results)
        facts = learn.pine_facts(execute.source["text"])
        with mock.patch.object(learn, "set_aside") as set_aside, mock.patch.object(learn, "leave_unrunnable") as leave:
            trials = learn._search({"cooldown_bars": 5}, execute, 50000, 8, None, None, None, facts, extras=False)
        set_aside.assert_not_called()
        leave.assert_not_called()
        self.assertFalse(trials.over_trades)
        over = [row for row in trials if row.get("over_cap")]
        self.assertEqual([row["trades"] for row in trials[:4]], [1240, 1240, 1240, 500])
        self.assertEqual(len(over), 3)
        self.assertTrue(all(not row["passed"] for row in over))
        self.assertEqual(trials[1]["params"]["cooldown_bars"], 10)
        # About 2x the $250 stop, and the library's weekly target rounded up to $50 (no take profit input here).
        weekly = trade_cut._dollars_up(objective.stage_bars(None)["weekly_profit"])
        self.assertEqual((trials[2]["params"]["desk_daily_loss"], trials[2]["params"]["desk_daily_profit"]), (500, weekly))
        self.assertEqual(trials[3]["params"]["desk_max_trades_day"], 3)
        self.assertIn(strategy_team.TRADE_LIMITS_MARKER, execute.source["text"])
        self.assertGreaterEqual(execute.source["stamp"], 1)
        self.assertFalse(execute.source["trade_cut"]["active"])
        # The search goes on from the cut settings, values only.
        self.assertGreater(len(trials), 4)
        for row in trials[4:]:
            self.assertIn("desk_daily_loss", row["params"])

    def test_twelve_cuts_still_over_the_cap_ask_the_owner(self):
        def results(params, text):
            return {"net_profit": 800.0 + len(execute.calls), "max_drawdown": 300.0, "trades": 1500 + len(execute.calls), "days": 260}

        execute = _executor(HEADER + COOLDOWN_BODY, results)
        facts = learn.pine_facts(execute.source["text"])
        with mock.patch.object(learn, "set_aside") as set_aside:
            trials = learn._search({"cooldown_bars": 5}, execute, 50000, 40, None, None, None, facts, extras=False)
        set_aside.assert_not_called()
        self.assertEqual(len(trials), 1 + learn.TRADE_CUT_ATTEMPTS)
        self.assertTrue(all(row.get("over_cap") for row in trials))
        self.assertIn("still takes too many trades on the ES 2m chart", trials.over_trades)
        self.assertIn(f"{learn.TRADE_CUT_ATTEMPTS} cut attempts in a row", trials.over_trades)
        self.assertIn("desk_max_trades_day = 2", trials.over_trades)

    def test_the_knob_sets_how_many_cuts_run_before_the_question(self):
        def results(params, text):
            return {"net_profit": 800.0 + len(execute.calls), "max_drawdown": 300.0, "trades": 1500 + len(execute.calls), "days": 260}

        execute = _executor(HEADER + COOLDOWN_BODY, results)
        with mock.patch.object(learn, "TRADE_CUT_ATTEMPTS", 2):
            trials = learn._search({"cooldown_bars": 5}, execute, 50000, 40, None, None, None, learn.pine_facts(execute.source["text"]), extras=False)
        self.assertEqual(len(trials), 3)
        self.assertTrue(trials.over_trades)

    def test_a_stage_pace_over_600_a_year_starts_the_cut_and_never_passes(self):
        def results(params, text):
            capped = int(params.get("desk_max_trades_day") or 0)
            return {"net_profit": 900.0, "max_drawdown": 50.0, "trades": 10 if capped else 30, "days": 5}

        execute = _executor(HEADER + BODY, results)
        execute.source["sessions"] = 5
        with mock.patch.object(learn, "STAGE_PATIENCE", 2):
            trials = learn._search({"len": 2}, execute, 50000, 10, None, None, None, learn.pine_facts(execute.source["text"]),
                                   extras=False, developer="5 min chart developer", stage_days=7)
        self.assertTrue(trials[0]["over_cap"])
        self.assertFalse(trials[0]["stage_passed"])
        under = [row for row in trials if not row.get("over_cap")]
        self.assertTrue(under)
        self.assertIs(trials.best_pass, under[0] if under[0]["stage_passed"] else trials.best_pass)
        self.assertFalse(trials.best_pass.get("over_cap"))

    def test_a_run_over_the_cap_after_a_pass_keeps_the_pass_and_the_next_stage_starts_from_it(self):
        def results(sessions, params):
            # The first settings pass with 2 trades a session. A longer length trades far past the cap.
            length = float(params.get("length", 10))
            if length > 10:
                return {"net_profit": 400, "max_drawdown": 100, "trades": 60 * sessions, "days": sessions}
            return {"net_profit": 300 + length, "max_drawdown": 100, "trades": 2 * sessions, "days": sessions}

        calls = []

        def execute(params):
            calls.append((execute.source["sessions"], dict(params)))
            return results(execute.source["sessions"], params)

        execute.source = {"text": "", "stamp": 0, "sessions": None}
        marks = []
        with mock.patch.object(learn, "_mark_stage", lambda *args, **kwargs: marks.append((args[3], args[4]))):
            settings, stopped = learn._prove_stages(
                {"length": 10}, execute, None, None, None, None, "5 min chart developer", "Chart researcher", None,
            )
        self.assertIsNone(stopped)
        self.assertEqual(settings["length"], 10)
        self.assertIn((7, "passed"), marks)
        self.assertIn((30, "passed"), marks)
        self.assertFalse(execute.source["trade_cut"]["active"])
        # The 30-day stage started from the 7-day pass, not from the over-cap run.
        first_30 = next(params for sessions, params in calls if sessions == 21)
        self.assertEqual(first_30["length"], 10)

    def test_an_over_cap_run_after_a_pass_is_not_cut_and_the_next_attempt_goes_back_to_the_pass(self):
        def results(params, text):
            length = int(params.get("len") or 2)
            if length != 2:
                return {"net_profit": 950.0, "max_drawdown": 50.0, "trades": 40, "days": 5}
            return {"net_profit": 900.0, "max_drawdown": 50.0, "trades": 3, "days": 5}

        execute = _executor(HEADER + BODY, results)
        execute.source["sessions"] = 5
        board = _Board()
        with tempfile.TemporaryDirectory() as folder:
            learning = Path(folder) / "learning"
            learning.mkdir()
            path = learning / "Busy.pine"
            path.write_text(execute.source["text"], encoding="utf-8")
            with mock.patch.object(learn, "STAGE_PATIENCE", 3), mock.patch.object(learn, "LEARNING", learning),                     mock.patch.object(learn, "NOTES", Path(folder) / "notes"):
                trials = learn._search({"len": 2}, execute, 50000, 10, board, path, None,
                                       learn.pine_facts(execute.source["text"]), extras=False,
                                       developer="5 min chart developer", stage_days=7)
        self.assertFalse(trials.over_trades)
        self.assertFalse(execute.source["trade_cut"]["active"])
        self.assertIs(trials.best_pass, trials[0])
        self.assertTrue(any(row.get("over_cap") for row in trials))
        self.assertTrue(any("The stage already passed, so I am not cutting its trades" in text for _author, text in board.posts))
        self.assertNotIn(strategy_team.TRADE_LIMITS_MARKER, execute.source["text"])

    def test_a_cut_left_on_by_the_last_stage_starts_from_this_stages_settings(self):
        execute = _executor(HEADER + BODY, lambda params, text: {"net_profit": 900.0, "max_drawdown": 50.0, "trades": 9, "days": 21})
        execute.source["sessions"] = 21
        cut = learn._cut_state(execute)
        cut.update({"active": True, "anchor": {"len": 9}, "used": 7})
        with mock.patch.object(learn, "STAGE_PATIENCE", 1):
            learn._search({"len": 2}, execute, 50000, 3, None, None, None, learn.pine_facts(execute.source["text"]),
                          extras=False, developer="5 min chart developer", stage_days=30)
        self.assertEqual(execute.calls[0][0]["len"], 2)

    def test_a_widened_cooldown_survives_the_next_search_on_the_same_copy(self):
        original = HEADER + COOLDOWN_BODY
        facts = learn.pine_facts(original)
        execute = _executor(strategy_team.widen_maxval(original, "cooldown_bars", 40), lambda params, text: {
            "net_profit": 900.0, "max_drawdown": 50.0, "trades": 9, "days": 21,
        })
        execute.source["stamp"] = 1
        execute.source["sessions"] = 21
        with mock.patch.object(learn, "STAGE_PATIENCE", 1):
            # The next stage is handed the facts read from the original file, maxval 20.
            learn._search({"cooldown_bars": 40}, execute, 50000, 2, None, None, None, facts,
                          extras=False, developer="5 min chart developer", stage_days=30)
        self.assertEqual(execute.calls[0][0]["cooldown_bars"], 40)

    def test_a_daily_limit_the_cut_turned_on_stays_on_at_the_next_stage(self):
        execute = _executor(HEADER + BODY, lambda params, text: {"net_profit": 1.0, "max_drawdown": 1.0, "trades": 1, "days": 5})
        cut = learn._cut_state(execute)
        cut["touched"] = True
        seen = []

        def results(params, text):
            seen.append(dict(params))
            return {"net_profit": 1.0, "max_drawdown": 1.0, "trades": 2, "days": 260}

        execute.calls = []
        holder = execute.source
        def run(params):
            return results(params, holder["text"])
        run.source = holder
        learn._search({"daily_loss_limit": 500, "len": 2}, run, 50000, 1, None, None, None, None, extras=False)
        self.assertEqual(seen[0]["daily_loss_limit"], 500)
        fresh = _executor(HEADER + BODY, results)
        seen.clear()
        learn._search({"daily_loss_limit": 500, "len": 2}, fresh, 50000, 1, None, None, None, None, extras=False)
        self.assertEqual(seen[0]["daily_loss_limit"], 0)


class PostAndQuestionTests(unittest.TestCase):
    def test_the_cut_posts_are_the_chart_researchers(self):
        board = _Board()
        cut = trade_cut.new_state()
        path = Path("Busy.pine")
        with mock.patch.object(learn, "_in_learning", lambda path: False):
            over = learn._track_trade_cut(cut, {"trades": 1240, "days": 260}, {"len": 2}, None, False, board, path, "Chart researcher", "Busy.pine", "ES|2m")
        self.assertTrue(over)
        self.assertTrue(cut["active"])
        author, text = board.posts[-1]
        self.assertEqual(author, "Chart researcher")
        self.assertIn("Busy.pine took 1,240 trades in a year", text)
        cut["steps"] = ["turned on a $500 daily loss limit and a $1,000 daily profit limit", "raised the cooldown cooldown_bars from 6 to 12 bars"]
        learn._track_trade_cut(cut, {"trades": 420, "days": 260}, {"len": 2}, None, True, board, path, "Chart researcher", "Busy.pine", "ES|2m")
        self.assertFalse(cut["active"])
        author, text = board.posts[-1]
        self.assertEqual(author, "Chart researcher")
        self.assertIn("took 420 trades after I turned on a $500 daily loss limit", text)
        self.assertIn("under the trade cap", text)

    def test_the_mark_is_per_chart_and_information_only(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(learn, "NOTES", Path(folder)):
                path = Path(folder) / "Busy.pine"
                learn._mark_over_cap(path, 1240, "ES|2m")
                learn._mark_over_cap(path, 990, "ES|2m")
                learn._mark_over_cap(path, 950, "NQ|5m")
                notes = learn._read_notes(path)
        self.assertEqual(notes["over_trade_charts"], {"ES|2m": 1240, "NQ|5m": 950})
        self.assertNotIn("over_trade_cap", notes)

    def test_a_chart_that_already_saw_the_cap_starts_in_the_cut(self):
        board = _Board()
        execute = _executor(HEADER + BODY, lambda params, text: {})
        learn._start_cut_from_notes(board, Path("Busy.pine"), execute, {"over_trade_charts": {"ES|2m": 1240}}, "ES", "2m", "Chart researcher")
        self.assertTrue(execute.source["trade_cut"]["active"])
        self.assertEqual(board.posts[-1][0], "Chart researcher")
        other = _executor(HEADER + BODY, lambda params, text: {})
        learn._start_cut_from_notes(board, Path("Busy.pine"), other, {"over_trade_charts": {"ES|2m": 1240}}, "ES", "15m", "Chart researcher")
        self.assertFalse(other.source.get("trade_cut", {}).get("active"))

    def test_the_question_goes_to_the_owner_through_the_questions_module(self):
        asked = []
        module = types.ModuleType("futuresfund.questions")
        module.ask_owner = lambda board, path, author, reason, kind: asked.append((author, reason, kind)) or "awaiting"
        with mock.patch.dict(sys.modules, {"futuresfund.questions": module}), mock.patch.object(learn, "leave_unrunnable") as leave:
            outcome = learn._ask_over_trades(None, Path("Busy.pine"), "Chart researcher", "Busy.pine still takes too many trades.")
        self.assertEqual(outcome, "awaiting")
        self.assertEqual(asked, [("Chart researcher", "Busy.pine still takes too many trades.", "over_trades")])
        leave.assert_not_called()

    def test_the_study_no_longer_refuses_a_script_with_more_than_900_trades(self):
        import inspect

        source = inspect.getsource(learn._study_chart) + inspect.getsource(learn._search_inputs)
        self.assertNotIn("trades_already_over_cap", source)
        self.assertNotIn("cannot be saved", source)

    def test_the_texts_read_the_cap_and_say_the_desk_cuts_trades(self):
        text = (ROOT / "futuresfund" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("cuts its trades with daily loss and profit limits", text)
        self.assertNotIn("cannot be saved", strategy_team.prompt_for([], [], chart="5m"))


if __name__ == "__main__":
    unittest.main()
