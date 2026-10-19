"""A chart far under 4 trades a week changes the session or the average length."""

import unittest
from pathlib import Path


CASH = 'inSession = not na(time(timeframe.period, "0930-1600", "America/New_York"))'
CROSS = "emaSmaCross = ta.crossover(ta.ema(close, emaLength), ta.sma(close, smaLength))"
VALUE_AREA = "\n".join([
    'strategy("Day Shift Delta Volume Breakout + VAH/VAL Zones", overlay=true)',
    "// ENTRY: Mean Reversion at VAH/VAL Zones",
    "// Long: RSI oversold + price near VAL zone",
    'sess_start_hr = input.int(8, "Start Hour (ET)")',
    'sess_start_mn = input.int(30, "Start Minute")',
    'sess_end_hr = input.int(15, "End Hour (ET)")',
    'sess_end_mn = input.int(0, "End Minute")',
    'ema_trend_len = input.int(50, "EMA Length")',
    'cooldown_bars = input.int(10, "Cooldown (bars)")',
    'min_delta_pct = input.float(10, "Min Delta")',
    "bool in_session = bar_t >= s_start and bar_t < s_end",
    "float ema_trend = ta.ema(close, ema_trend_len)",
    "bool long_sig = in_session and rsi_val <= rsi_os and min_delta_pct > 0 and cooldown_bars > 0",
])


class SessionRewriteTests(unittest.TestCase):
    def test_a_cash_session_becomes_the_full_futures_session(self):
        from futuresfund.frequency import apply_session, found_cash_session

        self.assertEqual(found_cash_session(CASH), "0930-1600")
        rewritten = apply_session(CASH, "1800-1600")
        self.assertIn('time(timeframe.period, "1800-1600", "America/New_York")', rewritten)
        self.assertNotIn("0930-1600", rewritten)

    def test_an_overnight_window_and_an_input_default_stay(self):
        from futuresfund.frequency import apply_session, cash_window

        source = "\n".join([
            'sessionStart = input.session("0930-1600", title="Session")',
            'inAsia = not na(time(timeframe.period, "1800-0800", "America/New_York"))',
            CASH,
        ])
        rewritten = apply_session(source, "1800-1600")
        self.assertIn('input.session("0930-1600"', rewritten)
        self.assertIn('"1800-0800"', rewritten)
        self.assertIn('"1800-1600"', rewritten)
        self.assertFalse(cash_window("1800-1600"))
        self.assertFalse(cash_window("1800-0800"))

    def test_the_engine_runs_the_widened_session(self):
        from unittest import mock

        from futuresfund.learn import _engine_execute

        captured = {}

        def run_script(source, *args, **kwargs):
            captured["source"] = source
            return {"net_profit": 1, "max_drawdown": 1, "trades": 1}

        with mock.patch("futuresfund.backtrader_engine.run_script", run_script):
            execute = _engine_execute(CASH, {"15m": [{}] * 80}, {"title": "Day shift", "inputs": []}, None, "GC", timeframe="15m")
            execute({"session_window": "1800-1600", "emaLength": 25})
        self.assertIn("1800-1600", captured["source"])
        self.assertNotIn("0930-1600", captured["source"])


