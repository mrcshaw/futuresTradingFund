"""Three engines per instrument on a bounded pool of worker processes."""

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = """//@version=6
strategy("Pool test")
length = input.int(10, "Length")
"""


def _bars(days: int, per_day: int = 40, start: int = 1_700_000_000, step: int = 900) -> list[dict]:
    """Bars in time order, per_day bars inside each futures session, with epoch-second times."""
    rows = []
    price = 100.0
    for day in range(days):
        # 6pm New York starts a session; 23:00 UTC is safely inside one in either daylight setting.
        base = start + day * 86_400
        for index in range(per_day):
            price += 0.5 if (day + index) % 3 else -0.75
            rows.append({"t": base + index * step, "o": price - 0.25, "h": price + 1, "l": price - 1, "c": price, "v": 10 + index})
    return rows


class RootedQueueTests(unittest.TestCase):
    """An engine only takes its own instrument's chart, and two instruments can share a file."""

    def setUp(self):
        from futuresfund import learn

        self.learn = learn
        self.home = tempfile.TemporaryDirectory()
        root = Path(self.home.name)
        self.learning = root / "learningStrategies"
        self.created = root / "createdStrategies"
        self.notes = root / "researchNotes"
        for folder in (self.learning, self.created, self.notes):
            folder.mkdir()
        (self.learning / "shared.pine").write_text(SCRIPT, encoding="utf-8")
        self.patches = [
            mock.patch.object(learn, "LEARNING", self.learning),
            mock.patch.object(learn, "CREATED", self.created),
            mock.patch.object(learn, "NOTES", self.notes),
            mock.patch.object(learn, "research_instruments", lambda: ["ES", "NQ"]),
        ]
        for patch in self.patches:
            patch.start()
        learn.clear_holds()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.learn.clear_holds()
        self.home.cleanup()

    def _finish(self, root: str, timeframe: str) -> None:
        run = {"attempts": 3, "trials": [{"trades": 8, "net_profit": 400}], "best": {"trades": 8, "net_profit": 400}}
        (self.notes / "shared.json").write_text(json.dumps({"instruments": {root: {"charts": {timeframe: run}}}}), encoding="utf-8")

    def test_an_engine_only_takes_a_script_its_own_root_still_needs(self):
        learn = self.learn
        self.assertEqual([path.name for path in learn.jobs_for_chart("2m", root="NQ")], ["shared.pine"])
        self._finish("NQ", "2m")
        self.assertEqual(learn.jobs_for_chart("2m", root="NQ"), [])
        self.assertEqual([path.name for path in learn.jobs_for_chart("2m", root="ES")], ["shared.pine"])
        # Without a root the chart still has work (ES), as before.
        self.assertEqual([path.name for path in learn.jobs_for_chart("2m")], ["shared.pine"])
        claims = learn.JobClaims()
        self.assertIsNone(claims.take("NQ", "2m"))
        self.assertEqual(claims.take("ES", "2m").name, "shared.pine")
        # NQ's other charts still need it.
        self.assertEqual(claims.take("NQ", "5m").name, "shared.pine")

    def test_two_engines_on_one_timeframe_study_the_same_file_at_once(self):
        claims = self.learn.JobClaims()
        es = claims.take("ES", "5m")
        nq = claims.take("NQ", "5m")
        self.assertEqual(es.name, "shared.pine")
        self.assertEqual(nq.name, "shared.pine")
        # The same instrument's chart is one engine's at a time (engine 4's spare study claims through it too).
        self.assertIsNone(claims.take("ES", "5m"))
        claims.release(es, "ES", "5m")
        self.assertEqual(claims.take("ES", "5m").name, "shared.pine")

    def test_the_folder_scan_runs_outside_the_claims_lock(self):
        learn = self.learn
        claims = learn.JobClaims()
        scans = []
        real = learn.jobs_for_chart

        def scan(*args, **kwargs):
            scans.append(claims.lock.locked())
            return real(*args, **kwargs)

        with mock.patch.object(learn, "jobs_for_chart", scan):
            first = claims.take("ES", "5m")
            second = claims.take("ES", "5m")
        self.assertEqual(scans, [False, False])
        self.assertEqual(first.name, "shared.pine")
        # The claim is still checked under the lock, so the same chart never gets the file twice.
        self.assertIsNone(second)

    def test_holds_are_per_instrument_and_a_compile_hold_covers_the_whole_file(self):
        learn = self.learn
        path = self.learning / "shared.pine"
        learn.park_engine_script(path, "2m", "NQ")
        self.assertEqual(learn.jobs_for_chart("2m", root="NQ", held=learn._engine_parked), [])
        self.assertEqual([item.name for item in learn.jobs_for_chart("2m", root="ES", held=learn._engine_parked)], ["shared.pine"])
        learn.clear_holds()
        self.assertTrue(learn.hold_script(path))
        self.assertFalse(learn.hold_script(path))
        claims = learn.JobClaims()
        self.assertIsNone(claims.take("ES", "15m"))
        self.assertIsNone(claims.take("NQ", "2m"))
        # A compile failure counted once for the file lets any chart's engine give it its retry.
        self.assertTrue(learn.release_retries("15m", {str(path): 1}, "NQ"))
        self.assertEqual(claims.take("NQ", "2m").name, "shared.pine")

    def test_a_study_returns_at_once_when_its_root_is_no_longer_pending(self):
        learn = self.learn
        path = self.learning / "shared.pine"
        loaded = []
        with mock.patch.object(learn, "_read_notes", lambda path: {}), \
                mock.patch.object(learn, "pending_for_chart", lambda notes, timeframe, instruments=None: ["ES"]), \
                mock.patch.object(learn, "_copy_finished", lambda path, timeframe, root: False), \
                mock.patch.object(learn, "_load_frames", lambda root="ES", **kwargs: loaded.append((root, kwargs)) or {}):
            self.assertIsNone(learn._study_chart(None, path, {}, None, timeframe="2m", root="NQ"))
        self.assertEqual(loaded, [])

    def test_a_study_loads_only_its_own_chart(self):
        learn = self.learn
        path = self.learning / "shared.pine"
        loaded = []

        class Board:
            def post(self, *args, **kwargs):
                return None

        with mock.patch.object(learn, "_read_notes", lambda path: {}), \
                mock.patch.object(learn, "pending_for_chart", lambda notes, timeframe, instruments=None: ["ES", "NQ"]), \
                mock.patch.object(learn, "_copy_finished", lambda path, timeframe, root: False), \
                mock.patch.object(learn, "describe_later", lambda *args: None), \
                mock.patch.object(learn, "file_indicators", lambda *args: []), \
                mock.patch.object(learn, "_load_frames", lambda root="ES", **kwargs: loaded.append((root, kwargs)) or {}):
            # No bars yet on that chart: the study is set aside, after loading only NQ 15m.
            self.assertEqual(learn._study_chart(Board(), path, {}, None, timeframe="15m", root="NQ"), "skipped")
        self.assertEqual(loaded, [("NQ", {"timeframes": ("15m",)})])

    def test_stop_testing_stops_only_the_studied_instrument(self):
        from futuresfund.proving import mark, rows, stop_testing

        mark("shared.pine", "ES", "2m", 7, "testing")
        mark("shared.pine", "NQ", "2m", 7, "testing")
        stopped = stop_testing("The ES study ended.", file="shared.pine", timeframe="2m", root="ES")
        self.assertEqual(stopped, ["shared.pine|ES|2m"])
        labels = {row["root"]: row["label"] for row in rows()}
        self.assertEqual(labels, {"ES": "Stopped at 7 days", "NQ": "Testing 7 days"})

    def test_a_study_marks_only_its_own_instrument_stopped(self):
        from futuresfund.proving import mark, rows

        learn = self.learn
        path = self.learning / "shared.pine"

        def studies(board, path, *args, **kwargs):
            mark(path.name, "NQ", "2m", 30, "testing")
            return "engine"

        mark("shared.pine", "ES", "2m", 7, "testing")
        with mock.patch.object(learn, "_study_chart", side_effect=studies):
            learn._study(None, path, {}, None, timeframe="2m", engine_slot=7, root="NQ")
        labels = {row["root"]: row["label"] for row in rows()}
        # An engine fault ends the stage without a result, so the NQ stage resumes there.
        self.assertEqual(labels["NQ"], "Stopped at 30 days, resumes there")
        self.assertEqual(labels["ES"], "Testing 7 days")


