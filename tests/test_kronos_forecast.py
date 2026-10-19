"""Kronos reads closed bars and draws a forecast. It does not send an order."""

from __future__ import annotations

import inspect
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from zoneinfo import ZoneInfo

from futuresfund import kronos_model, kronos_replay
from futuresfund.chart_view import chart_payload
from futuresfund.kronos_replay import _cache_key, body_mark, choose_path, next_open_times, replay, shape_of
from futuresfund.live_trading import halt_window

ET = ZoneInfo("America/New_York")


def _bars(count: int = 20) -> list[dict]:
    bars = []
    price = 100.0
    for index in range(count):
        open_ = price
        close = price + (1 if index % 2 == 0 else -1)
        bars.append({
            "t": 1_700_000_000 + index * 300,
            "o": open_,
            "h": max(open_, close) + 0.25,
            "l": min(open_, close) - 0.25,
            "c": close,
            "v": 10,
        })
        price = close
    return bars


def _matching_predictor(bars):
    def predictor(history, pred_len=6, future_times=None):
        printed = bars[len(history):len(history) + pred_len]
        far = []
        for bar in printed:
            far.append({key: (bar[key] + 40 if key in {"o", "h", "l", "c"} else bar[key]) for key in bar})
        return [far, [dict(bar) for bar in printed]]

    return predictor


class ReplayTests(unittest.TestCase):
    def test_a_replay_records_a_pattern_count_and_does_not_invent_prices(self):
        bars = _bars()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "kronos_patterns.json"
            recorded = replay(bars, "ES", "5m", _matching_predictor(bars), path=path)
            self.assertGreaterEqual(recorded, 1)
            saved = json.loads(path.read_text(encoding="utf-8"))
            windows = saved["windows"]["ES"]["5m"]
            counts = [int(row["count"]) for bucket in windows.values() for row in bucket.values()]
            self.assertEqual(sum(counts), recorded)
            self.assertTrue(all(isinstance(count, int) and count >= 1 for count in counts))
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("net_profit", text)
            self.assertNotIn("win_rate", text)
            self.assertNotIn("100.25", text)
            self.assertEqual(replay(bars, "ES", "5m", _matching_predictor(bars), path=path), 0)

    def test_the_playback_predicts_after_each_closed_candle(self):
        bars = _bars(12)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "kronos_patterns.json"
            replay(bars, "ES", "5m", _matching_predictor(bars), steps=2, path=path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["cursor"]["ES|5m|five"], bars[3]["t"])
            self.assertGreaterEqual(len(saved["windows"]["ES"]["5m"]), 2)

    def test_a_body_is_small_or_large_against_its_own_range(self):
        self.assertEqual(body_mark({"o": 1, "h": 3, "l": 1, "c": 2}), "up-small")
        self.assertEqual(body_mark({"o": 1, "h": 2, "l": 1, "c": 2}), "up-large")
        self.assertEqual(body_mark({"o": 2, "h": 2, "l": 1, "c": 1}), "down-large")

    def test_an_empty_table_follows_the_samples_and_a_stored_count_outranks_them(self):
        up = [{"o": 1, "h": 2, "l": 1, "c": 2, "v": 1} for _ in range(6)]
        down = [{"o": 2, "h": 2, "l": 1, "c": 1, "v": 1} for _ in range(6)]
        chosen, agreed, scored, shape = choose_path([up, down, down], {})
        self.assertFalse(scored)
        self.assertEqual(agreed, 2)
        self.assertEqual(shape, shape_of(down))
        self.assertEqual(chosen, down)
        counts = {shape_of(up): 4, shape_of(down): 1}
        chosen, agreed, scored, shape = choose_path([up, down, down, down], counts)
        self.assertTrue(scored)
        self.assertEqual(shape, shape_of(up))
        self.assertEqual(agreed, 1)

    def test_a_halt_is_not_filled_with_a_predicted_candle(self):
        friday = datetime(2026, 10, 9, 16, 30, tzinfo=ET)
        weekend = next_open_times(friday.isoformat(), "15m", 4)
        self.assertEqual(len(weekend), 4)
        sunday = datetime(2026, 10, 11, 18, 0, tzinfo=ET)
        self.assertEqual(int(weekend[0]), int(sunday.timestamp()))
        for stamp in weekend:
            self.assertFalse(halt_window(stamp))
        thursday = datetime(2026, 10, 8, 16, 30, tzinfo=ET)
        reopen = next_open_times(thursday.isoformat(), "15m", 1)
        self.assertEqual(int(reopen[0]), int(datetime(2026, 10, 8, 18, 0, tzinfo=ET).timestamp()))
        self.assertFalse(halt_window(reopen[0]))


