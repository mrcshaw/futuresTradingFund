"""The chart match rate waits for six candles. The free trader papers Kronos buy and sell signals."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import futuresfund.book as book_module
import futuresfund.kronos_track as track
import futuresfund.live_trading as live

ET = ZoneInfo("America/New_York")
START = int(datetime(2026, 10, 14, 10, 0, tzinfo=ET).timestamp())


def _candles(count: int, *, low: float = 4990.0, high: float = 5010.0, close: float = 5005.0) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append({
            "t": START + index * 300,
            "o": 5000.0,
            "h": high,
            "l": low,
            "c": 100.0 if index == 0 else close,
            "v": 10,
            "forming": False,
        })
    return rows


def _buy(candles, _root, _frame):
    closed = [candle for candle in candles if not candle.get("forming")]
    if len(closed) == 1:
        return {"candles": [{"o": 1, "h": 2, "l": 1, "c": 2, "v": 10}], "direction": "Buy", "anchor": closed[0]["t"]}
    return {"candles": [], "direction": "", "anchor": ""}


class Board:
    def __init__(self):
        self.posts = []

    def post(self, author, text, kind="report", channel=None):
        self.posts.append((author, text))

    def log(self, author, text):
        pass

    def set_task(self, agent, text, working=False):
        pass


class WeightTests(unittest.TestCase):
    def test_the_forecast_loads_the_finished_fine_tune(self):
        from futuresfund.kronos_model import _FINETUNE

        self.assertTrue((_FINETUNE / "tokenizer" / "best_model" / "config.json").is_file())
        self.assertTrue((_FINETUNE / "basemodel" / "best_model" / "config.json").is_file())


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "kronos_direction.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_the_percentage_waits_until_six_candles_have_printed(self):
        candles = _candles(6)
        forecast = {"candles": [{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], "direction": "Buy", "anchor": candles[0]["t"]}
        early = track.observe("ES", "5m", candles[:1], forecast, contract="ES1!", path=self.path)
        self.assertFalse(early["ready"])
        self.assertIsNone(early["percent"])
        self.assertNotIn("%", early["text"])
        self.assertIn("waits until 6 candles", early["text"])
        still = track.observe("ES", "5m", candles, {"candles": [], "direction": "", "anchor": ""}, contract="ES1!", path=self.path)
        self.assertFalse(still["ready"])
        self.assertEqual(still["pending"], 1)

    def test_a_finished_set_counts_only_when_the_direction_matches(self):
        first = _candles(7, close=5100.0)
        buy = {"candles": [{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], "direction": "Buy", "anchor": first[0]["t"]}
        track.observe("ES", "5m", first[:1], buy, contract="ES1!", path=self.path)
        matched = track.observe("ES", "5m", first, {"candles": [], "anchor": ""}, contract="ES1!", path=self.path)
        self.assertEqual((matched["correct"], matched["scored"], matched["percent"]), (1, 1, 100))
        self.assertIn("1 of 1 time, 100%", matched["text"])
        second = _candles(7)
        for candle in second:
            candle["t"] += 10000
        sell = {"candles": [{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], "direction": "Sell", "anchor": second[0]["t"]}
        track.observe("ES", "5m", second[:1], sell, contract="ES1!", path=self.path)
        mixed = track.observe("ES", "5m", second, {"candles": [], "anchor": ""}, contract="ES1!", path=self.path)
        self.assertEqual((mixed["correct"], mixed["scored"], mixed["percent"]), (1, 2, 50))

    def test_a_forecast_stays_on_its_own_bar_when_later_candles_have_printed(self):
        candles = _candles(3)
        stale = {"candles": [{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], "direction": "Buy", "anchor": candles[0]["t"]}
        result = track.observe("ES", "5m", candles, stale, contract="ES1!", path=self.path)
        self.assertEqual(result["pending"], 1)
        self.assertIsNone(result["percent"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        pending = saved["ES|5m"]["pending"]
        self.assertIn(str(track.epoch_of(candles[0]["t"])), pending)
        self.assertNotIn(str(track.epoch_of(candles[-1]["t"])), pending)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(book_module, "BOOK_PATH", Path(self.folder.name) / "book.json"),
            mock.patch.object(track, "PATH", Path(self.folder.name) / "kronos_direction.json"),
            mock.patch.object(live, "_queue", lambda board, slot_id: None),
            mock.patch.object(live, "_send_entry", side_effect=AssertionError("no live entry")),
            mock.patch.object(live, "_send_exit", side_effect=AssertionError("no live exit")),
            mock.patch("futuresfund.crosstrade._post", side_effect=AssertionError("no order may reach CrossTrade")),
            mock.patch.object(live, "_kronos_prediction", side_effect=_buy),
        ]
        for patch in self.patches:
            patch.start()
        self.board = Board()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.folder.cleanup()

    def test_the_free_trader_is_the_one_not_executing_a_strategy(self):
        slot = live.start_kronos(self.board, "ES1!", "5m")
        self.assertEqual(slot["trader"], "Floor Trader")
        self.assertEqual(slot["account"], "paper-2")
        self.assertTrue(slot["paper_only"])
        book = book_module.load()
        book["executing"] = [row for row in book["executing"] if row["id"] != live.KRONOS_ID]
        book["executing"].append({"id": "card-1", "trader": "Floor Trader", "kind": "strategy"})
        book_module.save(book)
        second = live.start_kronos(self.board, "NQ1!", "2m")
        self.assertEqual(second["trader"], "Quantitative Trader")
        book = book_module.load()
        book["executing"].append({"id": "card-2", "trader": "Quantitative Trader", "kind": "strategy"})
        book["executing"] = [row for row in book["executing"] if row["id"] != live.KRONOS_ID]
        book_module.save(book)
        with self.assertRaises(ValueError):
            live.start_kronos(self.board, "GC1!", "5m")

    def test_a_buy_is_paper_even_while_live_mode_is_on_and_the_stop_caps_the_loss(self):
        book = book_module.load()
        book["trading_mode"] = "live"
        book_module.save(book)
        live.start_kronos(self.board, "ES1!", "5m")
        live._follow_kronos(self.board, live.KRONOS_ID, _candles(1))
        self.assertIsNone(live._held(book_module.load(), live.KRONOS_ID)[1])
        stopped = _candles(2, low=4970.0, high=5001.0, close=4975.0)
        live._follow_kronos(self.board, live.KRONOS_ID, stopped)
        self.assertIsNone(live._held(book_module.load(), live.KRONOS_ID)[1])
        trade = book_module.load()["trades"][-1]
        self.assertEqual(trade["mode"], "paper")
        self.assertEqual(trade["account"], "paper-2")
        self.assertEqual(trade["exit_signal"], "Stop")
        self.assertEqual(trade["exit"], 4980.0)
        self.assertEqual(trade["pnl"], -1023.5)
        self.assertGreater(trade["pnl"], -1800)

    def test_the_trade_closes_when_the_six_candles_have_printed(self):
        live.start_kronos(self.board, "ES1!", "5m")
        live._follow_kronos(self.board, live.KRONOS_ID, _candles(1))
        live._follow_kronos(self.board, live.KRONOS_ID, _candles(2))
        held = live._held(book_module.load(), live.KRONOS_ID)[1]
        self.assertEqual(held["mode"], "paper")
        self.assertEqual(held["contracts"], 1)
        self.assertEqual(held["average_price"], 5000.25)
        live._follow_kronos(self.board, live.KRONOS_ID, _candles(7))
        self.assertIsNone(live._held(book_module.load(), live.KRONOS_ID)[1])
        trade = book_module.load()["trades"][-1]
        self.assertEqual(trade["exit_signal"], "Six candles")
        self.assertEqual(trade["exit"], 5004.75)
        self.assertEqual(trade["pnl"], 214.0)

    def test_a_signal_is_not_chased_after_its_entry_bar(self):
        live.start_kronos(self.board, "NQ1!", "2m")
        candles = _candles(1)
        track.observe("NQ", "2m", candles, _buy(candles, "NQ", "2m"), contract="NQ1!")
        live._follow_kronos(self.board, live.KRONOS_ID, _candles(3))
        self.assertIsNone(live._held(book_module.load(), live.KRONOS_ID)[1])

    def test_kronos_paper_does_not_read_ninjatrader(self):
        live.start_kronos(self.board, "NQ1!", "2m")
        slot = live._slot(book_module.load(), live.KRONOS_ID)
        self.assertEqual(live._feed(slot), "kronos")
        with mock.patch("futuresfund.ninjatrader.recent_minutes", side_effect=AssertionError("NinjaTrader")):
            live._follow_kronos(self.board, live.KRONOS_ID, _candles(1))
            live._follow_kronos(self.board, live.KRONOS_ID, _candles(2))
        held = live._held(book_module.load(), live.KRONOS_ID)[1]
        self.assertIsNotNone(held)
        self.assertEqual(held["mode"], "paper")
        self.assertEqual(held["account"], "paper-2")

    def test_the_kronos_slot_does_not_run_the_strategy_engine(self):
        live.start_kronos(self.board, "ES1!", "5m")
        with mock.patch("futuresfund.trading_engine.run_for_trader", side_effect=AssertionError("engine")):
            with mock.patch("futuresfund.chart_view._candles", return_value=_candles(1)):
                self.assertEqual(live.evaluate(self.board, live.KRONOS_ID), [])


if __name__ == "__main__":
    unittest.main()