class SharedPassTests(unittest.TestCase):
    def test_filing_runs_at_most_once_a_minute_across_every_engine(self):
        from futuresfund import learn

        calls = []
        with mock.patch.object(learn, "file_completed_scripts", lambda board: calls.append(board)), \
                mock.patch.object(learn, "_file_completed_at", 0.0):
            threads = [threading.Thread(target=learn.file_completed_soon, args=(index,)) for index in range(21)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(calls), 1)
            self.assertFalse(learn.file_completed_soon("again"))
            with mock.patch.object(learn, "_file_completed_at", time.monotonic() - learn.FILE_COMPLETED_EVERY - 1):
                self.assertTrue(learn.file_completed_soon("later"))
        self.assertEqual(len(calls), 2)

    def test_a_repair_is_the_files_and_any_engine_that_started_it_picks_it_up(self):
        from futuresfund import learn

        learn._repairs.clear()
        done = threading.Thread(target=lambda: None)
        done.start()
        done.join()
        first = Path("a.pine")
        second = Path("b.pine")
        try:
            learn._repairs[str(first)] = {"path": first, "timeframe": "2m", "root": "NQ", "developer": "2min chart developer", "slot": 7, "thread": done, "ready": True, "done": True}
            learn._repairs[str(second)] = {"path": second, "timeframe": "2m", "root": "ES", "developer": "2min chart developer", "slot": 0, "thread": done, "ready": False, "done": True}
            # Two of one developer's engines each have their own repair at the same time.
            self.assertEqual(len(learn._repairs), 2)
            status, job = learn.collect_validated("2min chart developer", 7)
            self.assertEqual((status, job["path"]), ("ready", first))
            self.assertEqual(learn.collect_validated("2min chart developer", 7), ("none", None))
            self.assertEqual(learn.collect_validated("2min chart developer", 0), ("rejected", None))
        finally:
            learn._repairs.clear()