class ChartForecastTests(unittest.TestCase):
    def setUp(self):
        self.track = TemporaryDirectory()
        self.track_patch = mock.patch.object(
            __import__("futuresfund.kronos_track", fromlist=["PATH"]),
            "PATH",
            Path(self.track.name) / "kronos_direction.json",
        )
        self.track_patch.start()

    def tearDown(self):
        self.track_patch.stop()
        self.track.cleanup()

    def test_predicted_candles_stay_beside_the_printed_candles(self):
        start = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
        candles = []
        for index in range(8):
            stamp = start + timedelta(minutes=5 * index)
            candles.append({
                "t": stamp.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "et": stamp.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
                "o": 100 + index,
                "h": 102 + index,
                "l": 99 + index,
                "c": 100.5 + index,
                "v": 3,
                "forming": index == 7,
            })
        seen = {}

        def predictor(history, pred_len=6, future_times=None):
            seen["closed"] = len(history)
            seen["horizon"] = pred_len
            last = history[-1]["c"]
            path = [{"o": last, "h": last + 1, "l": last - 0.25, "c": last + 0.5, "v": 2} for _ in range(pred_len)]
            return [path, [dict(bar) for bar in path]]

        kronos_replay._live = False
        empty = Path(self.track.name) / "patterns.json"
        empty.write_text('{"windows": {}, "shapes": {}}', encoding="utf-8")
        with mock.patch("futuresfund.chart_view._candles", return_value=candles), \
                mock.patch("futuresfund.chart_view._orders", return_value=[]), \
                mock.patch.object(kronos_replay, "_PATH", empty):
            payload = chart_payload("ES1!", "5m", predictor=predictor)
        self.assertEqual(seen["closed"], 7)
        self.assertEqual(seen["horizon"], 7)
        self.assertTrue(all(not candle.get("predicted") for candle in payload["candles"]))
        self.assertEqual([candle["c"] for candle in payload["candles"]], [candle["c"] for candle in candles])
        forecast = payload["prediction"]["candles"]
        self.assertEqual(len(forecast), 6)
        self.assertTrue(all(candle.get("predicted") and not candle.get("forming") for candle in forecast))
        self.assertGreater(forecast[0]["t"], candles[-1]["t"])
        self.assertEqual(forecast[0]["c"], candles[-2]["c"] + 0.5)
        self.assertEqual(payload["prediction"]["agreed"], 2)
        self.assertEqual(payload["prediction"]["paths"], 2)
        self.assertFalse(payload["prediction"]["scored"])
        self.assertIn("has not been seen in the five-year map", payload["prediction"]["note"])
        self.assertIn("not an order", payload["prediction"]["note"])
        self.assertNotIn("net_profit", payload["prediction"])
        self.assertIsNone(payload["direction_match"]["percent"])
        self.assertIn("waits until 6 candles", payload["direction_match"]["text"])

    def test_a_tick_of_the_forming_bar_does_not_change_the_forecast_key(self):
        closed = {"t": "2026-10-09T09:40:00+00:00", "forming": False}
        forming = {"t": "2026-10-09T09:45:00+00:00", "c": 1, "forming": True}
        ticked = {"t": "2026-10-09T09:45:00+00:00", "c": 2, "forming": True}
        opened = {"t": "2026-10-09T09:50:00+00:00", "forming": True}
        self.assertEqual(_cache_key([closed, forming], "ES", "5m", None), _cache_key([closed, ticked], "ES", "5m", None))
        self.assertNotEqual(_cache_key([closed, forming], "ES", "5m", None), _cache_key([closed, forming, opened], "es", "5m", None))

    def test_an_unreplayed_chart_says_the_pattern_table_is_empty(self):
        kronos_replay._live = False
        with mock.patch("futuresfund.chart_view._candles", return_value=[]), \
                mock.patch("futuresfund.chart_view._orders", return_value=[]), \
                mock.patch("futuresfund.kronos_replay.pattern_counts", return_value={}):
            payload = chart_payload("ES1!", "5m")
        self.assertEqual(payload["candles"], [])
        self.assertEqual(payload["prediction"]["candles"], [])
        self.assertFalse(payload["prediction"]["scored"])
        self.assertIn("The pattern table is empty.", payload["prediction"]["note"])


