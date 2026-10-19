"""The active strategy page, from the pasted script to the floor trader's paper trade in its own trading engine."""

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import futuresfund.book as book_module
import futuresfund.live_trading as live
import futuresfund.trading_engine as trading_engine
from tests.test_live_trading import START, UP_BAR, Board

ROOT = Path(__file__).resolve().parents[1]
EMA_BB = (ROOT / "futuresfund" / "learnedStrategies" / "ema_bb_mean_reversion_nq.pine").read_text(encoding="utf-8")
EMA_TITLE = "EMA BB Mean Reversion NQ"

SERIES = [
    {"t": START, "o": 100.0, "h": 100.25, "l": 99.75, "c": 100.0, "v": 10},
    {"t": START + 300, "o": 100.0, "h": 101.25, "l": 99.75, "c": 101.0, "v": 10},
]

BROKEN = '''//@version=5
strategy("Broken", overlay=true, process_orders_on_close=true)
x = array.get(array.new_float(0), 5)
if x > 0
    strategy.entry("Long", strategy.long)
'''


def fixture_engine(trader, inbound, outbound, stop):
    """A trading engine process whose chart is SERIES."""
    import futuresfund.live_trading as engine_live
    from futuresfund.trading_engine import _engine_process

    engine_live._execution_series = lambda root, frame, since=0, **_ignored: [dict(row) for row in SERIES]
    _engine_process(trader, inbound, outbound, stop)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def ema_card(**extra):
    row = {"id": "ema-bb-nq", "account": "paper", "instrument": "NQ1!", "timeframe": "2m", "order_instrument": "", "title": "Strategy", "text": EMA_BB, "locked": False}
    row.update(extra)
    return row