class ResearchWindowTests(unittest.TestCase):
    """A chart engine's run on the last year plus WARMUP_BARS is the run on the whole series."""

    def _run(self, rows, sessions):
        from futuresfund import backtrader_engine as engine

        seen = []

        def fake_pinets(source, bars, timeframe, root, candles_key=None):
            seen.append([(bar.epoch, bar.o, bar.h, bar.l, bar.c, bar.v) for bar in bars])
            trades = []
            for index in range(0, len(bars) - 30, 37):
                entry, leave = bars[index], bars[index + 12]
                pnl = round((leave.c - entry.c) * 50 - 5.5, 2)
                when = entry.when.strftime("%Y-%m-%d %H:%M")
                trades.append({
                    "entry_time": when, "exit_time": leave.when.strftime("%Y-%m-%d %H:%M"),
                    "side": "long", "entry_price": entry.c, "exit_price": leave.c, "pnl": pnl, "qty": 1,
                })
            return trades

        with mock.patch("futuresfund.pinets_engine.run_pinets", fake_pinets):
            measured = engine.execute_script(SCRIPT, rows, "15m", {}, "ES", "pool", 0, sessions=sessions)
        measured.pop("logs", None)
        return measured, seen[0]

    def test_the_window_gives_the_same_result_as_the_whole_series(self):
        from futuresfund import backtrader_engine as engine
        from futuresfund.objective import RESEARCH_SESSIONS

        rows = _bars(days=int(RESEARCH_SESSIONS * 7 / 5) + 160, per_day=30)
        window = engine.research_window(rows)
        self.assertLess(len(window), len(rows))
        self.assertGreater(len(window), engine.WARMUP_BARS)
        self.assertIs(window[-1], rows[-1])
        for sessions in (None, 5, 21, 64):
            with self.subTest(sessions=sessions):
                whole, whole_bars = self._run(rows, sessions)
                sliced, sliced_bars = self._run(engine.research_window(rows, sessions), sessions)
                self.assertEqual(sliced_bars, whole_bars)
                self.assertEqual(sliced, whole)
                self.assertGreater(whole["trades"], 0)

    def test_a_short_or_unsorted_chart_is_sent_whole(self):
        from futuresfund import backtrader_engine as engine

        short = _bars(days=40)
        self.assertIs(engine.research_window(short), short)
        rows = _bars(days=500, per_day=30)
        rows[10], rows[11] = rows[11], rows[10]
        self.assertEqual(engine.window_start_index(rows), 0)
        texts = [dict(row, t=str(row["t"])) for row in _bars(days=500, per_day=30)]
        self.assertEqual(engine.window_start_index(texts), 0)
        # A stage longer than a year reads before the window, so it gets the whole series.
        whole = _bars(days=500, per_day=30)
        self.assertIs(engine.research_window(whole, 400), whole)

    def test_a_worker_keeps_two_charts(self):
        from futuresfund import backtrader_engine as engine

        first, second, third = _bars(5), _bars(6), _bars(7)
        with mock.patch.object(engine, "_charts", []):
            a = engine._chart_of(first)
            b = engine._chart_of(second)
            self.assertIs(engine._chart_of(first), a)
            engine._chart_of(third)
            self.assertIs(engine._chart_of(first), a)
            self.assertIsNot(engine._chart_of(second), b)