class SetMapTests(unittest.TestCase):
    def test_the_same_window_keeps_the_predicted_set_with_the_higher_count(self):
        window = _bars(6)
        up = [{"o": 2, "h": 3, "l": 1.5, "c": 2.5, "v": 4} for _ in range(6)]
        down = [{"o": 2, "h": 2.5, "l": 1, "c": 1.5, "v": 8} for _ in range(6)]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "kronos_patterns.json"
            self.assertEqual(kronos_replay._store_sets(window, [up], "ES", "5m", path), 1)
            self.assertEqual(kronos_replay._store_sets(window, [down, down], "ES", "5m", path), 2)
            sets = kronos_replay.stored_sets("ES", "5m", window, path)
            self.assertEqual(len(sets), 2)
            chosen, _agreed, seen, count = kronos_replay._choose_set(window, sets, [up], kronos_replay._tick("ES"))
            self.assertTrue(seen)
            self.assertEqual(count, 2)
            self.assertEqual(chosen[-1]["c"], 1.5)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("net_profit", text)
            self.assertNotIn("win_rate", text)
            self.assertNotIn("fill", text)

    def test_the_live_set_uses_the_saved_forecast_counted_most_often(self):
        friday = datetime(2026, 10, 9, 16, 15, tzinfo=ET)
        window = []
        live = []
        for index in range(6):
            stamp = friday + timedelta(minutes=5 * index)
            window.append({
                "t": stamp.isoformat(),
                "o": 100,
                "h": 101,
                "l": 99.5,
                "c": 100.25,
                "v": 10,
                "forming": False,
            })
            live.append({
                "t": stamp.isoformat(),
                "o": 200,
                "h": 201.25,
                "l": 199.5,
                "c": 200.25,
                "v": 40,
                "forming": False,
            })
        later = dict(live[-1])
        later["t"] = datetime(2026, 10, 9, 16, 55, tzinfo=ET).isoformat()
        rare = [{"o": 110, "h": 112, "l": 109, "c": 111, "v": 4} for _ in range(6)]
        common = [{"o": 90, "h": 94, "l": 89, "c": 93, "v": 8} for _ in range(6)]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "kronos_patterns.json"
            kronos_replay._store_sets(window, [rare], "ES", "5m", path)
            kronos_replay._store_sets(window, [common, common, common], "ES", "5m", path)
            result = kronos_replay.prediction_for(live + [later], "ES", "5m", path=path)
        self.assertEqual(result["count"], 3)
        self.assertTrue(result["scored"])
        self.assertEqual(result["candles"][0]["c"], 193)
        first = datetime.fromisoformat(result["candles"][0]["t"]).astimezone(ET)
        self.assertEqual(first.hour, 16)
        self.assertEqual(first.minute, 45)
        self.assertEqual(first.date(), friday.date())
        self.assertIn("stored 3 times", result["note"])
        self.assertNotIn("Sunday", result["candles"][0]["et"])

    def test_a_window_missing_from_the_map_uses_the_repeated_sample(self):
        start = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
        candles = []
        for index in range(6):
            stamp = start + timedelta(minutes=5 * index)
            candles.append({
                "t": stamp.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "o": 5000 + index,
                "h": 5001 + index,
                "l": 4999 + index,
                "c": 5000.5 + index,
                "v": 12,
                "forming": False,
            })
        repeated = [{"o": 5006, "h": 5007, "l": 5005, "c": 5006.5, "v": 20} for _ in range(6)]
        other = [{"o": 5006, "h": 5006.5, "l": 5004, "c": 5004.5, "v": 3} for _ in range(6)]

        def predictor(history, pred_len=6, future_times=None):
            return [repeated, [dict(bar) for bar in repeated], other]

        result = kronos_replay._forecast(candles, "ES", "5m", predictor, {})
        self.assertEqual(result["agreed"], 2)
        self.assertFalse(result["scored"])
        self.assertEqual(result["candles"][0]["c"], 5006.5)
        self.assertIn("has not been seen in the five-year map", result["note"])
        self.assertIn("not an order", result["note"])
        self.assertIn(result["direction"], {"Buy", "Sell", "Flat"})

    def test_the_rnd_payload_names_the_candles_the_prediction_and_the_direction(self):
        start = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
        candles = []
        for index in range(6):
            stamp = start + timedelta(minutes=5 * index)
            candles.append({
                "t": stamp.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "o": 100 + index,
                "h": 101 + index,
                "l": 99 + index,
                "c": 100.5 + index,
                "v": 4,
                "forming": False,
            })

        def predictor(history, pred_len=6, future_times=None):
            last = history[-1]["c"]
            path = [{"o": last, "h": last + 1, "l": last - 0.25, "c": last + 0.5, "v": 9} for _ in range(pred_len)]
            return [path, [dict(bar) for bar in path]]

        result = kronos_replay._forecast(candles, "NQ", "15m", predictor, {})
        report = kronos_replay.kronos_report()
        self.assertTrue(report["candles"])
        self.assertEqual(len(report["predicted"]), 6)
        self.assertEqual(report["direction"], result["direction"])
        self.assertIn(report["direction"], {"Buy", "Sell", "Flat"})
        self.assertIn("has not been seen in the five-year map", report["note"])
        text = json.dumps(report)
        self.assertNotIn("net_profit", text)
        self.assertNotIn("win_rate", text)
        self.assertNotIn("place_order", text)

    def test_the_five_year_walk_keeps_the_download_span(self):
        old = {"t": datetime(2021, 10, 3, tzinfo=ET).timestamp(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
        first = {"t": datetime(2021, 10, 4, tzinfo=ET).timestamp(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
        last = {"t": datetime(2026, 10, 3, 12, tzinfo=ET).timestamp(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
        after = {"t": datetime(2026, 10, 4, tzinfo=ET).timestamp(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
        kept = kronos_replay._five_year([old, first, last, after])
        self.assertEqual(kept, [first, last])
        source = inspect.getsource(kronos_replay._research_loop)
        self.assertIn("_five_year", source)
        self.assertNotIn("_one_year", source)
        self.assertNotIn("window_start", source)


class DrawingTests(unittest.TestCase):
    def test_a_predicted_candle_is_dashed_and_a_real_candle_is_solid(self):
        root = Path(__file__).resolve().parents[1] / "futuresfund" / "static"
        js = (root / "app.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("function candleDash(candle)", js)
        self.assertIn("candle.predicted ? [5, 4] : []", js)
        self.assertIn("context.setLineDash(candleDash(candle))", js)
        self.assertIn("context.strokeRect(bodyX, bodyTop, bodyW, bodyH)", js)
        self.assertLess(js.index("if (candle.predicted)"), js.index("context.strokeRect(bodyX, bodyTop, bodyW, bodyH)"))
        self.assertIn("context.fillRect(bodyX, bodyTop, bodyW, bodyH)", js)
        self.assertIn("withGaps(chartCandles(payload), payload.timeframe)", js)
        self.assertIn("payload.prediction.note", js)
        self.assertIn("!(item.candle && item.candle.predicted)", js)
        self.assertIn("app.js?v=148", html)
        self.assertIn("Call Meeting", html)
        self.assertIn('data-meeting="portfolio"', html)
        self.assertIn("meeting-table.jpg", html)
        self.assertIn("function paintMeetingVoice()", js)
        self.assertIn("ensureKronosPaper", js)
        self.assertIn("shownAccounts", js)
        self.assertLess(html.index('id="kronos-accuracy"'), html.index('id="chart-canvas"'))
        self.assertIn("data-kronos-follow", html)
        self.assertIn("paintKronosAccuracy", js)
        self.assertIn("candle.direction", js)
        self.assertIn("kronosBoard", js)
        for module in (kronos_replay, kronos_model):
            source = inspect.getsource(module)
            self.assertNotIn("record_fill", source)
            self.assertNotIn("place_order", source)
            self.assertNotIn("strategy.entry", source)


if __name__ == "__main__":
    unittest.main()
