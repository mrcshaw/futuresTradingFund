"""Every test needs the library boxes pro-rated to its share of a 260-session year, and the page shows the week."""

import inspect
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import objective

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "futuresfund" / "static"

LIMITS = {
    "min_profit": 6000.0, "profit_factor": 1.1, "win_rate": 40.0, "min_trades": 60, "drawdown_pct": 2.0,
    "goal": "profit_factor",
    "use_drawdown": True, "use_trades": True, "use_profit": True, "use_factor": True, "use_win_rate": True,
}


def _limits(test, **values):
    """The acceptance boxes for this test only. The desk's saved book is never read."""
    limits = {**LIMITS, **values}
    patcher = mock.patch("futuresfund.book.strategy_acceptance", lambda *args, **kwargs: dict(limits))
    patcher.start()
    test.addCleanup(patcher.stop)
    return limits


def _row(profit, trades, drawdown=100.0, days=None, **values):
    row = {"net_profit": profit, "trades": trades, "max_drawdown": drawdown, "profit_factor": 1.5, "win_rate": 50.0, "biggest_loser": -150.0}
    if days is not None:
        row["days"] = days
    row.update(values)
    return row


class StageBarTests(unittest.TestCase):
    def setUp(self):
        _limits(self)

    def test_a_test_is_its_sessions_out_of_260(self):
        self.assertEqual(objective.STAGE_SESSIONS, {7: 5, 30: 21, 90: 64, 365: 260})
        self.assertEqual(objective.stage_share(None), 1.0)
        self.assertAlmostEqual(objective.stage_share(7), 5 / 260)
        self.assertAlmostEqual(objective.stage_share(90), 64 / 260)
        # Another day count is its trading days, five in every seven.
        self.assertAlmostEqual(objective.stage_sessions(14), 10.0)
        self.assertAlmostEqual(objective.stage_share(14), 10 / 260)

    def test_6000_and_60_trades_are_115_and_2_on_the_7_day_test(self):
        bars = {days: objective.stage_bars(days, LIMITS) for days in (7, 30, 90, 365)}
        self.assertEqual([round(bars[days]["profit"]) for days in bars], [115, 485, 1477, 6000])
        self.assertEqual([bars[days]["min_trades"] for days in bars], [2, 5, 15, 60])
        self.assertEqual([bars[days]["max_trades"] for days in bars], [11, 48, 147, 600])
        year = objective.stage_bars(None, LIMITS)
        self.assertEqual(year["min_trades"], 60)
        self.assertAlmostEqual(year["weekly_profit"], 6000 / 52, places=2)
        self.assertAlmostEqual(year["weekly_trades"], 60 / 52, places=3)

    def test_100_trades_a_year_is_2_9_25_and_100(self):
        limits = {**LIMITS, "min_trades": 100}
        self.assertEqual([objective.stage_bars(days, limits)["min_trades"] for days in (7, 30, 90, None)], [2, 9, 25, 100])

    def test_the_drawdown_the_stop_and_the_ratios_are_not_scaled(self):
        week, year = objective.stage_bars(7, LIMITS), objective.stage_bars(None, LIMITS)
        for name in ("drawdown_start", "drawdown_target", "drawdown_share", "trade_loss", "profit_factor", "win_rate"):
            self.assertEqual(week[name], year[name], name)
        self.assertEqual(week["drawdown_start"], 2500.0)
        self.assertEqual(week["trade_loss"], 750.0)
        # A 7-day window is held to the year's drawdown rule, so a $2,600 dip on a $400 week fails.
        self.assertIn("drawdown", objective.acceptance_misses(_row(400, 3, drawdown=2600), days=7, limits=LIMITS))
        self.assertNotIn("drawdown", objective.acceptance_misses(_row(400, 3, drawdown=2400), days=7, limits=LIMITS))

    def test_a_box_that_is_off_asks_for_nothing_but_a_profit_and_four_trades_a_year(self):
        off = {**LIMITS, "use_profit": False, "use_trades": False}
        week = objective.stage_bars(7, off)
        self.assertEqual(week["profit"], 0.0)
        self.assertEqual(week["min_trades"], 1)
        self.assertEqual(objective.stage_bars(None, off)["min_trades"], objective.MIN_TRADES)
        self.assertNotIn("trades", objective.acceptance_misses(_row(50, 1), days=7, limits=off))
        self.assertIn("trades", objective.acceptance_misses(_row(9000, 3, days=260), limits=off))