class FrequencyPlanTests(unittest.TestCase):
    def _row(self, params, trades, days=1291):
        return {"params": params, "trades": trades, "days": days, "net_profit": 1, "max_drawdown": 100}

    def test_about_180_trades_in_five_years_widens_the_session_first(self):
        from futuresfund.frequency import frequency_plan, frequency_reason

        origin = {"emaLength": 25, "smaLength": 10, "profile_lookback": 120}
        trials = [self._row(origin, 182)]
        plan = frequency_plan(origin, trials, CASH, {"emaLength": (10, 100), "smaLength": (10, 100)})
        self.assertEqual(plan["session_window"], "1800-1600")
        self.assertEqual(plan["emaLength"], 25)
        self.assertEqual(plan["smaLength"], 10)
        self.assertEqual(plan["profile_lookback"], 120)
        self.assertIn("1800-1600", frequency_reason(origin, plan, CASH))
        self.assertIn("far under", frequency_reason(origin, plan, CASH))

    def test_after_the_session_is_wide_the_longer_average_is_cut_in_half(self):
        from futuresfund.frequency import frequency_plan

        origin = {"emaLength": 25, "smaLength": 40, "profile_lookback": 120, "session_window": "1800-1600"}
        trials = [self._row(origin, 182)]
        plan = frequency_plan(origin, trials, CASH + "\n" + CROSS, {"emaLength": (10, 100), "smaLength": (10, 100)})
        self.assertEqual(plan["smaLength"], 20)
        self.assertEqual(plan["emaLength"], 25)
        self.assertEqual(plan["profile_lookback"], 120)

    def test_ema_25_with_a_minimum_of_10_becomes_12(self):
        from futuresfund.frequency import frequency_plan, frequency_reason

        origin = {"emaLength": 25, "smaLength": 10, "session_window": "1800-1600"}
        trials = [self._row(origin, 182)]
        bounds = {"emaLength": (10, 100), "smaLength": (10, 100)}
        source = CASH + "\nemaSmaCross = ta.crossover(ema, sma)"
        plan = frequency_plan(origin, trials, source, bounds)
        self.assertEqual(plan["emaLength"], 12)
        self.assertEqual(plan["smaLength"], 10)
        self.assertIn("follows price more closely", frequency_reason(origin, plan, source))

    def test_a_cut_that_adds_trades_keeps_shortening_the_same_length(self):
        from futuresfund.frequency import frequency_plan

        first = {"emaLength": 25, "smaLength": 40, "session_window": "1800-1600"}
        second = {"emaLength": 25, "smaLength": 20, "session_window": "1800-1600"}
        trials = [self._row(first, 182), self._row(second, 400)]
        plan = frequency_plan(first, trials, CASH + "\n" + CROSS, {"emaLength": (10, 100), "smaLength": (10, 100)})
        self.assertEqual(plan["smaLength"], 10)
        self.assertEqual(plan["emaLength"], 25)

    def test_a_cut_that_does_not_add_trades_moves_to_the_other_average(self):
        from futuresfund.frequency import frequency_plan

        first = {"emaLength": 25, "smaLength": 40, "session_window": "1800-1600"}
        second = {"emaLength": 25, "smaLength": 20, "session_window": "1800-1600"}
        trials = [self._row(first, 182), self._row(second, 182)]
        plan = frequency_plan(first, trials, CASH + "\n" + CROSS, {"emaLength": (10, 100), "smaLength": (10, 100)})
        self.assertEqual(plan["emaLength"], 12)
        self.assertEqual(plan["smaLength"], 20)

    def test_a_week_already_near_the_floor_is_left_to_the_ordinary_walk(self):
        from futuresfund.frequency import frequency_plan

        origin = {"emaLength": 25, "profile_lookback": 120}
        near = [self._row(origin, 900)]
        paced = [self._row({"emaLength": 25}, 20, days=20)]
        self.assertIsNone(frequency_plan(origin, near, CASH))
        self.assertIsNone(frequency_plan({"emaLength": 25}, paced, CASH))
        self.assertIsNone(frequency_plan(origin, [self._row(origin, 182, days=None)], CASH))

    def test_the_search_widens_the_session_before_it_nudges_another_input(self):
        from futuresfund.learn import _search

        seen = []

        def execute(params):
            seen.append(dict(params))
            trades = 182
            if params.get("session_window") == "1800-1600":
                trades = 400
            if int(params.get("emaLength") or 0) <= 12:
                trades = 700
            return {"net_profit": float(trades), "max_drawdown": 1000.0, "trades": trades, "days": 1291}

        facts = {"inputs": [
            {"name": "emaLength", "min": 10, "max": 100},
            {"name": "smaLength", "min": 10, "max": 100},
            {"name": "profile_lookback", "min": 20, "max": 200},
        ]}
        trials = _search(
            {"emaLength": 25, "smaLength": 10, "profile_lookback": 120},
            execute,
            50000,
            3,
            None,
            None,
            None,
            facts,
            extras=False,
            source=CASH + "\n" + CROSS,
        )
        self.assertEqual(len(trials), 3)
        self.assertNotIn("session_window", seen[0])
        self.assertEqual(seen[1]["session_window"], "1800-1600")
        self.assertEqual(seen[1]["emaLength"], 25)
        self.assertEqual(seen[1]["profile_lookback"], 120)
        self.assertEqual(seen[2]["session_window"], "1800-1600")
        self.assertEqual(seen[2]["emaLength"], 12)
        self.assertEqual(seen[2]["profile_lookback"], 120)


