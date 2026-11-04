"""A researcher can redesign a losing script, and the last months stay out of every search."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import attempt_reason, book, learn, objective, redesign
from futuresfund import script_diagnosis as diagnosis
from futuresfund.board import Board

OLD = """//@version=5
strategy("Old Idea", overlay=true)
pv = input.float(50.0, "Point Value ($)")
length = input.int(20, "Length", minval=2, maxval=200)
plot(ta.sma(close, length))
"""

NEW = """//@version=5
strategy("Whatever The Model Called It", overlay=true, initial_capital=50000)
pv = input.float(50.0, "Point Value ($)")
stop_dollars = input.int(500, "Stop ($)", minval=250, maxval=700)
tp_dollars = input.int(1000, "Take Profit ($)", minval=250)
fast = input.int(9, "Fast", minval=2, maxval=50)
slow = input.int(21, "Slow", minval=5, maxval=200)
long_stop = strategy.position_size > 0 ? strategy.position_avg_price - stop_dollars / pv : close - stop_dollars / pv
long_limit = close + tp_dollars / pv
short_stop = strategy.position_size < 0 ? strategy.position_avg_price + stop_dollars / pv : close + stop_dollars / pv
short_limit = close - tp_dollars / pv
if ta.crossover(ta.ema(close, fast), ta.ema(close, slow))
    strategy.entry("Long", strategy.long)
    strategy.exit("XL", "Long", stop=long_stop, limit=long_limit)
if ta.crossunder(ta.ema(close, fast), ta.ema(close, slow))
    strategy.entry("Short", strategy.short)
    strategy.exit("XS", "Short", stop=short_stop, limit=short_limit)
"""


def _default_book(test):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    patcher = mock.patch.object(book, "BOOK_PATH", Path(folder.name) / "book.json")
    patcher.start()
    test.addCleanup(patcher.stop)


class _Folders:
    """A learning folder and notes of their own, so a test never writes to the desk's."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.learning = root / "learningStrategies"
        self.learning.mkdir()
        self.notes = root / "researchNotes"
        self.patches = [
            mock.patch.object(learn, "LEARNING", self.learning),
            mock.patch.object(learn, "NOTES", self.notes),
            mock.patch.object(diagnosis, "HOME", root / "diagnosis"),
            mock.patch.object(learn, "_ensure_worker", lambda: None),
        ]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def script(self, name="Old Idea.txt", text=OLD):
        path = self.learning / name
        path.write_text(text, encoding="utf-8")
        return path