class StageCheckTests(unittest.TestCase):
    def setUp(self):
        _limits(self)

    def test_the_stage_profit_is_the_year_times_its_share(self):
        self.assertIn("profit", objective.acceptance_misses(_row(115, 3, days=5), days=7))
        self.assertNotIn("profit", objective.acceptance_misses(_row(116, 3, days=5), days=7))
        self.assertIn("profit", objective.acceptance_misses(_row(484, 6, days=21), days=30))
        self.assertNotIn("profit", objective.acceptance_misses(_row(485, 6, days=21), days=30))

    def test_the_stage_trade_count_comes_from_the_library_only(self):
        self.assertTrue(objective.stage_passed(_row(300, 2, days=5), 7))
        self.assertFalse(objective.stage_passed(_row(300, 1, days=5), 7))
        self.assertTrue(objective.stage_passed(_row(1500, 15, days=64), 90))
        self.assertFalse(objective.stage_passed(_row(1500, 14, days=64), 90))

    def test_the_600_a_year_cap_holds_at_every_stage(self):
        self.assertTrue(objective.stage_passed(_row(300, 11, days=5), 7))
        missed = objective.acceptance_misses(_row(300, 12, days=5), days=7)
        self.assertEqual(set(missed), {"pace"})
        self.assertEqual(missed["pace"], 624.0)

    def test_the_year_needs_the_library_count_and_no_hidden_weekly_floor(self):
        # 60 trades on 260 sessions is about 1.2 a week. The old rule asked for 156.
        self.assertTrue(objective.passes_acceptance(_row(9000, 60, days=260)))
        self.assertIn("trades", objective.acceptance_misses(_row(9000, 59, days=260)))
        with mock.patch.object(objective, "STAGE_TRADES_PER_WEEK", 3):
            self.assertIn("trades", objective.acceptance_misses(_row(9000, 155, days=260)))
            self.assertTrue(objective.passes_acceptance(_row(9000, 156, days=260)))

    def test_a_row_is_judged_on_the_window_the_caller_names(self):
        # A stage row judged without days is a year, so the scan and the created-script check keep the year bar.
        row = _row(300, 3, days=5, stage_days=7)
        self.assertFalse(objective.acceptance_misses(row, days=7))
        self.assertIn("profit", objective.acceptance_misses(row))
        self.assertNotIn("stage_days", inspect.getsource(objective.acceptance_misses).split('"""')[-1])

    def test_the_email_ratio_counts_trades_by_the_library_number(self):
        # 100 trades a year is under the old 130 band but over the library's 60.
        row = {"net_profit": 9000.0, "max_drawdown": 2000.0, "trades": 100, "days": 260}
        self.assertTrue(objective.ratio_success(row))
        self.assertFalse(objective.ratio_success({**row, "trades": 59}))
        self.assertFalse(objective.ratio_success({**row, "trades": 700}))


class StageWordsTests(unittest.TestCase):
    def setUp(self):
        _limits(self)

    def test_a_missed_stage_says_which_bar_with_the_stage_numbers(self):
        row = _row(80, 1, days=5)
        lines = objective.stage_miss_lines(row, 7)
        self.assertIn("The 7-day test needs at least 2 trades in 7 days (60 a year at a steady pace). This run took 1.", lines)
        self.assertIn("The 7-day test needs more than $115 profit in 7 days (a $6,000 year at a steady pace). This run made $80.", lines)
        self.assertEqual(objective.acceptance_reason(_row(80, 3, days=5), 7), lines[1])

    def test_the_stage_reasons_name_the_cap_the_drawdown_and_the_ratios(self):
        busy = objective.acceptance_reason(_row(300, 14, days=5), 7)
        self.assertIn("allows at most 11 trades in 7 days (600 a year at a steady pace)", busy)
        deep = objective.acceptance_reason(_row(300, 3, drawdown=3000, days=5), 30)
        self.assertIn("held to the same limit as the year", deep)
        thin = objective.acceptance_reason(_row(900, 9, days=21, profit_factor=1.0), 30)
        self.assertIn("The 30-day test needs a profit factor above 1.1, the same as the year", thin)

    def test_the_year_reason_names_the_library_count(self):
        reason = objective.acceptance_reason(_row(9000, 50, days=260))
        self.assertEqual(reason, "The library keeps a strategy after at least 60 trades. This run has 50, so it was not filed.")
        self.assertEqual(objective.acceptance_reason(_row(9000, 60, days=260)), "")

    def test_the_acceptance_line_gives_the_week_and_each_test(self):
        line = objective.acceptance_line()
        self.assertIn("That is $115 a week and about 1.2 trades a week at a steady pace.", line)
        self.assertIn("The 7, 30, and 90-day tests need more than $115, $485, and $1,477 profit and at least 2, 5, and 15 trades.", line)
        self.assertNotIn("at least 3 times a week", line)
        self.assertIn("no more than 600 times a year", line)
        with mock.patch.object(objective, "STAGE_TRADES_PER_WEEK", 3):
            self.assertIn("trade at least 3 times a week", objective.acceptance_line())

    def test_the_bars_sentence_for_a_post(self):
        text = objective.stage_bars_text(7)
        self.assertIn("The 7-day test needs more than $115 profit, at least 2 and at most 11 trades", text)
        self.assertIn("the same as the year", text)