class DeadScriptTests(unittest.TestCase):
    def test_a_script_that_never_trades_is_left_after_the_entry_conditions(self):
        from futuresfund.frequency import _uses_cross, far_under
        from futuresfund.learn import _search

        source = "\n".join([
            'strategy("POC RSI Momentum v4", overlay=true)',
            'len_ema = input.int(50, "EMA Length", minval=10, maxval=200)',
            'cooldown_bars = input.int(3, "Cooldown Bars", minval=1, maxval=20)',
            "ema = ta.ema(close, len_ema)",
            "long_cond = close > ema and not ta.crossover(close, ema)",
            'inSession = not na(time(timeframe.period, "0930-1600", "America/New_York"))',
            "if inSession and bar_index > cooldown_bars",
            '    strategy.entry("Long", strategy.long)',
        ])
        self.assertFalse(_uses_cross(source))
        self.assertTrue(far_under({"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}))
        seen = []

        def execute(params):
            seen.append(dict(params))
            return {"net_profit": 0.0, "max_drawdown": 0.0, "trades": 0}

        trials = _search(
            {"len_ema": 50, "cooldown_bars": 3},
            execute,
            50000,
            30,
            None,
            None,
            None,
            {"inputs": [
                {"name": "len_ema", "min": 10, "max": 200},
                {"name": "cooldown_bars", "min": 1, "max": 20},
            ]},
            extras=False,
            source=source,
        )
        self.assertEqual(len(seen), 3)
        self.assertNotIn("session_window", seen[0])
        self.assertEqual(seen[1]["session_window"], "1800-1600")
        self.assertEqual(seen[1]["len_ema"], 50)
        self.assertEqual(seen[1]["cooldown_bars"], 3)
        self.assertEqual(seen[2]["session_window"], "1800-1600")
        self.assertEqual(seen[2]["cooldown_bars"], 2)
        self.assertEqual(seen[2]["len_ema"], 50)
        self.assertTrue(trials[0]["skipped"])
        self.assertIn("no trades", trials[0]["note"])
        self.assertIn("rewrite", trials[0]["note"])


class GuidanceTests(unittest.TestCase):
    def test_the_research_guide_is_one_list(self):
        from futuresfund.learn import research_guidelines

        text = research_guidelines()
        self.assertEqual(text.count("Limit risk"), 1)
        self.assertEqual(text.count("Then raise profit"), 1)
        self.assertIn("18:00 to 16:00 New York", text)
        self.assertIn("Flat the trade when the session ends.", text)
        self.assertIn("Every trade has a dollar stop.", text)
        self.assertIn("3 percent of the account", text)
        self.assertIn("3.6 percent", text)
        self.assertIn("$1,500", text)
        self.assertIn("larger than the stop", text)
        self.assertIn("$2,000", text)
        self.assertIn("under 5 percent of the account", text)
        self.assertIn("$3,000", text)
        self.assertIn("1.5 times the drawdown", text)
        self.assertIn("stop_dollars", text)
        self.assertIn("No ATR stop.", text)
        self.assertIn("Change one input at a time.", text)
        self.assertIn("$250 stop", text)
        self.assertIn("$500 take profit", text)
        self.assertIn("under 2 trades", text)
        self.assertIn("over 600 trades", text)
        self.assertIn("20 attempts", text)
        self.assertIn("learning folder", text)
        self.assertIn("How this script works", text)
        self.assertIn("Every script uses this exit.", text)
        self.assertIn("long_stop = close - stop_dollars / pv", text)
        root = Path(__file__).resolve().parents[1] / "futuresfund"
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "server.py").read_text(encoding="utf-8")
        self.assertIn('<textarea id="research-guide"', html)
        self.assertIn('id="research-guide-save"', html)
        self.assertIn("saveResearchGuide", js)
        self.assertIn('@app.post("/api/research-guide")', server)
        self.assertIn('strategy.close_all(comment="Open P&L > SL Risk")', text)
        self.assertNotIn("What we do not want", text)
        self.assertNotIn("What to change", text)

    def test_a_saved_guide_is_what_the_desks_read(self):
        import tempfile
        from unittest import mock

        from futuresfund import indicator_guidance

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "how_to_research.md"
            path.write_text("Old line.\n", encoding="utf-8")
            with mock.patch.object(indicator_guidance, "GUIDE_PATH", path):
                saved = indicator_guidance.save_guidance("Every script uses this exit.\n")
                self.assertIn("Every script uses this exit.", saved)
                self.assertEqual(indicator_guidance.price_guidance(), saved)
                with self.assertRaises(ValueError):
                    indicator_guidance.save_guidance("   ")
                self.assertIn("Every script uses this exit.", path.read_text(encoding="utf-8"))

    def test_every_library_script_uses_the_standard_session(self):
        root = Path(__file__).resolve().parents[1] / "futuresfund"
        for folder in ("learnedStrategies", "learningStrategies"):
            pines = list((root / folder).glob("*.pine"))
            self.assertTrue(pines)
            for path in pines:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(":12345", text, path.name)
                self.assertNotIn("before1645", text, path.name)
                self.assertIn("strategy.close_all", text, path.name)
                self.assertTrue("sess_start_hr" in text or "in_session" in text, path.name)


class ScriptReadingTests(unittest.TestCase):
    def _row(self, params, trades, days=1291):
        return {"params": params, "trades": trades, "days": days, "net_profit": 1, "max_drawdown": 100}

    def test_a_value_area_script_is_read_before_its_session_hours_change(self):
        from futuresfund.frequency import frequency_plan, frequency_reason, script_reading

        origin = {
            "sess_start_hr": 8, "sess_start_mn": 30, "sess_end_hr": 15, "sess_end_mn": 0,
            "ema_trend_len": 50, "cooldown_bars": 10, "min_delta_pct": 10.0, "profile_lookback": 120,
        }
        reading = script_reading(VALUE_AREA, origin)
        self.assertIn("Mean Reversion at VAH/VAL Zones", reading)
        self.assertIn("8:30", reading)
        self.assertIn("15:00", reading)
        self.assertIn("not a cross", reading)
        plan = frequency_plan(origin, [self._row(origin, 182)], VALUE_AREA, {"cooldown_bars": (0, 100), "min_delta_pct": (0, 80)})
        self.assertEqual(plan["sess_start_hr"], 18)
        self.assertEqual(plan["ema_trend_len"], 50)
        self.assertEqual(plan["profile_lookback"], 120)
        self.assertEqual(plan["cooldown_bars"], 10)
        reason = frequency_reason(origin, plan, VALUE_AREA)
        self.assertIn("8:30", reason)
        self.assertIn("18:00", reason)
        self.assertNotIn("cash", reason.lower())

    def test_after_the_session_opens_the_next_change_is_a_condition_the_entry_requires(self):
        from futuresfund.frequency import frequency_plan

        opened = {
            "sess_start_hr": 18, "sess_start_mn": 30, "sess_end_hr": 15, "sess_end_mn": 0,
            "ema_trend_len": 50, "cooldown_bars": 10, "min_delta_pct": 10.0, "profile_lookback": 120,
        }
        plan = frequency_plan(opened, [self._row(opened, 182)], VALUE_AREA, {"cooldown_bars": (0, 100)})
        self.assertEqual(plan["cooldown_bars"], 5)
        self.assertEqual(plan["ema_trend_len"], 50)
        self.assertEqual(plan["profile_lookback"], 120)


CROSS_STOP = "\n".join([
    "//@version=5",
    "// Built from: EMA cross",
    'strategy("EMA Cross", overlay=true)',
    'fast = input.int(12, "Fast EMA")',
    'slow = input.int(50, "Slow EMA")',
    'stop_dollars = input.int(250, "Stop ($)", minval=250, maxval=1500)',
    "long_sig = ta.crossover(ta.ema(close, fast), ta.ema(close, slow))",
])


class MechanismCommentTests(unittest.TestCase):
    def test_a_script_is_not_given_the_example_until_the_analyst_reads_it(self):
        from futuresfund.frequency import install_mechanism

        noted = install_mechanism(CROSS_STOP)
        self.assertNotIn("How this script works", noted)
        self.assertNotIn("crosses sooner", noted)

    def test_an_accepted_description_is_written_above_the_script(self):
        from futuresfund.frequency import install_mechanism, mechanism_note, script_reading

        lines = [
            "The entry is a moving-average cross of the fast and slow EMAs.",
            "The stop is $250. A smaller length tracks price more closely and crosses sooner, so the trade can finish before that stop.",
        ]
        noted = install_mechanism(CROSS_STOP, lines)
        self.assertIn("// How this script works", noted)
        self.assertLess(noted.index("How this script works"), noted.index("strategy("))
        self.assertEqual(install_mechanism(noted), noted)
        reading = mechanism_note(noted)
        self.assertIn("moving-average cross", reading)
        self.assertIn("$250", reading)
        self.assertIn("tracks price more closely", reading)
        self.assertIn(reading, script_reading(noted, {"fast": 12, "slow": 50}))

    def test_a_cross_description_is_sent_back_when_the_script_does_not_cross(self):
        from futuresfund.script_brief import description_problem

        source = VALUE_AREA + '\nstop_dollars = input.int(250, "Stop ($)")\n'
        problem = description_problem(
            source,
            ["The entry is a moving-average cross. A smaller length tracks price more closely and crosses sooner."],
        )
        self.assertIn("does not enter on a cross", problem)

    def test_the_analyst_reads_the_script_and_the_checker_accepts_a_matching_description(self):
        from unittest.mock import patch

        from futuresfund.board import Board
        from futuresfund.llm import _reasons, model_name
        from futuresfund.roster import ROSTER
        from futuresfund.script_brief import prepare_description

        self.assertTrue(_reasons("Trading Analyst"))
        analyst = next(agent for agent in ROSTER if agent["name"] == "Trading Analyst")
        checker = next(agent for agent in ROSTER if agent["name"] == "Script checker")
        self.assertIn("describes how that script works", " ".join(analyst["responsibilities"]))
        self.assertIn("does not match the code", " ".join(checker["responsibilities"]))
        calls = []

        def fake_post(model, messages, **kwargs):
            calls.append((model, kwargs.get("think")))
            if model == model_name("Trading Analyst"):
                return (
                    "The entry is a moving-average cross of the fast and slow EMAs. "
                    "The stop is $250. A small stop is hit unless the trade is quick. "
                    "A smaller length tracks price more closely and crosses sooner, so the trade can finish before that stop."
                )
            return "ACCURATE"

        board = Board()
        with patch("futuresfund.llm._post", fake_post):
            noted = prepare_description(CROSS_STOP, board)
        self.assertEqual(calls[0], (model_name("Trading Analyst"), True))
        self.assertEqual(calls[1], (model_name("Script checker"), True))
        self.assertIn("How this script works", noted)
        self.assertIn("crosses sooner", noted)
        checker_notes = [message["text"] for message in board.messages if message["author"] == "Script checker"]
        self.assertIn("The description matches this script.", checker_notes)

    def test_a_description_that_invents_a_cross_is_not_written_on_the_script(self):
        from unittest.mock import patch

        from futuresfund.board import Board
        from futuresfund.script_brief import prepare_description

        source = VALUE_AREA + '\nstop_dollars = input.int(250, "Stop ($)")\n'

        def fake_post(model, messages, **kwargs):
            return "The entry is an EMA crossover. A smaller length tracks price more closely and crosses sooner."

        board = Board()
        with patch("futuresfund.llm._post", fake_post):
            noted = prepare_description(source, board)
        self.assertNotIn("How this script works", noted)
        self.assertNotIn("crosses sooner", noted)
        sent = [message["text"] for message in board.messages if message["author"] == "Script checker"]
        self.assertTrue(sent)
        self.assertIn("sent back", sent[0])