class PoolTests(unittest.TestCase):
    """Real worker processes. Each spawn takes a second or two."""

    def setUp(self):
        from futuresfund import engine_workers
        from futuresfund.backtrader_engine import clear_skips

        self.workers = engine_workers
        clear_skips()
        self.desk = engine_workers.EngineDesk()
        self.desk.start()
        self.size = mock.patch.object(engine_workers, "ENGINES_AT_ONCE", 2)
        self.size.start()

    def tearDown(self):
        from futuresfund.backtrader_engine import _ENGINE_SLOTS, clear_skips, set_engine_notice

        self.size.stop()
        self.desk.stop()
        clear_skips()
        for index in range(_ENGINE_SLOTS):
            set_engine_notice(index, "")

    def test_the_pool_never_runs_more_than_engines_at_once_backtests(self):
        from futuresfund.backtrader_engine import engine_status

        self.assertEqual(self.desk.pids(), [])
        slots = (0, 4, 7, 12, 20)
        results = {}
        notices = set()

        def hold(slot):
            results[slot] = self.desk.hold(slot, 0.8)

        threads = [threading.Thread(target=hold, args=(slot,)) for slot in slots]
        for thread in threads:
            thread.start()
        deadline = time.time() + 60
        while time.time() < deadline and any(thread.is_alive() for thread in threads):
            self.assertLessEqual(self.desk.running(), 2)
            for row in engine_status():
                if row.get("notice"):
                    notices.add(row["notice"])
            time.sleep(0.05)
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), list(slots))
        self.assertTrue(all(result.get("held") for result in results.values()))
        self.assertEqual(self.desk.peak, 2)
        self.assertLessEqual(len(self.desk.pids()), 2)
        self.assertIn("Waiting for a free worker (2 running at once)", notices)
        # Waiting ends with the notice cleared.
        self.assertFalse(any(engine_status()[slot].get("notice") for slot in slots))

    def test_skip_drops_a_waiting_job_and_restart_cuts_the_running_one(self):
        from futuresfund.backtrader_engine import clear_skip, request_skip

        outcome = {}

        def hold(slot, seconds):
            try:
                outcome[slot] = self.desk.hold(slot, seconds)
            except RuntimeError as exc:
                outcome[slot] = exc

        busy = [threading.Thread(target=hold, args=(slot, 2.5)) for slot in (0, 1)]
        for thread in busy:
            thread.start()
        deadline = time.time() + 30
        while time.time() < deadline and self.desk.running() < 2:
            time.sleep(0.05)
        self.assertEqual(self.desk.running(), 2)
        # Engine 10 waits for a worker. Skip drops its queued job without touching the two running ones.
        waiting = threading.Thread(target=hold, args=(9, 0.1))
        waiting.start()
        time.sleep(0.3)
        request_skip(10)
        waiting.join(5)
        self.assertFalse(waiting.is_alive())
        self.assertTrue(outcome[9].get("skipped"))
        clear_skip(9)
        self.assertEqual(self.desk.running(), 2)
        # Restart of engine 1 ends the worker that runs engine 1's job, and only that one.
        running = next(item for item in self.desk.workers() if item["engine"] == 1)
        other = next(item for item in self.desk.workers() if item["engine"] == 2)
        started = time.perf_counter()
        self.desk.restart(0)
        busy[0].join(10)
        self.assertLess(time.perf_counter() - started, 2.4)
        self.assertIsInstance(outcome[0], RuntimeError)
        self.assertNotIn(running["pid"], self.desk.pids())
        busy[1].join(10)
        self.assertTrue(outcome[1].get("held"))
        self.assertIn(other["pid"], self.desk.pids())

    def test_restart_of_an_engine_waiting_for_a_worker_drops_its_queued_job(self):
        from futuresfund.backtrader_engine import clear_restart, clear_skip, request_restart

        outcome = {}

        def hold(slot, seconds):
            outcome[slot] = self.desk.hold(slot, seconds)

        busy = [threading.Thread(target=hold, args=(slot, 2.0)) for slot in (0, 1)]
        for thread in busy:
            thread.start()
        deadline = time.time() + 30
        while time.time() < deadline and self.desk.running() < 2:
            time.sleep(0.05)
        waiting = threading.Thread(target=hold, args=(5, 0.1))
        waiting.start()
        time.sleep(0.4)
        try:
            request_restart(6)
            waiting.join(5)
            self.assertFalse(waiting.is_alive())
            self.assertTrue(outcome[5].get("skipped"))
            self.assertFalse(outcome[5].get("held"))
        finally:
            clear_skip(5)
            clear_restart(5)
            for thread in busy:
                thread.join(10)

    def test_a_dead_worker_is_started_again_for_the_next_job(self):
        import os
        import signal

        first = self.desk.hold(0, 0.1)
        self.assertTrue(first.get("held"))
        pids = self.desk.pids()
        self.assertEqual(len(pids), 1)
        os.kill(pids[0], getattr(signal, "SIGTERM", 15))
        deadline = time.time() + 10
        while time.time() < deadline and self.desk.pids():
            time.sleep(0.05)
        second = self.desk.hold(3, 0.1)
        self.assertTrue(second.get("held"))
        after = self.desk.pids()
        self.assertEqual(len(after), 1)
        self.assertNotEqual(after[0], pids[0])

    def test_a_smaller_pool_ends_the_extra_workers_after_their_job(self):
        results = {}

        def hold(slot):
            results[slot] = self.desk.hold(slot, 0.6)

        threads = [threading.Thread(target=hold, args=(slot,)) for slot in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(self.desk.workers()), 2)
        with mock.patch.object(self.workers, "ENGINES_AT_ONCE", 1):
            self.assertTrue(self.desk.hold(2, 0.1).get("held"))
            self.assertEqual(len(self.desk.workers()), 1)

    def test_the_stop_flag_takes_no_lock_a_terminated_worker_could_keep(self):
        # An Event's check takes a cross-process lock. A worker terminated while holding it (a Restart,
        # a Skip, a pool shrink) left stop() and every other worker's loop waiting on it forever.
        from futuresfund.trading_engine import TradingEngines

        self.assertFalse(hasattr(self.desk._stop, "get_lock"))
        self.assertEqual(self.desk._stop.value, 0)
        engines = TradingEngines()
        engines.start()
        try:
            self.assertFalse(hasattr(engines._stop, "get_lock"))
            self.assertEqual(engines._stop.value, 0)
        finally:
            engines.stop()
        self.assertEqual(engines._stop.value, 1)

    def test_a_job_goes_to_the_free_worker_that_holds_its_chart(self):
        worker_a = self.workers._Worker(1)
        worker_b = self.workers._Worker(2)
        worker_a.keep("chart-a")
        worker_b.keep("chart-b")
        self.desk._workers = [worker_a, worker_b]
        self.assertIs(self.desk._pick("chart-b", 2), worker_b)
        self.assertIs(self.desk._pick("chart-a", 2), worker_a)
        worker_b.slot = 5
        # The worker with the chart is busy: a free one is used instead of waiting.
        self.assertIs(self.desk._pick("chart-b", 2), worker_a)
        worker_a.slot = 6
        self.assertIsNone(self.desk._pick("chart-b", 2))
        worker_a.keep("chart-c")
        self.assertEqual(list(worker_a.charts), ["chart-a", "chart-c"])
        worker_a.keep("chart-d")
        self.assertEqual(list(worker_a.charts), ["chart-c", "chart-d"])
        self.desk._workers = []