class SearchPostTests(unittest.TestCase):
    def setUp(self):
        _limits(self)
        from futuresfund import learn

        patcher = mock.patch.object(learn, "_ensure_worker", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(learn.drain_decisions)

    def test_the_stage_post_names_the_missed_bar(self):
        from futuresfund import learn
        from futuresfund import script_diagnosis as diagnosis
        from futuresfund.board import Board

        board = Board()

        def execute(params):
            return {"net_profit": 80.0 + float(params.get("length", 0)), "max_drawdown": 50.0, "trades": 3, "days": 5, "profit_factor": 1.5, "win_rate": 50.0}

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            script = folder / "s.pine"
            script.write_text('//@version=5\nstrategy("S")\n', encoding="utf-8")
            with mock.patch.object(diagnosis, "HOME", folder / "d"), mock.patch.object(learn, "LEARNING", folder), \
                    mock.patch.object(learn, "NOTES", folder / "notes"), \
                    mock.patch("futuresfund.attempt_reason.reasoned_change", lambda *args, **kwargs: None):
                learn._search({"length": 10}, execute, 50000, 2, board, script, None, stage_days=7)
                learn.drain_decisions()
        posts = [message["text"] for message in board.messages if "did not pass the 7-day stage" in message["text"]]
        self.assertTrue(posts)
        self.assertIn("needs more than $115 profit in 7 days (a $6,000 year at a steady pace)", posts[0])


class GapReportTests(unittest.TestCase):
    def test_a_stage_trial_is_held_to_its_own_stage(self):
        from futuresfund.acceptance_gaps import measure

        limits = {**LIMITS, "min_profit": 8000.0, "min_trades": 80, "profit_factor": 1.05}
        trials = [
            # Passes the 7-day test ($154 and 2 trades), though it would miss the year by far.
            *[{**_row(200, 5, days=5), "stage_days": 7} for _ in range(3)],
            *[{**_row(100, 5, days=5), "stage_days": 7} for _ in range(2)],
            {**_row(9000, 160, days=260)},
        ]
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "sample.json").write_text(json.dumps({"trials": trials}), encoding="utf-8")
            report = measure(Path(folder), limits, 2_500.0)
        self.assertEqual(report["trials"], 6)
        self.assertEqual(report["passed"], 4)
        self.assertEqual(report["rows"][0]["reason"], "Profit on the 7-day test")
        self.assertEqual(report["rows"][0]["acceptance"], "Above $154 in 7 days (a $8,000 year)")
        self.assertEqual(report["rows"][0]["misses"], "2 of 5")
        self.assertIn("2 of 5 7-day tests miss it.", report["lines"][0])