class BookFileTests(unittest.TestCase):
    """Ingestion, the traders, and the page save the book at the same time. None of them may blank it."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "book.json"
        self.patch = mock.patch.object(book_module, "BOOK_PATH", self.path)
        self.patch.start()
        book_module._GOOD.clear()

    def tearDown(self):
        self.patch.stop()
        book_module._GOOD.clear()
        self.folder.cleanup()

    def test_two_writers_from_stale_copies_keep_each_others_changes(self):
        book = book_module.load()
        book["active_strategies"] = [ema_card(title=EMA_TITLE)]
        book_module.save(book)
        ingestion = book_module.load()
        trader = book_module.load()
        trader["executing"] = [{"id": "ema-bb-nq", "trader": "Floor Trader"}]
        trader["active_strategies"][0]["locked"] = True
        book_module.save(trader)
        ingestion["intervals"].append({"action": "bar"})
        book_module.save(ingestion)
        final = book_module.load()
        self.assertEqual(final["executing"], [{"id": "ema-bb-nq", "trader": "Floor Trader"}])
        self.assertTrue(final["active_strategies"][0]["locked"])
        self.assertEqual(final["active_strategies"][0]["text"], EMA_BB)
        self.assertEqual(final["intervals"], [{"action": "bar"}])
        ingestion["intervals"].append({"action": "hold"})
        book_module.save(ingestion)
        again = book_module.load()
        self.assertEqual(again["executing"], [{"id": "ema-bb-nq", "trader": "Floor Trader"}])
        self.assertEqual(len(again["intervals"]), 2)

    def test_a_reader_never_sees_a_blank_book_while_the_trader_saves(self):
        book = book_module.load()
        book["active_strategies"] = [ema_card(title=EMA_TITLE)]
        book["intervals"] = [{"n": i, "note": "x" * 200} for i in range(500)]
        book_module.save(book)
        stop = threading.Event()

        def trader():
            n = 0
            while not stop.is_set():
                row = book_module.load()
                row["executing"] = [{"id": "ema-bb-nq", "n": n}]
                book_module.save(row)
                n += 1

        thread = threading.Thread(target=trader)
        thread.start()
        seen = []
        try:
            for _ in range(300):
                seen.append([row.get("id") for row in book_module.load().get("active_strategies") or []])
        finally:
            stop.set()
            thread.join()
        self.assertEqual({tuple(ids) for ids in seen}, {("ema-bb-nq",)})
        self.assertEqual(len(book_module.load()["intervals"]), 500)

    def test_an_unreadable_book_is_set_aside_and_a_known_good_one_is_used(self):
        book = book_module.load()
        book["active_strategies"] = [ema_card(title=EMA_TITLE)]
        book_module.save(book)
        self.path.write_text('{"active_strategies": [', encoding="utf-8")
        with mock.patch.object(book_module, "_READ_TRIES", 2):
            self.assertEqual(book_module.load()["active_strategies"][0]["text"], EMA_BB)
            book_module._GOOD.clear()
            fresh = book_module.load()
        self.assertNotIn("active_strategies", fresh)
        kept = list(self.path.parent.glob("book.unreadable-*.json"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding="utf-8"), '{"active_strategies": [')


class ActivePageTests(unittest.TestCase):
    """The calls the active strategy page makes: paste, Add, Remove, Start, and Stop."""

    @classmethod
    def setUpClass(cls):
        import futuresfund.server as server

        cls.server = server

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(book_module, "BOOK_PATH", Path(self.folder.name) / "book.json"),
            mock.patch.object(live, "_queue", lambda board, slot_id: None),
            mock.patch("futuresfund.crosstrade._post", side_effect=AssertionError("no order may reach CrossTrade in a test")),
            mock.patch.object(live, "_email", lambda *args, **kwargs: None),
            mock.patch.object(self.server.board, "on_error", None),
        ]
        for patch in self.patches:
            patch.start()
        book_module._GOOD.clear()
        self.client = TestClient(self.server.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        book_module._GOOD.clear()
        self.folder.cleanup()

    def cards(self):
        response = self.client.get("/api/book")
        self.assertEqual(response.status_code, 200)
        return {row["id"]: row for row in response.json()["active_strategies"]}

    def test_a_new_book_shows_the_ema_bb_nq_script_on_paper(self):
        card = self.cards()["ema-bb-nq"]
        self.assertEqual((card["instrument"], card["timeframe"], card["account"]), ("NQ1!", "2m", "paper"))
        self.assertEqual(card["title"], EMA_TITLE)
        self.assertEqual(card["text"], EMA_BB)
        self.assertFalse(card["locked"])

    def test_a_pasted_script_is_saved_and_shown_in_the_card(self):
        self.client.post("/api/active-strategies/ema-bb-nq", json=ema_card(text=""))
        response = self.client.post("/api/active-strategies/ema-bb-nq", json=ema_card())
        self.assertEqual(response.status_code, 200)
        card = self.cards()["ema-bb-nq"]
        self.assertEqual(card["text"], EMA_BB)
        self.assertEqual(card["title"], EMA_TITLE)
        self.assertEqual((card["instrument"], card["timeframe"]), ("NQ1!", "2m"))
        # A save that lands before the paste is in the box keeps the script.
        self.client.post("/api/active-strategies/ema-bb-nq", json=ema_card(text="", title="Strategy"))
        card = self.cards()["ema-bb-nq"]
        self.assertEqual((card["text"], card["title"]), (EMA_BB, EMA_TITLE))
        self.client.post("/api/active-strategies/ema-bb-nq", json=ema_card(timeframe="5m"))
        self.assertEqual(self.cards()["ema-bb-nq"]["timeframe"], "5m")

    def test_add_keeps_every_script_and_remove_takes_only_that_card(self):
        added = self.client.post("/api/active-strategies/add", json={"id": "second", "instrument": "ES1!", "timeframe": "5m"})
        self.assertEqual(added.status_code, 200)
        self.assertEqual([row["id"] for row in added.json()["strategies"]], ["ema-bb-nq", "second"])
        self.client.post("/api/active-strategies/second", json={"id": "second", "instrument": "ES1!", "timeframe": "5m", "text": UP_BAR})
        cards = self.cards()
        self.assertEqual((cards["ema-bb-nq"]["text"], cards["second"]["text"]), (EMA_BB, UP_BAR))
        self.assertEqual(cards["second"]["title"], "Up Bar")
        third = self.client.post("/api/active-strategies/add", json={"id": "blank"})
        self.assertEqual(len(third.json()["strategies"]), 3)

        removed = self.client.post("/api/active-strategies/ema-bb-nq/remove")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(set(self.cards()), {"second", "blank"})
        self.client.post("/api/active-strategies/second/remove")
        self.assertEqual(set(self.cards()), {"blank"})
        missing = self.client.post("/api/active-strategies/second/remove")
        self.assertEqual(missing.status_code, 400)

    def test_start_hands_the_card_to_the_floor_trader_and_stop_releases_it(self):
        self.client.post("/api/active-strategies/add", json={"id": "second", "instrument": "ES1!", "timeframe": "5m", "text": UP_BAR})
        self.client.post("/api/active-strategies/add", json={"id": "third", "instrument": "ES1!", "timeframe": "5m", "text": UP_BAR})
        started = self.client.post("/api/execution/ema-bb-nq/start")
        self.assertEqual(started.status_code, 200, started.text)
        slot = next(row for row in started.json()["executing"] if row["id"] == "ema-bb-nq")
        self.assertEqual((slot["trader"], slot["contract"], slot["timeframe"], slot["account"]), ("Floor Trader", "NQ1!", "2m", "paper"))
        self.assertEqual(slot["title"], EMA_TITLE)
        self.assertEqual(slot["engine"]["trader"], "Floor Trader")
        card = self.cards()["ema-bb-nq"]
        self.assertTrue(card["locked"])
        self.assertEqual(card["text"], EMA_BB)
        self.assertEqual(self.client.post("/api/active-strategies/ema-bb-nq/remove").status_code, 400)
        self.client.post("/api/active-strategies/ema-bb-nq", json=ema_card(text="", timeframe="15m"))
        self.assertEqual((self.cards()["ema-bb-nq"]["text"], self.cards()["ema-bb-nq"]["timeframe"]), (EMA_BB, "2m"))

        second = self.client.post("/api/execution/second/start")
        self.assertEqual(next(row for row in second.json()["executing"] if row["id"] == "second")["trader"], "Quantitative Trader")
        full = self.client.post("/api/execution/third/start")
        self.assertEqual(full.status_code, 400)
        self.assertIn("Two strategies are executing", full.json()["detail"])

        stopped = self.client.post("/api/execution/ema-bb-nq/stop")
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual([row["id"] for row in stopped.json()["executing"]], ["second"])
        card = self.cards()["ema-bb-nq"]
        self.assertFalse(card["locked"])
        self.assertEqual(card["text"], EMA_BB)
        again = self.client.post("/api/execution/ema-bb-nq/start")
        self.assertEqual(next(row for row in again.json()["executing"] if row["id"] == "ema-bb-nq")["trader"], "Floor Trader")

    def test_a_card_without_a_script_does_not_start(self):
        self.client.post("/api/active-strategies/add", json={"id": "blank"})
        response = self.client.post("/api/execution/blank/start")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Paste the Pine strategy", response.json()["detail"])


class TradingEngineTests(unittest.TestCase):
    """The floor trader's script runs in the floor trader's own process. The trade is booked on paper."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(book_module, "BOOK_PATH", Path(self.folder.name) / "book.json"),
            mock.patch.object(live, "_queue", lambda board, slot_id: None),
            mock.patch("futuresfund.crosstrade._post", side_effect=AssertionError("no order may reach CrossTrade in a test")),
            mock.patch.object(live, "_email", lambda *args, **kwargs: None),
            mock.patch.object(live, "_minutes", return_value=[]),
        ]
        for patch in self.patches:
            patch.start()
        book_module._GOOD.clear()
        self.board = Board()
        book = book_module.load()
        book["active_strategies"] = [
            {"id": "card-1", "account": "paper", "instrument": "ES1!", "timeframe": "5m", "order_instrument": "", "title": "Up Bar", "text": UP_BAR, "locked": False},
        ]
        book_module.save(book)
        self.engines = trading_engine.TradingEngines(target=fixture_engine)
        self.engines.start()
        self.patches.append(mock.patch.object(trading_engine, "_engines", self.engines))
        self.patches[-1].start()

    def tearDown(self):
        self.engines.stop()
        for patch in reversed(self.patches):
            patch.stop()
        book_module._GOOD.clear()
        self.folder.cleanup()

    def start_at(self, source=None):
        live.start(self.board, "card-1")
        book = book_module.load()
        slot = book["executing"][0]
        slot["started_epoch"] = START
        slot["since"] = START
        if source is not None:
            slot["source"] = source
        book_module.save(book)

    def test_the_floor_traders_engine_is_its_own_process_and_books_the_paper_entry(self):
        self.start_at()
        actions = live.evaluate(self.board, "card-1")
        self.assertEqual([action["do"] for action in actions], ["enter"])
        self.assertEqual((actions[0]["stop"], actions[0]["limit"]), (99.0, 105.0))
        position = book_module.load()["positions"]["paper|ES1!|card-1"]
        self.assertEqual((position["mode"], position["trader"], position["contracts"]), ("paper", "Floor Trader", 1))
        self.assertEqual(position["average_price"], actions[0]["price"])
        engines = {row["trader"]: row for row in self.engines.view()}
        floor = engines["Floor Trader"]
        self.assertTrue(floor["running"])
        self.assertNotEqual(floor["pid"], os.getpid())
        self.assertEqual(floor["runs"], 1)
        self.assertFalse(engines["Quantitative Trader"]["running"])
        shown = live.executing_view()[0]
        self.assertEqual(shown["engine"]["pid"], floor["pid"])
        self.assertEqual(shown["position"]["side"], "long")
        again = live.evaluate(self.board, "card-1")
        self.assertEqual(again, [])
        self.assertEqual(len(book_module.load()["positions"]), 1)

    def test_a_script_error_in_the_engine_is_reported_on_the_card(self):
        self.start_at(BROKEN)
        self.assertEqual(live.evaluate(self.board, "card-1"), [])
        note = book_module.load()["executing"][0]["note"]
        self.assertIn("The script stopped on the newest bars", note)
        self.assertTrue(any("The script stopped" in text for _author, text in self.board.posts))
        self.assertEqual(book_module.load()["positions"], {})
        self.assertTrue(self.engines.view()[0]["running"])


class EngineLifetimeTests(unittest.TestCase):
    def test_the_engines_end_when_their_desk_is_gone(self):
        script = textwrap.dedent('''
            import os
            from futuresfund.engine_workers import EngineDesk
            from futuresfund.trading_engine import TradingEngines

            engines = TradingEngines()
            engines.start()
            research = EngineDesk()
            research.start()
            pids = [engines.ping("Floor Trader")["pid"]] + [proc.pid for proc in research._proc]
            print(" ".join(str(pid) for pid in pids), flush=True)
            os._exit(0)
        ''')
        done = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        pids = [int(value) for value in done.stdout.split()]
        self.assertEqual(len(pids), 5)
        deadline = time.time() + 30
        while time.time() < deadline and any(_alive(pid) for pid in pids):
            time.sleep(0.25)
        self.assertEqual([pid for pid in pids if _alive(pid)], [])


if __name__ == "__main__":
    unittest.main()
