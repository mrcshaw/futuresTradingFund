"""The research pipeline spends its time on backtests: one Node process per engine, bars read once,
no attempts on inputs that change nothing, and no slow scans between runs."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import input_effects, learn
from futuresfund.backtrader_engine import Bar

MOVES = (
    '//@version=5\n'
    'strategy("Moves", overlay=true)\n'
    'length = input.int(3, "Length", minval=1)\n'
    'if bar_index % (length * 2) == 0\n'
    '    strategy.entry("Long", strategy.long)\n'
    'if bar_index % (length * 2) == length\n'
    '    strategy.close_all(comment="Done")\n'
)
BROKEN = '//@version=5\nstrategy("Broken")\nruntime.error("boom")\n'


def _rows(count: int, start: int = 1_788_307_200, step: int = 300) -> list[dict]:
    rows = []
    for k in range(count):
        close = 100 + (k % 17) - (k % 5) * 0.5
        rows.append({"t": start + k * step, "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 10 + k % 3})
    return rows


def _measured(net_profit: float, trades: int = 40, drawdown: float = 900.0, win_rate: float = 45.0) -> dict:
    return {
        "net_profit": net_profit, "max_drawdown": drawdown, "trades": trades, "win_rate": win_rate, "profit_factor": 0.9,
        "timeframe": "5m", "instrument": "ES", "runner": "backtrader", "engine": "backtrader", "days": 21,
    }


class TrackerTests(unittest.TestCase):
    def test_a_number_that_changes_nothing_up_and_down_is_taken_out(self):
        tracker = input_effects.Tracker()
        tracker.observe(0, {"level": 5.0, "length": 10}, _measured(-100))
        self.assertEqual(tracker.observe(0, {"level": 10.0, "length": 10}, _measured(-100)), ["level"])
        self.assertTrue(tracker.dead_move("level", 10.0, 20.0))
        self.assertFalse(tracker.dead_move("level", 10.0, 2.5))
        self.assertFalse(tracker.is_removed("level"))
        self.assertEqual(tracker.observe(0, {"level": 2.5, "length": 10}, _measured(-100)), ["level"])
        self.assertTrue(tracker.is_removed("level"))
        # The same direction again teaches nothing new.
        self.assertEqual(tracker.observe(0, {"level": 1.0, "length": 10}, _measured(-100)), [])

    def test_a_switch_that_changes_nothing_is_taken_out_at_once(self):
        tracker = input_effects.Tracker()
        tracker.observe(0, {"flag": True, "length": 10}, _measured(-100))
        tracker.observe(0, {"flag": False, "length": 10}, _measured(-100))
        self.assertTrue(tracker.is_removed("flag"))

    def test_an_input_that_moves_the_result_is_kept_with_its_effect(self):
        tracker = input_effects.Tracker()
        tracker.observe(0, {"length": 10}, _measured(-100))
        tracker.observe(0, {"length": 20}, _measured(250))
        self.assertFalse(tracker.is_removed("length"))
        self.assertEqual(tracker.moves["length"]["profit_changes"], [350.0])

    def test_a_result_that_comes_back_after_other_moves_is_not_evidence(self):
        tracker = input_effects.Tracker()
        tracker.observe(0, {"a": 1, "b": 1}, _measured(-100))
        tracker.observe(0, {"a": 2, "b": 1}, _measured(50))
        tracker.observe(0, {"a": 2, "b": 2}, _measured(70))
        # a and b both differ from the first run, and that run is not the one just before.
        self.assertEqual(tracker.observe(0, {"a": 3, "b": 2}, _measured(-100)), [])
        self.assertFalse(tracker.dead.get("a") or tracker.dead.get("b"))

    def test_a_run_without_trades_or_with_an_error_says_nothing(self):
        tracker = input_effects.Tracker()
        tracker.observe(0, {"level": 5.0}, _measured(0, trades=0))
        self.assertEqual(tracker.observe(0, {"level": 10.0}, _measured(0, trades=0)), [])
        self.assertEqual(tracker.observe(0, {"level": 20.0}, {**_measured(-100), "error": "stopped"}), [])

    def test_a_rewrite_starts_the_record_of_the_search_over(self):
        tracker = input_effects.Tracker()
        tracker.observe(0, {"flag": True}, _measured(-100))
        tracker.observe(0, {"flag": False}, _measured(-100))
        self.assertTrue(tracker.is_removed("flag"))
        tracker.observe(1, {"flag": False}, _measured(-300))
        self.assertFalse(tracker.is_removed("flag"))

    def test_the_record_is_written_once_per_search_and_summarized(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(input_effects, "HOME", Path(folder)):
            path = Path(folder) / "Some Script.txt"
            for _ in range(2):
                tracker = input_effects.Tracker(path, "ES 5m", "30-day stage", core=True)
                tracker.observe(0, {"length": 10, "flag": True}, _measured(-100))
                tracker.observe(0, {"length": 20, "flag": True}, _measured(400))
                tracker.observe(0, {"length": 20, "flag": False}, _measured(400))
                tracker.finish()
            saved = json.loads((Path(folder) / "Some_Script.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["searches"], 2)
            self.assertTrue(saved["core"])
            self.assertEqual(saved["inputs"]["length"]["changed"], 2)
            self.assertEqual(saved["inputs"]["flag"]["no_effect"], 2)
            self.assertEqual(saved["inputs"]["flag"]["removed"], 2)
            lines = input_effects.script_lines(path)
            self.assertIn("length (profit moved $500 on average over 2 moves)", lines[0])
            self.assertIn("flag (2 moves)", lines[1])
            desk = input_effects.desk_lines(core_only=True)
            self.assertIn("length ($500 on average)", desk[0])
            self.assertIn("flag", desk[1])
            # A search that moved nothing writes nothing.
            input_effects.Tracker(Path(folder) / "Other.txt").finish()
            self.assertFalse((Path(folder) / "Other.json").exists())


class SearchTests(unittest.TestCase):
    """Only length moves the result. dead_level and dead_flag change nothing."""

    def _search(self, *, stage_days=None, limit: int = 20, tracked: bool = True):
        calls: list[dict] = []

        def execute(params):
            calls.append(dict(params))
            length = params["length"]
            # Under 600 trades a year on the 30-day stage's 21 sessions, so the trade cut does not start.
            return _measured(-2000.0 + 35.0 * length, trades=10 + length // 4, drawdown=900.0 + length, win_rate=40.0 + length / 10)

        execute.source = {"text": "", "stamp": 0, "sessions": None}
        base = {"length": 10, "dead_level": 5.0, "dead_flag": True}
        patch = mock.patch("futuresfund.learn.input_effects.current", return_value=None) if not tracked else mock.patch.object(learn, "LOSS_LOOK", learn.LOSS_LOOK)
        with patch:
            trials = learn._search(base, execute, learn.ACCOUNT_SIZE, limit, None, None, None, None, extras=False,
                                   developer="5 min chart developer", stage_days=stage_days)
        moved_dead = sum(
            1 for before, after in zip([base] + calls, calls)
            if before.get("dead_level") != after.get("dead_level") or before.get("dead_flag") != after.get("dead_flag")
        )
        return calls, trials, moved_dead

    def test_a_stage_stops_spending_runs_on_inputs_that_change_nothing(self):
        old_calls, old_trials, old_dead = self._search(stage_days=30, tracked=False)
        calls, trials, dead = self._search(stage_days=30)
        # Each dead direction is tried once: the switch flipped, the level up, the level down.
        self.assertLessEqual(dead, 4)
        self.assertGreater(old_dead, 10)
        self.assertGreater(len(trials), len(old_trials))
        self.assertLess(len(calls), len(old_calls))

    def test_repeats_that_found_a_dead_input_do_not_end_the_year_search(self):
        _calls, old_trials, _ = self._search(tracked=False)
        calls, trials, dead = self._search()
        self.assertEqual(len(old_trials), 1)
        self.assertEqual(len(trials), 20)
        self.assertLessEqual(dead, 4)
        # The dead inputs stay where they were while length is searched.
        self.assertTrue(all(row["params"]["dead_level"] == 5.0 and row["params"]["dead_flag"] is True for row in trials))

    def test_no_search_leaves_a_tracker_behind(self):
        self._search(stage_days=30, limit=3)
        self.assertIsNone(input_effects.current())


class ChartIndexTests(unittest.TestCase):
    def test_a_window_is_the_same_bars_the_full_conversion_gives(self):
        from futuresfund.backtrader_engine import _Chart, _bars_from
        from futuresfund.objective import window_start

        rows = _rows(4000, step=900)
        rows[10]["t"] = "not a time"
        rows[20], rows[21] = rows[21], rows[20]
        every = _bars_from(rows)
        chart = _Chart(rows)
        self.assertEqual(chart.epochs, [bar.epoch for bar in every])
        for sessions in (5, 21, None):
            start = window_start((bar.epoch for bar in every), sessions)
            expected = [bar for bar in every if bar.epoch >= start]
            first = chart.first(sessions)
            self.assertEqual(chart.bars(first), expected)
        self.assertIs(chart.bars(chart.first(5)), chart.bars(chart.first(5)))

    def test_the_engine_reads_a_new_list_of_rows_as_a_new_chart(self):
        from futuresfund.backtrader_engine import _chart_of

        rows = _rows(300)
        first = _chart_of(rows)
        self.assertIs(_chart_of(rows), first)
        rows.append({"t": rows[-1]["t"] + 300, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})
        self.assertIsNot(_chart_of(rows), first)
        self.assertIsNot(_chart_of(list(rows)), _chart_of(rows))

    def test_the_desk_does_not_read_every_bar_before_it_sends_a_run(self):
        from futuresfund import backtrader_engine

        class Desk:
            def run(self, *args, **kwargs):
                return {"net_profit": 1.0, "trades": 1}

        with mock.patch("futuresfund.engine_workers.active_desk", return_value=Desk()), \
                mock.patch.object(backtrader_engine, "_bars_from", side_effect=AssertionError("read every bar")):
            result = backtrader_engine.run_script('strategy("x")\nv = volume\n', _rows(500), "5m", {}, None, "ES", "x", 0)
        self.assertEqual(result["net_profit"], 1.0)

    def test_a_chart_with_no_volume_is_still_blocked(self):
        from futuresfund import backtrader_engine

        rows = [{**row, "v": 0} for row in _rows(200)]
        result = backtrader_engine.run_script('strategy("x")\nv = volume\n', rows, "5m", {}, None, "ES", "x", 0)
        self.assertTrue(result["blocked"])


@unittest.skipUnless(shutil.which("node"), "PineTS needs Node")
class ServerTests(unittest.TestCase):
    def setUp(self):
        from futuresfund import pinets_engine

        self.engine = pinets_engine
        pinets_engine.close_server()
        self.addCleanup(pinets_engine.close_server)
        self.bars = [Bar(row["t"], row["o"], row["h"], row["l"], row["c"], row["v"]) for row in _rows(240)]

    def _once(self, source, bars):
        with mock.patch.dict(os.environ, {"FUTURES_PINETS_SERVER": "0"}):
            return self.engine.run_pinets(source, bars, "5m", "ES")

    def test_the_server_takes_the_trades_one_process_per_run_takes(self):
        once = self._once(MOVES, self.bars)
        served = self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:0")
        self.assertTrue(once)
        self.assertEqual(served, once)
        other = MOVES.replace("input.int(3,", "input.int(4,")
        self.assertEqual(self.engine.run_pinets(other, self.bars, "5m", "ES", candles_key="test:0"), self._once(other, self.bars))

    def test_the_next_attempt_on_a_window_sends_only_the_script(self):
        self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:1")
        server = self.engine._server
        process = server.proc
        sent = []
        original = server._ask

        def ask(message):
            sent.append("candles" in message)
            return original(message)

        with mock.patch.object(server, "_ask", side_effect=ask):
            again = self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:1")
        self.assertEqual(sent, [False])
        self.assertIs(server.proc, process)
        self.assertEqual(again, self._once(MOVES, self.bars))

    def test_a_script_error_is_a_script_error_and_the_server_keeps_running(self):
        self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:2")
        process = self.engine._server.proc
        with self.assertRaises(self.engine.PineTSScriptError):
            self.engine.run_pinets(BROKEN, self.bars, "5m", "ES", candles_key="test:2")
        self.assertIs(self.engine._server.proc, process)
        self.assertTrue(self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:2"))

    def test_a_server_that_stopped_is_started_again(self):
        self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:3")
        self.engine._server.proc.kill()
        self.engine._server.proc.wait(timeout=10)
        self.assertEqual(self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:3"), self._once(MOVES, self.bars))

    def test_a_backtest_running_past_the_limit_is_stopped(self):
        self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:4")
        with mock.patch.object(self.engine, "RUN_SECONDS", 0.0):
            with self.assertRaises(self.engine.PineTSError):
                self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:4")
        self.assertTrue(self.engine.run_pinets(MOVES, self.bars, "5m", "ES", candles_key="test:4"))


class BetweenRunsTests(unittest.TestCase):
    def test_the_chart_folders_are_scanned_once_a_minute(self):
        from futuresfund import charts

        charts._roots_seen.update(at=0.0, roots=None)
        self.addCleanup(charts._roots_seen.update, at=0.0, roots=None)
        with mock.patch.object(charts, "_scan_roots", return_value=["ES", "NQ"]) as scan:
            self.assertEqual(charts.chart_roots(), ["ES", "NQ"])
            self.assertEqual(charts.chart_roots(), ["ES", "NQ"])
            self.assertEqual(scan.call_count, 1)
            charts._roots_seen["at"] = time.monotonic() - 61
            charts.chart_roots()
            self.assertEqual(scan.call_count, 2)

    def test_a_note_is_written_as_one_line_of_json(self):
        text = learn._notes_json({"instruments": {"ES": {"charts": {"5m": {"trials": [{"net_profit": 1.5}]}}}}})
        self.assertNotIn("\n", text)
        self.assertEqual(json.loads(text)["instruments"]["ES"]["charts"]["5m"]["trials"][0]["net_profit"], 1.5)

    def test_headquarters_renders_a_script_only_for_a_run_that_beats_it(self):
        from futuresfund import strategy

        book = {"strategy": {}, "headquarters_research": {"net_profit": 99999, "max_drawdown": 10, "trades": 300, "days": 260}}
        row = {"engine": "backtrader", "trades": 50, "net_profit": 10, "max_drawdown": 5000, "timeframe": "5m", "days": 260}
        with mock.patch.object(strategy, "load", return_value=book), mock.patch.object(strategy, "save") as save, \
                mock.patch.object(strategy, "_pine_for", side_effect=AssertionError("rendered")):
            strategy.remember_research(row, "x.txt", "X")
        save.assert_not_called()

    def test_the_entry_rule_reads_the_bars_only_when_it_writes_a_rule(self):
        from futuresfund import entry_rules

        with mock.patch.object(entry_rules, "_ready", return_value=True):
            self.assertTrue(entry_rules.ensure_entry_rule(object(), "dev", 'strategy("x")', lambda: (_ for _ in ()).throw(AssertionError("read"))))


if __name__ == "__main__":
    unittest.main()