class ReplyTests(unittest.TestCase):
    PLAN = "BECAUSE: The average cross loses in every window.\nREDESIGN:\nEnter on an EMA cross. SETTING: fast = 9 starts it.\nEND"

    def test_a_redesign_is_read_only_when_it_is_open(self):
        found = attempt_reason.parse_reasoned_reply(self.PLAN, {"length": 20}, script=OLD, redesign=True)
        self.assertIn("EMA cross", found["redesign"])
        self.assertIsNone(found["script"])
        self.assertIsNone(attempt_reason.parse_reasoned_reply(self.PLAN, {"length": 20}, script=OLD))

    def test_the_brief_offers_it_only_when_it_is_open(self):
        row = {"params": {"length": 20}, "net_profit": -100.0, "trades": 9}
        self.assertIn("REDESIGN:", attempt_reason._brief(OLD, [row], {"length": 20}, "why", None, True))
        self.assertNotIn("REDESIGN:", attempt_reason._brief(OLD, [row], {"length": 20}, "why"))

    def test_a_redesign_may_add_inputs_and_drop_most_of_the_old_script(self):
        long_old = OLD + "".join(f"x{index} = close + {index}\n" for index in range(60))
        with mock.patch("futuresfund.llm._post", lambda *a, **k: f"SCRIPT:\n{NEW}\nEND"), \
                mock.patch("futuresfund.strategy_team.engine_problem", lambda source: ""):
            written = attempt_reason._write_redesign(long_old, "Use an EMA cross.", "The idea loses.")
        self.assertIn("fast = input.int", written)
        self.assertLess(len(written.splitlines()), len(long_old.splitlines()) + 20)
        self.assertNotEqual(attempt_reason.rewrite_problem(long_old, NEW), "")

    def test_a_redesign_still_holds_the_stop_at_entry_and_closes_at_the_session_end(self):
        with mock.patch("futuresfund.llm._post", lambda *a, **k: f"SCRIPT:\n{NEW}\nEND"), \
                mock.patch("futuresfund.strategy_team.engine_problem", lambda source: ""):
            written = attempt_reason._write_redesign(OLD, "Use an EMA cross.", "The idea loses.")
        self.assertIn("strategy.position_avg_price - stop_dollars / pv", written)
        self.assertIn("strategy.close_all", written)
        self.assertIn("maxval=700", written)

    def test_a_placeholder_is_sent_back_and_then_dropped(self):
        bad = NEW + "// the rest of the script is omitted\n"
        sent = []

        def post(model, messages, **kwargs):
            sent.append(messages)
            return f"SCRIPT:\n{bad}\nEND"

        with mock.patch("futuresfund.llm._post", post), mock.patch("futuresfund.strategy_team.engine_problem", lambda source: ""):
            self.assertIsNone(attempt_reason._write_redesign(OLD, "Plan.", "Why."))
        self.assertEqual(len(sent), 2)
        self.assertIn("was refused", sent[1][-1]["content"])

    def test_an_atr_stop_in_a_redesign_is_converted_not_sent_back(self):
        from futuresfund.strategy_team import atr_exit

        atr_stop = NEW + "stop_mult = input.float(2.0)\natr = ta.atr(14)\nlong_stop := close - atr * stop_mult\n"
        sent = []

        def post(model, messages, **kwargs):
            sent.append(messages)
            return f"SCRIPT:\n{atr_stop}\nEND"

        with mock.patch("futuresfund.llm._post", post), mock.patch("futuresfund.strategy_team.engine_problem", lambda source: ""):
            written = attempt_reason._write_redesign(OLD, "Plan.", "Why.")
        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(written)
        self.assertEqual(atr_exit(written), "")
        self.assertIn("long_stop := close - stop_dollars / pv", written)
        self.assertNotIn("stop_mult", written)
        self.assertNotIn("ta.atr", written)