class LibraryPageTests(unittest.TestCase):
    def test_the_book_sends_what_each_test_needs(self):
        from futuresfund import book

        view = book.acceptance_view({"strategy_acceptance": LIMITS})
        self.assertEqual([bar["days"] for bar in view["acceptance_stages"]], [7, 30, 90, 365])
        self.assertEqual([bar["min_trades"] for bar in view["acceptance_stages"]], [2, 5, 15, 60])
        rules = view["acceptance_rules"]
        self.assertEqual(rules["stage_sessions"], {"7": 5.0, "30": 21.0, "90": 64.0, "365": 260.0})
        self.assertEqual((rules["trades_year_max"], rules["weeks_per_year"], rules["research_sessions"]), (600, 52, 260))
        self.assertEqual(rules["trades_per_week_floor"], 0.0)
        self.assertIn("**acceptance_view(book)", inspect.getsource(book.snapshot))

    def test_the_page_uses_the_same_formula_as_python(self):
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        python = inspect.getsource(objective.stage_bars)
        for formula in ("math.ceil(library_count * share - 1e-9)", "math.ceil(MIN_TRADES * share - 1e-9)",
                        "math.ceil(floor_week * sessions / 5 - 1e-9)", "math.floor(TRADES_YEAR_MAX * share + 1e-9)",
                        "min_profit * share"):
            self.assertIn(formula, python)
        for formula in ("const share = count / yearSessions;", "Math.ceil(minTrades * share - 1e-9)", "Math.ceil(least * share - 1e-9)",
                        "Math.ceil(floorWeek * count / 5 - 1e-9)", "Math.floor(most * share + 1e-9)", "minProfit * share"):
            self.assertIn(formula, js)
        self.assertIn('acceptanceForm.addEventListener("input", () => renderAcceptancePace(acceptanceForm));', js)
        self.assertIn("state.book.acceptance_rules", js)

    @unittest.skipUnless(shutil.which("node"), "the page formula runs in Node")
    def test_the_page_formula_gives_the_python_numbers(self):
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        start = js.index("const ACCEPTANCE_STAGE_DAYS")
        end = js.index("function renderAcceptancePace")
        code = js[start:end]
        from futuresfund import book

        cases = [(6000, 60, True, True, 0), (20000, 100, True, True, 0), (5000, 60, False, False, 0), (6000, 60, True, True, 3), (1234.5, 7, True, True, 1.5)]
        for profit, trades, use_profit, use_trades, floor in cases:
            limits = {**LIMITS, "min_profit": profit, "min_trades": trades, "use_profit": use_profit, "use_trades": use_trades}
            with self.subTest(profit=profit, trades=trades, floor=floor), mock.patch.object(objective, "STAGE_TRADES_PER_WEEK", floor):
                view = book.acceptance_view({"strategy_acceptance": limits})
                form = {"min_profit": {"value": str(profit)}, "min_trades": {"value": str(trades)},
                        "use_profit": {"checked": use_profit}, "use_trades": {"checked": use_trades}}
                script = code + f"\nconsole.log(JSON.stringify(acceptanceBars({json.dumps(form)}, {json.dumps(view['acceptance_rules'])})));\n"
                done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                page = json.loads(done.stdout)
                for shown, bar in zip(page, view["acceptance_stages"]):
                    self.assertEqual(shown["days"], bar["days"])
                    self.assertEqual(shown["min_trades"], bar["min_trades"])
                    self.assertEqual(shown["max_trades"], bar["max_trades"])
                    self.assertAlmostEqual(shown["profit"], bar["profit"], places=1)

    def test_the_library_page_shows_the_pace_and_the_table(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-pace="min_profit"', html)
        self.assertIn('data-pace="min_trades"', html)
        self.assertIn('id="acceptance-stages"', html)
        self.assertIn("What each test needs", html)
        for header in ("7 days", "30 days", "90 days", "1 year"):
            self.assertIn(f"<th scope=\"col\">{header}</th>", html)
        library = html[html.index('id="library"'):html.index('id="untested-table"')]
        self.assertNotIn("3 times a week", library)
        self.assertNotIn("900 trades is not saved", library)
        self.assertNotIn("there is no drawdown limit until the profit reaches 5 percent", library)
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        for label in ('"Profit"', '"Fewest trades"', '"Most trades"', '"Drawdown"', "\"One trade's loss\"", '"Profit factor"', '"Win rate"'):
            self.assertIn(label, js)
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".acceptance-stages", css)
        self.assertIn(".rules-form .rule-pace", css)

    def test_the_workflow_mission_bar_says_the_weekly_pace(self):
        js = (STATIC / "workflow.js").read_text(encoding="utf-8")
        self.assertIn("function weeklyPace()", js)
        self.assertIn("ui.live.book?.acceptance_stages", js)
        hud = js[js.index("function renderHud()"):]
        self.assertIn("weeklyPace()", hud[: hud.index("setHtml(ui.els.objectives")])
        self.assertNotIn("N/365", js)
        self.assertRegex(js, re.compile(r'\["stage_trades_per_week", "[^"]+", "goal", "float", 0, 10, 0\.5, "trades a week", 0,'))


if __name__ == "__main__":
    unittest.main()