STATIC = Path(__file__).resolve().parents[1] / "futuresfund" / "static"


class PageTests(unittest.TestCase):
    """Under the hood and the Workflow tab read the engine's own instrument from its row."""

    @staticmethod
    def _function(source, name):
        start = source.index(f"function {name}(")
        end = source.index("\n}\n", start) + 3
        return source[start:end]

    def test_the_pages_have_no_six_engine_layout_left(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        flow = (STATIC / "workflow.js").read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("ENGINE_CHART", flow)
        self.assertNotIn("engines.length || 6", flow)
        self.assertNotIn("engineById(4)", flow)
        self.assertNotIn("Number(engine.id) !== 4", flow)
        self.assertIn("engines.length || ENGINE_COUNT", flow)
        self.assertNotIn("Six engines", html)
        self.assertNotIn("Engines 2 and 5", html)
        self.assertIn("workflow.js?v=11", html)
        self.assertIn("workflow_catalog.json?v=9", flow)
        self.assertIn("report.charts", app)

    @unittest.skipUnless(shutil.which("node"), "the page text runs in Node")
    def test_cards_are_named_by_engine_and_instrument_and_count_their_own_chart(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        flow = (STATIC / "workflow.js").read_text(encoding="utf-8")
        consts = (
            'const ENGINE_ROOTS = ["ES", "GC", "NQ", "MNQ", "MES", "M2K", "YM"];\n'
            'const ENGINE_DEVELOPERS = { "2m": "2min chart developer", "5m": "5 min chart developer", "15m": "15 min chart developer" };\n'
        )
        report = {
            "ready": True,
            "rows": [{"timeframe": "5m", "learning_done": 9, "learning_total": 10, "created_done": 0, "created_total": 2}],
            "charts": [{"root": "NQ", "timeframe": "5m", "learning_done": 3, "learning_total": 10, "created_done": 1, "created_total": 2}],
        }
        names = ("isCreationEngine", "engineRoot", "progressRow", "fallbackEngines", "engineHeading")
        script = (
            consts
            + "".join(self._function(app, name) for name in names)
            + f"const report = {json.dumps(report)};\n"
            + "const rows = fallbackEngines();\n"
            + "const nq = {id: 9, root: 'NQ', instrument: 'NQ', timeframe: '5m', creation: false};\n"
            + "const ym = {id: 21, root: 'YM', instrument: 'YM', timeframe: '5m', creation: false};\n"
            + "const make = {id: 4, root: '', instrument: 'GC', timeframe: '15m', creation: true, busy: true};\n"
            + "console.log(JSON.stringify({ids: rows.map((row) => row.id), creation: rows.filter(isCreationEngine).map((row) => row.id), "
            + "named: rows.map((row) => engineHeading(row)), nq: progressRow(report, nq).learning_done, ym: progressRow(report, ym).learning_done, "
            + "make: engineHeading(make), old: isCreationEngine({id: 4})}));\n"
        )
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        shown = json.loads(done.stdout)
        self.assertEqual(shown["ids"], list(range(1, 23)))
        self.assertEqual(shown["creation"], [4])
        self.assertEqual(shown["named"][:5], ["Engine 1 · ES 2m", "Engine 2 · ES 5m", "Engine 3 · ES 15m", "Engine 4 · creation", "Engine 5 · GC 2m"])
        self.assertEqual(shown["named"][-1], "Engine 22 · YM 15m")
        # NQ 5m counts its own instrument; YM has no row yet and falls back to the chart's.
        self.assertEqual((shown["nq"], shown["ym"]), (3, 9))
        self.assertEqual(shown["make"], "Engine 4 · creation · GC 15m")
        self.assertTrue(shown["old"])
        helpers = "".join(self._function(flow, name) for name in ("engineRows", "engineById", "isCreation", "engineChart"))
        flow_script = (
            'const ENGINE_ROOTS = ["ES", "GC", "NQ", "MNQ", "MES", "M2K", "YM"];\n'
            "const ui = {live: {lab: null}};\n"
            + helpers
            + "const before = [1, 3, 4, 5, 13, 22].map(engineChart);\n"
            + "ui.live.lab = {engines: [{id: 8, root: 'NQ', timeframe: '2m', creation: false}, {id: 4, creation: true}]};\n"
            + "console.log(JSON.stringify({before, live: [engineChart(8), engineChart(4)]}));\n"
        )
        done = subprocess.run(["node", "-e", flow_script], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        shown = json.loads(done.stdout)
        self.assertEqual(shown["before"], ["ES 2m", "ES 15m", "creation", "GC 2m", "MNQ 15m", "YM 15m"])
        self.assertEqual(shown["live"], ["NQ 2m", "creation"])

    def test_the_catalog_describes_22_engines(self):
        from futuresfund.roster import ENGINE_DESKS
        from futuresfund.workflow_design import KNOBS

        catalog = json.loads((STATIC / "workflow_catalog.json").read_text(encoding="utf-8"))
        nodes = {node["id"]: node for node in catalog["nodes"]}
        every = [f"E{desk['engine']} {desk['root']} {desk['timeframe']}" for desk in ENGINE_DESKS]
        self.assertEqual(len(nodes["claim"]["engines"]), 22)
        self.assertEqual(nodes["claim"]["engines"][3], "E4 creation")
        self.assertEqual([item for item in nodes["claim"]["engines"] if item != "E4 creation"], every)
        self.assertEqual(nodes["created"]["engines"], every)
        self.assertEqual(nodes["engine4"]["engines"], ["E4 creation"])
        self.assertIn("engines_at_once", nodes["claim"]["knobs"])
        self.assertIn("E1-E3 ES", nodes["claim"]["summary"])
        for node in catalog["nodes"]:
            self.assertFalse({"E5 5m", "E6 15m"} & set(node.get("engines") or []), node["id"])
        self.assertIn("engines_at_once", {knob["key"] for knob in KNOBS})
        flow = (STATIC / "workflow.js").read_text(encoding="utf-8")
        self.assertIn('["engines_at_once", "Backtests at once", "claim", "int", 1, 64, 1, "workers", 7,', flow)


if __name__ == "__main__":
    unittest.main()