class InPlaceTests(unittest.TestCase):
    """A redesign replaces the script it answers. No new script is filed, and a worse one is taken back."""

    def setUp(self):
        # These check the logic changes themselves. The desk runs with VALUES_ONLY on, which turns them off.
        from futuresfund import learn

        patcher = mock.patch.object(learn, "VALUES_ONLY", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_redesign_is_noted_on_the_ledger_and_files_no_script(self):
        with _Folders() as folders:
            path = folders.script()
            self.assertTrue(redesign.note_redesign(path, "The idea loses.", "Use an EMA cross."))
            self.assertEqual(sorted(item.name for item in folders.learning.iterdir()), ["Old Idea.txt"])
            row = json.loads((folders.notes / "redesigns" / "ledger.json").read_text(encoding="utf-8"))["Old Idea"][0]
            self.assertEqual((row["file"], row["in_place"], row["because"], row["plan"]), ("Old Idea.txt", True, "The idea loses.", "Use an EMA cross."))
            self.assertIn("Use an EMA cross.", redesign.earlier_lines(path)[0])

    def test_the_limit_counts_only_in_place_redesigns(self):
        with _Folders() as folders:
            path = folders.script()
            ledger = folders.notes / "redesigns" / "ledger.json"
            ledger.parent.mkdir(parents=True)
            old_rows = [{"file": f"Old Idea_redesign{number}.pine", "because": f"Old {number}."} for number in (1, 2, 3)]
            ledger.write_text(json.dumps({"Old Idea": old_rows}), encoding="utf-8")
            self.assertTrue(redesign.room(path))
            self.assertEqual(len(redesign.earlier_lines(path)), 3)
            for number in range(redesign.LIMIT):
                self.assertTrue(redesign.note_redesign(path, f"Try {number}."))
            self.assertFalse(redesign.room(path))
            self.assertFalse(redesign.note_redesign(path, "One more."))

    def test_the_redesign_keeps_the_title_of_the_script_it_replaces(self):
        self.assertIn('strategy("Old Idea", overlay=true, initial_capital=50000)', redesign.keep_title(NEW, OLD))

    def test_a_redesign_already_filed_is_still_the_next_script_a_chart_takes(self):
        with _Folders() as folders:
            folders.script("Aaa.txt")
            (folders.learning / "created_x.pine").write_text(OLD, encoding="utf-8")
            folders.script("Zzz_redesign1.pine", NEW)
            names = [path.name for path in learn._pine_files(folders.learning, False)]
            self.assertEqual(names, ["Zzz_redesign1.pine", "Aaa.txt", "created_x.pine"])

    def test_an_answer_with_a_redesign_waits_for_the_next_chart_as_this_scripts_rewrite(self):
        with _Folders() as folders:
            path = folders.script()
            seen = []

            def reasoned(script, rows, bounds, choices, why, response, can_redesign):
                seen.append(can_redesign)
                return {"because": "The idea loses.", "params": None, "script": None, "redesign": "Use an EMA cross.", "redesigned": NEW}

            trials = [{"params": {"length": 20}, "net_profit": -100.0, "max_drawdown": 80.0, "trades": 5}]
            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                learn.ask_researcher(Board(), path, OLD, trials, {}, "It failed.", None, "Chart researcher", "stage7", review="7 ES 2m")
                learn.drain_decisions()
            self.assertEqual(seen, [True])
            self.assertEqual(sorted(item.name for item in folders.learning.iterdir()), ["Old Idea.txt"])
            self.assertEqual(path.read_text(encoding="utf-8"), OLD)
            answer = diagnosis.load(OLD)["answer"]
            self.assertTrue(answer["redesign"])
            self.assertIn('strategy("Old Idea"', answer["script"])
            self.assertEqual(answer["settings"], {"length": 20})
            self.assertEqual(diagnosis.load(OLD)["reviewed"], ["7 ES 2m"])

    def test_a_redesign_stays_only_when_it_does_better(self):
        before = {"net_profit": 1200.0, "max_drawdown": 600.0, "trades": 40, "days": 30}
        self.assertFalse(learn.redesign_holds(before, {**before, "net_profit": 900.0}))
        self.assertFalse(learn.redesign_holds(before, {**before, "trades": 0, "net_profit": 0.0}))
        self.assertFalse(learn.redesign_holds(before, {**before, "error": "boom", "net_profit": 5000.0}))
        better = {**before, "net_profit": 1800.0, "max_drawdown": 500.0}
        self.assertEqual(learn.redesign_holds(before, better), learn._rank(better) > learn._rank(before))

    def test_a_worse_redesign_puts_the_script_before_it_back(self):
        with _Folders() as folders:
            path = folders.script()
            runs = []

            def execute(params):
                runs.append(execute.source["text"])
                worse = "fast = input.int" in execute.source["text"]
                return {"net_profit": -400.0 if worse else 300.0, "max_drawdown": 500.0, "trades": 12, "params": params}

            execute.source = {"text": OLD, "stamp": 0, "root": "ES", "timeframe": "2m"}
            with mock.patch("futuresfund.strategy_team.engine_problem", lambda source: ""),                     mock.patch.object(diagnosis, "measure_response", lambda *a, **k: {"moved": {}}):
                kept = learn._install_checked(Board(), path, redesign.keep_title(NEW, OLD), execute, {"net_profit": 300.0, "max_drawdown": 500.0, "trades": 12, "params": {"length": 20}}, "Chart researcher", None, "Why.", redesign=True)
            self.assertIsNone(kept)
            self.assertEqual(path.read_text(encoding="utf-8"), OLD)
            self.assertEqual(execute.source["text"], OLD)

class OfferTests(unittest.TestCase):
    def test_a_first_read_is_not_offered_a_redesign(self):
        with _Folders() as folders:
            path = folders.script()
            seen = []

            def reasoned(script, rows, bounds, choices, why, response, can_redesign):
                seen.append(can_redesign)
                return None

            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                learn.ask_researcher(Board(), path, OLD, [{"params": {"length": 20}}], {}, "Read it.", None, "Chart researcher", "read")
                learn.drain_decisions()
            self.assertEqual(seen, [False])

    def test_no_redesign_is_offered_once_the_original_has_used_them_all(self):
        with _Folders() as folders:
            path = folders.script()
            for number in range(redesign.LIMIT):
                redesign.note_redesign(path, f"Try {number}.")
            seen = []

            def reasoned(script, rows, bounds, choices, why, response, can_redesign):
                seen.append(can_redesign)
                return None

            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                learn.ask_researcher(Board(), path, OLD, [{"params": {"length": 20}}], {}, "It failed.", None, "Chart researcher", "stage7")
                learn.drain_decisions()
            self.assertEqual(seen, [False])


class ReviewTests(unittest.TestCase):
    def _review(self, chart, days=7, trials=None):
        def execute(params):
            return {}

        root, timeframe = chart.split()
        execute.source = {"text": OLD, "stamp": 0, "root": root, "timeframe": timeframe}
        trials = trials or [{"params": {"length": 20}, "net_profit": -100.0, "max_drawdown": 80.0, "trades": 5}]
        learn._review_failed_stage(Board(), Path("s.pine"), trials, days, execute, {}, "Chart researcher")

    def test_each_chart_is_reviewed_once_up_to_the_cap_on_a_script(self):
        with _Folders():
            asked = []

            def reasoned(script, rows, bounds, choices, why, response, can_redesign):
                asked.append(why)
                return None

            charts = ["ES 2m", "ES 5m", "ES 15m", "NQ 2m", "NQ 5m", "NQ 15m", "GC 2m", "GC 5m"]
            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                self._review("ES 2m")
                learn.drain_decisions()
                self._review("ES 2m")
                learn.drain_decisions()
                self.assertEqual(len(asked), 1)
                self.assertIn("on the ES 2m chart", asked[0])
                for chart in charts[1:]:
                    self._review(chart)
                    learn.drain_decisions()
            self.assertEqual(len(asked), learn.STAGE_REVIEWS)

    def test_the_year_review_is_judged_as_the_year_not_as_a_stage(self):
        limits = {
            "min_profit": 6000.0, "profit_factor": 1.1, "win_rate": 40.0, "min_trades": 60, "drawdown_pct": 2.0,
            "use_drawdown": True, "use_trades": True, "use_profit": True, "use_factor": False, "use_win_rate": False,
        }
        row = {"params": {"length": 20}, "net_profit": 7000.0, "max_drawdown": 8000.0, "trades": 100, "days": 260, "biggest_loser": -150.0,
               "spans": {"7": {"net_profit": 500.0}, "30": {"net_profit": 400.0}, "90": {"net_profit": 3000.0}, "365": {"net_profit": 7000.0}}}
        with _Folders(), mock.patch("futuresfund.book.strategy_acceptance", lambda *args, **kwargs: dict(limits)):
            self.assertEqual(set(objective.acceptance_misses(row, days=365, limits=limits)), set(objective.acceptance_misses(row, limits=limits)))
            asked = []

            def reasoned(script, rows, bounds, choices, why, response, can_redesign):
                asked.append(why)
                return None

            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                self._review("ES 5m", days=365, trials=[row])
                learn.drain_decisions()
        self.assertEqual(len(asked), 1)
        why = asked[0]
        self.assertNotIn("the same limit as the year", why)
        self.assertNotIn("the same as the year", why)
        self.assertIn("The year needs more profit than drawdown. This run made $7,000 with a drawdown of $8,000.", why)
        self.assertIn("This run's profit did not grow that way.", why)
        self.assertIn("so it did not pass the year.", why)
        self.assertIn("This script failed the year search on the ES 5m chart", why)
        self.assertNotIn("365-day stage", why)

    def test_another_stage_on_the_same_chart_is_its_own_review(self):
        with _Folders():
            asked = []

            def reasoned(script, rows, bounds, choices, why, response, can_redesign):
                asked.append(why)
                return None

            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                self._review("ES 2m", 7)
                learn.drain_decisions()
                self._review("ES 2m", 30)
                learn.drain_decisions()
            self.assertEqual(len(asked), 2)

    def test_a_long_line_of_waiting_questions_does_not_grow(self):
        with _Folders():
            full = {f"script{number}|stage7" for number in range(learn.REVIEW_BACKLOG)}
            with mock.patch.object(learn, "_decisions_open", set(full)):
                self._review("ES 2m")
            self.assertTrue(learn._decisions.empty())
            self.assertNotIn("reviewed", diagnosis.load(OLD))

    def test_waiting_repairs_do_not_keep_a_review_out_and_it_is_answered_first(self):
        with _Folders():
            asked = []

            def reasoned(script, rows, bounds, choices, why, response, can_redesign):
                asked.append(why)
                return None

            trials = [{"params": {"length": 20}}]
            with mock.patch("futuresfund.attempt_reason.reasoned_change", reasoned):
                for number in range(learn.REVIEW_BACKLOG + 1):
                    learn.ask_researcher(Board(), Path("s.pine"), f"{OLD}\nx{number} = 1\n", trials, {}, "Repair it.", None, "Chart researcher", "repair", urgent=True)
                self._review("ES 2m")
                learn.drain_decisions()
            self.assertIn("failed the 7-day stage", asked[0])
            self.assertEqual(len(asked), learn.REVIEW_BACKLOG + 2)



class WholeChartTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)

    @staticmethod
    def _bars(sessions):
        # One bar each noon New York, weekdays only, starting on a Monday.
        start = 1_700_000_000 - 1_700_000_000 % 86400 + 17 * 3600
        found, day = [], 0
        while len(found) < sessions:
            stamp = start + day * 86400
            day += 1
            if objective.datetime.fromtimestamp(stamp, objective.ET).weekday() < 5:
                found.append({"t": stamp, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1})
        return found

    def test_every_run_and_every_stage_sees_the_chart_through_its_last_bar(self):
        bars = self._bars(300)
        ran = []

        def run_script(source, chart, timeframe, overrides, cancel, instrument, title, slot, full=False, sessions=None):
            ran.append((chart[-1], sessions))
            return {"net_profit": 1.0, "max_drawdown": 1.0, "trades": 3}

        with mock.patch("futuresfund.backtrader_engine.run_script", run_script):
            execute = learn._engine_execute("strategy()", {"5m": bars}, {"title": "S", "inputs": [], "named": {}}, None, "ES", "5 min chart developer", "5m", None)
            execute({})
            execute.source["sessions"] = 5
            execute({})
        self.assertEqual(ran, [(bars[-1], None), (bars[-1], 5)])

    def test_the_profit_has_to_grow_from_7_to_30_to_90_to_365_days(self):
        row = {"net_profit": 9000.0, "max_drawdown": 200.0, "trades": 200, "days": 260, "biggest_loser": -150.0, "win_rate": 50.0, "profit_factor": 1.5}
        growing = {"7": {"net_profit": 300.0}, "30": {"net_profit": 900.0}, "90": {"net_profit": 2500.0}, "365": {"net_profit": 9000.0}}
        self.assertTrue(objective.passes_acceptance({**row, "spans": growing}))
        self.assertTrue(objective.passes_acceptance(row))
        for days, profit in (("7", 0.0), ("30", 300.0), ("90", 900.0), ("365", 2500.0)):
            shrunk = {**growing, days: {"net_profit": profit}}
            self.assertEqual(set(objective.acceptance_misses({**row, "spans": shrunk})), {"growth"})
        self.assertIn("did not grow", objective.acceptance_reason({**row, "spans": {**growing, "7": {"net_profit": -50.0}}}))

    def test_the_year_needs_the_library_trade_count_and_no_ratio(self):
        # The default book asks for 100 trades a year. The old 3-a-week floor (156) is off unless it is set.
        row = {"net_profit": 9000.0, "max_drawdown": 200.0, "trades": 100, "days": 260, "biggest_loser": -150.0, "win_rate": 50.0, "profit_factor": 1.5, "profit_ratio": 0.8}
        self.assertTrue(objective.passes_acceptance(row))
        self.assertTrue(objective.passes_acceptance({**row, "trades": 150}))
        self.assertIn("trades", objective.acceptance_misses({**row, "trades": 99}))
        with mock.patch.object(objective, "STAGE_TRADES_PER_WEEK", 3):
            self.assertIn("trades", objective.acceptance_misses({**row, "trades": 150}))
            self.assertTrue(objective.passes_acceptance({**row, "trades": 156}))


class ReportTests(unittest.TestCase):
    def setUp(self):
        _default_book(self)

    def test_the_gap_report_names_the_miss(self):
        from futuresfund.acceptance_gaps import _line, _row

        limits = book.strategy_acceptance()
        self.assertEqual(_row("growth", [], limits, 750.0, 2, 10)["reason"], "Profit growing over 7, 30, 90, and 365 days")
        self.assertIn("A longer window made less", _line("growth", [], limits, 750.0, 2, 10))


if __name__ == "__main__":
    unittest.main()
