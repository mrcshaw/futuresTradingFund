"""Successful-run count and Skip for the script an engine is running."""

import unittest
from pathlib import Path


class EngineProgressTests(unittest.TestCase):
    def tearDown(self):
        from futuresfund.backtrader_engine import clear_engine_results, clear_skips, finish_study

        from futuresfund.backtrader_engine import clear_restarts

        clear_skips()
        clear_restarts()
        import futuresfund.learn as learn

        learn._engine4_learning = False
        clear_engine_results()
        for index in range(4):
            finish_study(index)

    def test_a_new_script_starts_the_success_count_over(self):
        from futuresfund.backtrader_engine import begin_study, engine_status, record_success

        begin_study(0, "one.pine", 10)
        self.assertEqual(record_success(0), 1)
        self.assertEqual(record_success(0), 2)
        row = engine_status()[0]
        self.assertEqual(row["successes"], 2)
        self.assertEqual(row["study"], "one.pine")
        self.assertTrue(row["studying"])
        self.assertEqual(row["goal"], 10)
        begin_study(0, "two.pine", 10)
        fresh = engine_status()[0]
        self.assertEqual(fresh["successes"], 0)
        self.assertEqual(fresh["study"], "two.pine")

    def test_the_scale_sits_on_the_engine_between_the_opposite_and_the_goal(self):
        from futuresfund.backtrader_engine import begin_study, engine_status, record_scale

        begin_study(1, "breakout.pine", 10)
        record_scale(1, 0.62, "closer", "Stop")
        row = engine_status()[1]
        self.assertEqual(row["scale"], 0.62)
        self.assertEqual(row["scale_move"], "closer")
        self.assertEqual(row["scale_phase"], "Stop")
        begin_study(1, "other.pine", 10)
        fresh = engine_status()[1]
        self.assertEqual(fresh["scale"], 0.0)
        self.assertEqual(fresh["scale_move"], "")

    def test_an_engine_keeps_the_last_20_results(self):
        from futuresfund.backtrader_engine import engine_status, record_engine_result

        for number in range(21):
            record_engine_result(0, {
                "script": f"Script {number}",
                "time": f"19:1{number % 10}:00",
                "net_profit": 1000.0 + number,
                "max_drawdown": 400.0,
                "profit_factor": round(number / 7, 3),
                "profit_ratio": 1.2,
                "trades": 20 + number,
                "win_rate": 40.0,
            })
        results = engine_status()[0]["results"]
        self.assertEqual(len(results), 20)
        self.assertEqual(results[0]["script"], "Script 1")
        self.assertEqual(results[-1]["script"], "Script 20")
        self.assertEqual(results[-1]["profit_factor"], round(20 / 7, 3))
        self.assertEqual(results[-1]["profit_ratio"], 1.2)
        self.assertEqual(results[-1]["trades"], 40)
        self.assertEqual(results[-1]["win_rate"], 40.0)
        record_engine_result(0, {"script": "Skipped", "net_profit": None, "trades": 0})
        self.assertEqual(len(engine_status()[0]["results"]), 20)

    def test_the_same_script_keeps_its_count(self):
        from futuresfund.backtrader_engine import begin_study, engine_status, record_success

        begin_study(2, "held.pine", 10)
        record_success(2)
        begin_study(2, "held.pine", 10)
        self.assertEqual(engine_status()[2]["successes"], 1)

    def test_skip_stops_the_search_after_the_attempt_in_flight(self):
        from futuresfund.backtrader_engine import engine_status, request_skip
        from futuresfund.learn import _search

        calls = []

        def execute(params):
            calls.append(dict(params))
            request_skip(1)
            return {"net_profit": 100, "max_drawdown": 50, "trades": 20, "days": 20}

        trials = _search({"len": 14}, execute, 50000, 20, None, None, None, slot=0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(getattr(trials, "skipped", False))
        self.assertEqual(engine_status()[0]["successes"], 1)

    def test_skip_stays_clickable_and_posts_without_waiting(self):
        js = (Path(__file__).resolve().parents[1] / "futuresfund" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("skip.disabled = false", js)
        self.assertNotIn("skip.disabled = !running", js)
        self.assertIn("keepalive: true", js)
        self.assertIn("pressEngineButton", js)

    def test_a_busy_skip_cuts_the_engine_and_returns_at_once(self):
        import threading
        import time
        from unittest import mock

        from futuresfund import backtrader_engine as engine

        engine._acquire_engine("live.pine", "5m", "ES", 1)
        seen = threading.Event()

        class Desk:
            online = True
            slot = None

            def restart(self, slot):
                self.slot = slot
                seen.set()

        desk = Desk()
        try:
            with mock.patch("futuresfund.engine_workers.active_desk", return_value=desk):
                started = time.perf_counter()
                result = engine.request_skip(2)
                elapsed = time.perf_counter() - started
                self.assertTrue(result["ok"])
                self.assertEqual(result["script"], "live.pine")
                self.assertLess(elapsed, 0.5)
                self.assertTrue(engine.skipped(1))
                self.assertTrue(seen.wait(1))
                self.assertEqual(desk.slot, 1)
        finally:
            engine.clear_skips()
            engine._release_engine(1)

    def test_skip_names_the_running_script_and_rejects_an_unknown_engine(self):
        from futuresfund.backtrader_engine import begin_study, request_skip

        begin_study(3, "created_fresh.pine", 10)
        result = request_skip(4)
        self.assertEqual(result["engine"], 4)
        self.assertEqual(result["script"], "created_fresh.pine")
        self.assertTrue(result["ok"])
        with self.assertRaises(ValueError):
            request_skip(0)
        with self.assertRaises(ValueError):
            request_skip(5)

    def test_restart_stops_a_hung_engine_and_sends_engine_4_to_the_learning_folder(self):
        from futuresfund.backtrader_engine import begin_study, request_restart, restarted
        from futuresfund.learn import engine4_should_study_learning, restart_research_engine

        begin_study(3, "hung.pine", 10)
        result = request_restart(4)
        self.assertTrue(result["ok"])
        self.assertEqual(result["engine"], 4)
        self.assertTrue(restarted(3))
        with self.assertRaises(ValueError):
            request_restart(0)
        board = type("Board", (), {"posts": [], "post": lambda self, *args, **kwargs: self.posts.append(args)})()
        restart_research_engine(4, board)
        self.assertTrue(engine4_should_study_learning())
        self.assertTrue(any("learning folder" in str(item) for item in board.posts))

    def test_the_engine_page_shows_the_count_the_skip_and_the_results(self):
        root = Path(__file__).resolve().parents[1] / "futuresfund" / "static"
        js = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "styles.css").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Successful attempts", js)
        self.assertNotIn("Successful runs", js)
        self.assertIn("successful", js)
        self.assertIn("/api/engines/", js)
        self.assertIn("hood-skip", js)
        self.assertIn("hood-restart", js)
        self.assertIn("/restart", js)
        self.assertIn("hood-tools", js)
        self.assertNotIn("hood-log", js)
        self.assertLess(js.index("hood-tools"), js.index("hood-results"))
        self.assertIn(".hood-tools", css)
        self.assertIn("justify-content: flex-end", css)
        self.assertIn("hood-scale", js)
        self.assertLess(js.index("hood-scale"), js.index("hood-results"))
        self.assertIn("Opposite of the goal", js)
        self.assertIn(".hood-scale-track", css)
        self.assertIn("strategy_acceptance", js)
        self.assertIn("Win rate ${rate}%", js)
        self.assertIn("function goalLabel", js)
        self.assertIn("hood-scale-goal", js)
        self.assertIn("goalLabel((state.book && state.book.strategy_acceptance)", js)
        self.assertIn("hood-results", js)
        self.assertIn("renderEngineResults", js)
        self.assertIn(".hood-result-table", css)
        self.assertIn("box-shadow", css)
        self.assertIn("app.js?v=133", html)
        self.assertIn("styles.css?v=61", html)
        self.assertIn('id="kronos-count"', html)
        self.assertIn('id="kronos-bar"', html)
        self.assertIn("hold-new", html)
        self.assertIn("Hold new strategies", html)
        self.assertIn("Make new strategies", js)
        self.assertNotIn("ninja-canvas", html)
        self.assertIn("library-add", html)
        self.assertIn("script-delete", html)
        self.assertIn("script-save", html)
        self.assertIn("openLibraryAdd", js)
        self.assertIn("deleteLibrary", js)
        self.assertIn("the acceptance number chosen on the strategy library page", html)
        self.assertIn('name="goal"', html)
        self.assertIn("Not tested on the full dataset", html)
        self.assertIn("untested-table", html)
        self.assertIn("untested-add", html)
        self.assertIn("paintKronosProgress", js)
        self.assertIn("kronos-track", css)
        self.assertIn('data-sort="profit_ratio"', html)
        self.assertIn("renderUntested", js)
        self.assertIn("libraryRow", js)


class KronosBarTests(unittest.TestCase):
    def test_a_step_line_sets_the_counter(self):
        from futuresfund.kronos_feed import snapshot

        row = snapshot(
            "2026-10-13 01:23:10 - tokenizer_training_rank_0 - INFO - [Epoch 1/30, Step 50/266] LR: 0.000039, Loss: -0.0295",
            "tokenizer",
        )
        self.assertEqual(row["step"], 50)
        self.assertEqual(row["steps"], 266)
        self.assertEqual(row["epoch"], 1)
        self.assertEqual(row["epochs"], 30)
        self.assertEqual(row["phase"], "tokenizer")
        self.assertAlmostEqual(row["percent"], 100 * 50 / 266, places=3)

    def test_a_finished_tokenizer_leaves_the_predictor_epochs_on_the_bar(self):
        from futuresfund.kronos_feed import overall

        tokenizer = "\n".join([
            "Training epochs: 30",
            "[Epoch 30/30, Step 236/266] LR: 0.000000, Loss: -0.0314",
            "--- Epoch 30/30 Summary ---",
            "Tokenizer training completed! Best validation loss: 0.0031",
        ])
        predictor = "\n".join([
            "Training epochs: 20",
            "Starting training...",
        ])
        row = overall(tokenizer, predictor, 30, 20)
        self.assertEqual(row["whole_done"], 30 * 266)
        self.assertEqual(row["whole_total"], 50 * 266)
        self.assertEqual(row["whole_left"], 20 * 266)
        self.assertEqual(row["phase"], "predictor")
        self.assertEqual(row["step"], 0)
        self.assertEqual(row["epochs"], 20)
        self.assertTrue(row["tokenizer_finished"])
        self.assertTrue(row["running"])
        self.assertFalse(row["finished"])
        self.assertAlmostEqual(row["percent"], 100 * 30 / 50, places=3)
