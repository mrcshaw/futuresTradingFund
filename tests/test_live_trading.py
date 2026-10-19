"""The traders execute active strategies on the live candles, on paper or live."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import futuresfund.book as book_module
import futuresfund.live_trading as live
from futuresfund.backtrader_engine import Bar
from futuresfund.pine_runtime import run_pine_live

ET = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 29, 10, 0, tzinfo=ET).timestamp())

UP_BAR = '''//@version=5
strategy("Up Bar", overlay=true, process_orders_on_close=true, slippage=1, commission_type=strategy.commission.cash_per_contract, commission_value=5.5)
if close > open and strategy.position_size == 0
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", stop=close - 2, limit=close + 4)
'''

FLIP = '''//@version=5
strategy("Flip", overlay=true, process_orders_on_close=true, slippage=1, commission_type=strategy.commission.cash_per_contract, commission_value=5.5)
if bar_index == 10
    strategy.entry("Long", strategy.long)
if bar_index == 12
    strategy.entry("Short", strategy.short)
'''

NEXT_OPEN = '''//@version=5
strategy("Next Open", overlay=true, slippage=1, commission_type=strategy.commission.cash_per_contract, commission_value=5.5)
if bar_index == 12
    strategy.entry("Long", strategy.long)
    strategy.exit("Exit", "Long", loss=8, profit=16)
'''


def bars(count=13, last_up=True, width=300):
    rows = []
    for i in range(count):
        o, c = 100.0, 100.0
        if last_up and i == count - 1:
            c = 101.0
        rows.append(Bar(START + i * width, o, max(o, c) + 0.25, min(o, c) - 0.25, c, 100.0))
    return rows


def slot(started=None, **extra):
    row = {"id": "card-1", "title": "Up Bar", "root": "ES", "contract": "ES1!", "timeframe": "5m", "started_epoch": started or START, "handled": []}
    row.update(extra)
    return row


class Board:
    def __init__(self):
        self.posts = []

    def post(self, author, text, kind="report", channel=None):
        self.posts.append((author, text))

    def log(self, author, text):
        pass

    def set_task(self, agent, text, working=False):
        pass


class DecideTests(unittest.TestCase):
    def test_a_new_entry_at_the_bar_close_is_taken_at_tradingviews_fill(self):
        series = bars()
        state = run_pine_live(UP_BAR, series, root="ES", timeframe="5m")
        actions = live.decide(slot(), None, state, series, [], 300)
        self.assertEqual([action["do"] for action in actions], ["enter"])
        enter = actions[0]
        self.assertEqual(enter["price"], 101.25)
        self.assertEqual(enter["time"], series[-1].epoch + 300)
        self.assertEqual((enter["stop"], enter["limit"]), (99.0, 105.0))
        self.assertEqual(enter["fee"], 5.5)

    def test_a_position_the_script_is_still_in_is_taken_at_the_incoming_price(self):
        series = bars()
        state = run_pine_live(UP_BAR, series, root="ES", timeframe="5m")
        actions = live.decide(slot(started=series[-1].epoch + 301), None, state, series, [], 300)
        self.assertEqual([action["do"] for action in actions], ["enter"])
        self.assertTrue(actions[0]["caught_up"])
        self.assertEqual(actions[0]["price"], series[-1].c + state["slip"])
        self.assertEqual(actions[0]["time"], series[-1].epoch + 300)

    def test_a_trade_left_open_at_the_halt_is_closed(self):
        late = int(datetime(2026, 10, 7, 17, 30, tzinfo=ET).timestamp())
        series = [Bar(late - 300, 100.0, 100.25, 99.75, 100.0, 10)]
        held = {
            "contracts": -1,
            "average_price": 100.0,
            "entry_ms": 15_30,
            "stop": None,
            "take_profit": None,
            "halt_exempt_until": late + 1800,
        }
        state = {
            "trades": [],
            "open": {"entry_ms": 15_30, "size": -1, "entry_signal": "Short"},
            "pending": {},
            "on_close": True,
            "slip": 0.25,
            "broker": None,
        }
        minutes = [(late - 60, 100.0, 100.25, 99.75, 100.0)]
        closed = live.decide(slot(), held, state, series, minutes, 300)
        self.assertEqual(closed[0]["do"], "exit")
        self.assertEqual(closed[0]["signal"], "Halt")

    def test_a_trade_still_open_after_the_reopen_is_closed(self):
        opened = int(datetime(2026, 10, 7, 10, 0, tzinfo=ET).timestamp())
        late = int(datetime(2026, 10, 7, 18, 5, tzinfo=ET).timestamp())
        series = [Bar(late - 300, 100.0, 100.25, 99.75, 100.0, 10)]
        held = {"contracts": -1, "average_price": 100.0, "entry_ms": opened * 1000, "stop": None, "take_profit": None}
        state = {
            "trades": [],
            "open": {"entry_ms": opened * 1000, "size": -1, "entry_signal": "Short"},
            "pending": {},
            "on_close": True,
            "slip": 0.25,
            "broker": None,
        }
        minutes = [(late - 60, 100.0, 100.25, 99.75, 100.0)]
        closed = live.decide(slot(), held, state, series, minutes, 300)
        self.assertEqual(closed[0]["do"], "exit")
        self.assertEqual(closed[0]["signal"], "Halt")

    def test_the_stop_is_reached_inside_the_next_bar(self):
        series = bars()
        state = run_pine_live(UP_BAR, series, root="ES", timeframe="5m")
        end = series[-1].epoch + 300
        held = {"contracts": 1, "average_price": 101.25, "entry_ms": series[-1].epoch * 1000, "stop": 99.0, "take_profit": 105.0}
        quiet = [(end, 101.0, 101.5, 100.5, 101.0)]
        self.assertEqual(live.decide(slot(), held, state, series, quiet, 300), [])
        falling = quiet + [(end + 60, 100.5, 100.75, 98.5, 99.0)]
        actions = live.decide(slot(), held, state, series, falling, 300)
        self.assertEqual([action["do"] for action in actions], ["exit"])
        self.assertEqual(actions[0]["price"], 98.75)
        self.assertEqual(actions[0]["time"], end + 60)

    def test_a_reversal_the_script_is_still_in_is_taken(self):
        series = bars(last_up=False)
        state = run_pine_live(FLIP, series, root="ES", timeframe="5m")
        held = {"contracts": 1, "average_price": 100.25, "entry_ms": series[10].epoch * 1000}
        actions = live.decide(slot(), held, state, series, [], 300)
        self.assertEqual([action["do"] for action in actions], ["exit", "enter"])
        self.assertEqual(actions[0]["price"], 99.75)
        self.assertEqual(actions[1]["entry_ms"], series[12].epoch * 1000)
        self.assertEqual(actions[1]["side"], -1)

    def test_a_next_open_entry_fills_at_the_first_minute_of_the_next_bar(self):
        series = bars(last_up=False)
        state = run_pine_live(NEXT_OPEN, series, root="ES", timeframe="5m")
        end = series[-1].epoch + 300
        self.assertEqual(live.decide(slot(), None, state, series, [], 300), [])
        actions = live.decide(slot(), None, state, series, [(end, 100.5, 100.75, 100.25, 100.5)], 300)
        self.assertEqual([action["do"] for action in actions], ["enter"])
        self.assertEqual(actions[0]["price"], 100.75)
        self.assertEqual(actions[0]["entry_ms"], end * 1000)
        self.assertEqual((actions[0]["stop"], actions[0]["limit"]), (98.75, 104.75))
        now = live.decide(slot(), None, state, series, [], 300, mode="live")
        self.assertEqual(now[0]["do"], "enter")
        self.assertEqual(now[0]["live_price"], 100.25)

    def test_no_risk_is_held_into_the_halt(self):
        late = int(datetime(2026, 9, 29, 16, 44, tzinfo=ET).timestamp())
        self.assertTrue(live.halt_window(late + 60))
        self.assertFalse(live.halt_window(late))
        self.assertFalse(live.halt_window(int(datetime(2026, 9, 29, 18, 0, tzinfo=ET).timestamp())))
        self.assertTrue(live.halt_window(int(datetime(2026, 10, 3, 12, 0, tzinfo=ET).timestamp())))

    def test_the_live_contract_is_the_front_month(self):
        self.assertEqual(live.front_month("ES", date(2026, 10, 3)), "ES 12-26")
        self.assertEqual(live.front_month("ES", date(2026, 12, 9)), "ES 12-26")
        self.assertEqual(live.front_month("NQ", date(2026, 12, 11)), "NQ 03-27")
        self.assertEqual(live.front_month("QO", date(2026, 10, 3)), "")


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(book_module, "BOOK_PATH", Path(self.folder.name) / "book.json"),
            mock.patch.object(live, "_queue", lambda board, slot_id: None),
            mock.patch("futuresfund.crosstrade._post", side_effect=AssertionError("no order may reach CrossTrade in a test")),
            mock.patch.object(live, "_email", lambda *args, **kwargs: None),
        ]
        for patch in self.patches:
            patch.start()
        self.board = Board()
        book = book_module.load()
        book["active_strategies"] = [
            {"id": "card-1", "account": "paper", "instrument": "ES1!", "timeframe": "5m", "order_instrument": "", "title": "Up Bar", "text": UP_BAR, "locked": False},
            {"id": "card-2", "account": "paper", "instrument": "NQ1!", "timeframe": "2m", "order_instrument": "", "title": "Flip", "text": FLIP, "locked": False},
            {"id": "card-3", "account": "paper", "instrument": "ES1!", "timeframe": "15m", "order_instrument": "", "title": "Next Open", "text": NEXT_OPEN, "locked": False},
        ]
        book_module.save(book)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.folder.cleanup()

    def enter(self, slot_id="card-1", price=101.25):
        live._apply(self.board, slot_id, {
            "do": "enter", "entry_ms": START * 1000, "side": 1, "qty": 1, "price": price, "live_price": price,
            "time": START + 300, "signal": "Long", "fee": 5.5, "stop": 99.0, "limit": 105.0,
        })

    def test_the_floor_trader_takes_the_first_and_the_quantitative_trader_the_second(self):
        first = live.start(self.board, "card-1")
        second = live.start(self.board, "card-2")
        self.assertEqual((first["trader"], second["trader"]), ("Floor Trader", "Quantitative Trader"))
        with self.assertRaisesRegex(ValueError, "Two strategies are executing"):
            live.start(self.board, "card-3")
        cards = {row["id"]: row for row in book_module.load()["active_strategies"]}
        self.assertTrue(cards["card-1"]["locked"])
        self.assertEqual(cards["card-1"]["order_instrument"], live.front_month("ES"))
        with self.assertRaisesRegex(ValueError, "Stop the strategy"):
            book_module.save_active_strategies([row for row in cards.values() if row["id"] != "card-1"])
        live.stop(self.board, "card-1")
        third = live.start(self.board, "card-3")
        self.assertEqual(third["trader"], "Floor Trader")

    def test_a_paper_trade_is_booked_marked_by_ingestion_and_closed(self):
        live.start(self.board, "card-1")
        self.enter()
        book = book_module.load()
        position = book["positions"]["paper|ES1!|card-1"]
        self.assertEqual((position["contracts"], position["average_price"], position["mode"], position["trader"]), (1, 101.25, "paper", "Floor Trader"))
        self.assertEqual(book["orders"][-1]["rating"], "Floor Trader entry")
        book_module.mark_paper(book_module.load(), {"instrument": "ES1!", "price": 103.0})
        view = live.executing_view()
        self.assertEqual(view[0]["position"]["last"], 103.0)
        self.assertEqual(view[0]["position"]["pnl"], 87.5)
        accounts = {row["account"]: row for row in book_module.display_accounts(book_module.load())}
        self.assertEqual(accounts["paper"]["active_pnl"], 87.5)
        live._apply(self.board, "card-1", {"do": "exit", "price": 105.0, "live_price": 105.0, "time": START + 900, "signal": "Exit", "fee": 5.5, "reason": "Target."})
        book = book_module.load()
        self.assertNotIn("paper|ES1!|card-1", book["positions"])
        self.assertEqual(book["trades"][-1]["pnl"], 176.5)
        self.assertEqual(book["account_books"]["paper"]["realized"], 176.5)
        row = live.executing_view(book)[0]
        self.assertEqual((row["trades"], row["wins"], row["realized"]), (1, 1, 176.5))

    def test_ingestion_marks_a_contract_by_its_root(self):
        live.start(self.board, "card-2")
        live._apply(self.board, "card-2", {
            "do": "enter", "entry_ms": START * 1000, "side": -1, "qty": 1, "price": 31000.0, "live_price": 31000.0,
            "time": START, "signal": "Short", "fee": 5.5,
        })
        book_module.mark_paper(book_module.load(), {"instrument": "NQ!", "price": 30990.0})
        self.assertEqual(live.executing_view()[0]["position"]["pnl"], 200.0)

    def test_stop_closes_the_open_paper_trade(self):
        live.start(self.board, "card-1")
        self.enter()
        live.stop(self.board, "card-1")
        book = book_module.load()
        self.assertEqual(book["positions"], {})
        self.assertEqual(book["trades"][-1]["exit"], 101.0)
        self.assertEqual(live.executing_view(book), [])
        self.assertFalse(book["active_strategies"][0]["locked"])

    def test_live_needs_a_live_account_and_sends_through_crosstrade(self):
        with mock.patch("futuresfund.config.prop_accounts", return_value={"TEST1"}), \
             mock.patch("futuresfund.config.public_settings", return_value={"crosstrade_configured": True}):
            live.start(self.board, "card-1")
            with self.assertRaisesRegex(ValueError, "Choose the live account"):
                live.set_mode(self.board, "live")
            live.stop(self.board, "card-1")
            cards = book_module.load()["active_strategies"]
            cards[0]["account"] = "TEST1"
            book_module.save_active_strategies(cards)
            live.start(self.board, "card-1")
            self.assertEqual(live.set_mode(self.board, "live")["mode"], "live")
            sent = []
            with mock.patch.object(live, "_send_entry", lambda board, slot, side, qty, price: sent.append(("entry", side, qty)) or {"sent": True}):
                self.enter(price=101.0)
            position = book_module.load()["positions"]["TEST1|ES1!|card-1"]
            self.assertEqual((position["mode"], position["order_instrument"]), ("live", live.front_month("ES")))
            self.assertEqual(sent, [("entry", 1, 1)])
            exit_action = {"do": "exit", "price": 99.0, "live_price": 99.25, "time": START + 600, "signal": "Exit", "fee": 5.5}
            with mock.patch.object(live, "_send_exit", lambda board, slot, position: {"sent": False, "reason": "CrossTrade returned 500."}):
                live._apply(self.board, "card-1", exit_action)
            self.assertIn("TEST1|ES1!|card-1", book_module.load()["positions"])
            with mock.patch.object(live, "_send_exit", lambda board, slot, position: {"sent": True}):
                live._apply(self.board, "card-1", exit_action)
            book = book_module.load()
            self.assertEqual(book["positions"], {})
            self.assertEqual(book["trades"][-1]["exit"], 99.25)
            self.assertEqual(book["account_books"]["TEST1"]["realized"], -98.5)
            live.set_mode(self.board, "paper")

    def test_closed_live_bars_after_the_download_are_added_and_the_open_bar_is_not(self):
        historical = [{"t": 1000, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1, "source": "databento"}]
        minutes = {1200 + i * 60: (100.0, 101.0, 99.0, 101.0, 1) for i in range(5)}
        minutes[1500] = (101.0, 102.0, 100.0, 102.0, 1)
        with mock.patch("futuresfund.research_bars.research_series", return_value=historical), \
             mock.patch("futuresfund.research_bars._live", return_value=minutes):
            series = live._execution_series("ES", "5m")
        self.assertEqual([row["t"] for row in series], [1000, 1200])
        self.assertEqual(series[-1]["source"], "live")
        self.assertEqual(series[-1]["c"], 101.0)

    def test_the_trader_enters_and_exits_from_the_script_without_an_order_alert(self):
        live.start(self.board, "card-1")
        book = book_module.load()
        slot = book["executing"][0]
        slot["started_epoch"] = START
        slot["since"] = START
        book_module.save(book)
        end = START + 300
        series = [
            {"t": START, "o": 100.0, "h": 100.25, "l": 99.75, "c": 100.0, "v": 10},
            {"t": end, "o": 100.0, "h": 101.25, "l": 99.75, "c": 101.0, "v": 10},
        ]
        with mock.patch.object(live, "_execution_series", return_value=series), \
             mock.patch.object(live, "_minutes", return_value=[]):
            actions = live.evaluate(self.board, "card-1")
        self.assertEqual([action["do"] for action in actions], ["enter"])
        self.assertEqual((actions[0]["stop"], actions[0]["limit"]), (99.0, 105.0))
        position = book_module.load()["positions"]["paper|ES1!|card-1"]
        self.assertEqual((position["stop"], position["take_profit"], position["mode"]), (99.0, 105.0, "paper"))
        falling = [(end + 360, 100.5, 100.75, 98.5, 99.0)]
        with mock.patch.object(live, "_execution_series", return_value=series), \
             mock.patch.object(live, "_minutes", return_value=falling):
            actions = live.evaluate(self.board, "card-1")
        self.assertEqual(actions[0]["do"], "exit")
        self.assertIn("stop", actions[0]["reason"])
        self.assertEqual(actions[0]["price"], 98.75)
        self.assertEqual(book_module.load()["positions"], {})

    def test_both_traders_keep_the_script_open_a_paper_trade_and_the_display_tracks_the_close(self):
        """The pasted QO script stays on the active list, and each trader's paper round trip shows on the page."""
        title = "POC Momentum Breakout v1 QO 5min chart"
        cards = [
            {"id": "poc-floor", "account": "paper", "instrument": "QO1!", "timeframe": "5m", "order_instrument": "", "title": title, "text": POC_MOMENTUM, "locked": False},
            {"id": "poc-quant", "account": "paper", "instrument": "QO1!", "timeframe": "5m", "order_instrument": "", "title": title, "text": POC_MOMENTUM, "locked": False},
        ]
        book_module.save_active_strategies(cards)
        blank = book_module.save_active_strategies([{**row, "text": "", "title": "Strategy"} for row in cards])
        self.assertEqual([row["text"] for row in blank], [POC_MOMENTUM, POC_MOMENTUM])
        self.assertEqual([row["title"] for row in blank], [title, title])
        added = book_module.save_active_strategies([
            {"id": "new-blank", "account": "paper", "instrument": "ES1!", "timeframe": "5m", "order_instrument": "", "title": "Strategy", "text": "", "locked": False},
        ])
        self.assertEqual({row["id"] for row in added}, {"poc-floor", "poc-quant", "new-blank"})
        book_module.save_active_strategies([row for row in added if row["id"] != "new-blank"])

        view = book_module.snapshot()
        shown = {row["id"]: row for row in view["active_strategies"]}
        self.assertEqual(shown["poc-floor"]["text"], POC_MOMENTUM)
        self.assertEqual(shown["poc-quant"]["instrument"], "QO1!")
        self.assertIn(title, shown["poc-quant"]["text"])

        first = live.start(self.board, "poc-floor")
        second = live.start(self.board, "poc-quant")
        self.assertEqual((first["trader"], second["trader"]), ("Floor Trader", "Quantitative Trader"))
        still = {row["id"]: row["text"] for row in book_module.load()["active_strategies"]}
        self.assertEqual(still, {"poc-floor": POC_MOMENTUM, "poc-quant": POC_MOMENTUM})

        opened, closed = momentum_bars()
        engine = run_pine_live(
            POC_MOMENTUM,
            [Bar(row["t"], row["o"], row["h"], row["l"], row["c"], row["v"]) for row in closed],
            root="QO",
            timeframe="5m",
        )
        trade = engine["trades"][-1]
        self.assertEqual(trade["entry_signal"], "Long")
        self.assertIsNone(engine["open"])
        point = 50
        pnl = round((float(trade["exit_price"]) - float(trade["entry_price"])) * int(trade["size"]) * point, 2)
        self.assertGreater(pnl, 0)

        book = book_module.load()
        for slot in book["executing"]:
            slot["started_epoch"] = opened[0]["t"]
            slot["since"] = opened[0]["t"]
        book_module.save(book)
        for card_id in ("poc-floor", "poc-quant"):
            with mock.patch.object(live, "_execution_series", return_value=opened), \
                 mock.patch.object(live, "_minutes", return_value=[]):
                actions = live.evaluate(self.board, card_id)
            self.assertEqual([action["do"] for action in actions], ["enter"])

        marked = float(trade["entry_price"]) + 2
        book_module.mark_paper(book_module.load(), {"instrument": "QO1!", "price": marked})
        view = book_module.snapshot()
        self.assertEqual(len(view["positions"]), 2)
        self.assertEqual({row["trader"] for row in view["positions"]}, {"Floor Trader", "Quantitative Trader"})
        self.assertTrue(all(row["strategy"] == title and row["instrument"] == "QO1!" and row["contracts"] == 1 for row in view["positions"]))
        open_pnl = round((marked - float(trade["entry_price"])) * point, 2)
        self.assertTrue(all(row["pnl"] == open_pnl for row in view["positions"]))
        paper = next(row for row in view["accounts"] if row["account"] == "paper")
        self.assertEqual(paper["active_pnl"], round(open_pnl * 2, 2))
        self.assertEqual(paper["pnl"], paper["active_pnl"])
        running = {row["trader"]: row for row in view["executing"]}
        for name in ("Floor Trader", "Quantitative Trader"):
            slot = running[name]
            self.assertEqual(slot["title"], title)
            self.assertEqual(slot["contract"], "QO1!")
            self.assertEqual((slot["trades"], slot["wins"], slot["realized"]), (0, 0, 0))
            self.assertEqual(slot["position"]["side"], "long")
            self.assertEqual(slot["position"]["qty"], 1)
            self.assertEqual(slot["position"]["last"], marked)
            self.assertEqual(slot["position"]["pnl"], open_pnl)
            self.assertEqual(slot["position"]["mode"], "paper")
        self.assertEqual(view["trades"], [])

        for card_id in ("poc-floor", "poc-quant"):
            with mock.patch.object(live, "_execution_series", return_value=closed), \
                 mock.patch.object(live, "_minutes", return_value=[]):
                actions = live.evaluate(self.board, card_id)
            self.assertEqual(actions[0]["do"], "exit")
            self.assertEqual(actions[0]["price"], float(trade["exit_price"]))

        view = book_module.snapshot()
        self.assertEqual(view["positions"], [])
        self.assertEqual({row["id"]: row["text"] for row in view["active_strategies"]}, {"poc-floor": POC_MOMENTUM, "poc-quant": POC_MOMENTUM})
        running = {row["trader"]: row for row in view["executing"]}
        for name in ("Floor Trader", "Quantitative Trader"):
            slot = running[name]
            self.assertIsNone(slot["position"])
            self.assertEqual((slot["trades"], slot["wins"], slot["realized"]), (1, 1, pnl))
        paper = next(row for row in view["accounts"] if row["account"] == "paper")
        self.assertEqual(paper["active_pnl"], 0)
        self.assertEqual(paper["realized"], round(pnl * 2, 2))
        self.assertEqual(paper["pnl"], round(pnl * 2, 2))
        self.assertEqual(len(view["trades"]), 2)
        self.assertEqual({row["trader"] for row in view["trades"]}, {"Floor Trader", "Quantitative Trader"})
        for row in view["trades"]:
            self.assertEqual(row["strategy"], title)
            self.assertEqual(row["account"], "paper")
            self.assertEqual(row["instrument"], "QO1!")
            self.assertEqual(row["side"], "long")
            self.assertEqual(row["qty"], 1)
            self.assertEqual(row["entry"], float(trade["entry_price"]))
            self.assertEqual(row["exit"], float(trade["exit_price"]))
            self.assertEqual(row["pnl"], pnl)


def momentum_bars():
    """A cash-session path where the pasted script is long, then flat on the next bar."""
    start = int(datetime(2026, 10, 6, 10, 0, tzinfo=ET).timestamp())
    closes = [100 - i * 0.4 for i in range(26)]
    closes.append(70)
    closes.extend(82 + i for i in range(8))
    closes.extend(90 + i * 3 for i in range(20))
    rows = []
    prev = closes[0]
    for i, close in enumerate(closes):
        low = 70 if i == 26 else min(prev, close) - 0.2
        rows.append({
            "t": start + i * 300,
            "o": prev,
            "h": max(prev, close) + 0.2,
            "l": low,
            "c": close,
            "v": 100,
        })
        prev = close
    return rows[:39], rows[:40]


POC_MOMENTUM = r'''//@version=5
// Built from: EMA SMA Pivot Filter (atr_len 16 to 14), rsi_3system_v2 (cooldown_bars 376), indicators: ta.ema, ta.rsi, ta.atr, ta.pivothigh, ta.pivotlow, ta.crossover, ta.crossunder.
strategy("POC Momentum Breakout v1 QO 5min chart", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1, initial_capital=50000, process_orders_on_close=true)

// Inputs
lookback = input.int(60, "Profile Lookback", minval=30, maxval=100)
atr_len = input.int(14, "ATR Length", minval=5, maxval=50)
rsi_len = input.int(14, "RSI Length", minval=5, maxval=50)
stop_mult = input.float(3.09, "Stop Multiplier", minval=0.5, maxval=5.0)
tp_mult = input.float(2.4413, "TP Multiplier", minval=0.5, maxval=5.0)
cooldown_bars = input.int(3, "Cooldown Bars", minval=1, maxval=20)

// Indicators
atr_val = ta.atr(atr_len)
rsi_val = ta.rsi(close, rsi_len)
ema_fast = ta.ema(close, 9)
ema_slow = ta.ema(close, 21)

// Pivot Structure
left = 5
right = 5
p_high = ta.pivothigh(high, left, right)
p_low = ta.pivotlow(low, left, right)

// Trend Filter: Higher Lows / Lower Highs structure
var float last_pivot_low = na
var float last_pivot_high = na
var int last_pivot_type = 0 // 1=low, -1=high

if not na(p_low)
    last_pivot_low := p_low
    last_pivot_type := 1
if not na(p_high)
    last_pivot_high := p_high
    last_pivot_type := -1

// Momentum Confirmation
momentum_up = ta.crossover(ema_fast, ema_slow)
momentum_dn = ta.crossunder(ema_fast, ema_slow)

// RSI Filter
rsi_oversold = rsi_val < 30
rsi_overbought = rsi_val > 70

// Entry Conditions
long_cond = momentum_up and (last_pivot_type == 1 or na) and not rsi_overbought
short_cond = momentum_dn and (last_pivot_type == -1 or na) and not rsi_oversold

// Cooldown Logic
var int last_entry_bar = -cooldown_bars

// Futures week: 18:00 New York until 16:45 the next day.
// No new trade from 16:45 to 18:00, or from Friday 16:45 until Sunday 18:00.
inSession = not na(time(timeframe.period, "1800-1645:12345", "America/New_York"))

if not inSession
    strategy.close_all(comment="Session")

// Execution
if long_cond and bar_index >= cooldown_bars and last_entry_bar <= bar_index - cooldown_bars and inSession
    strategy.entry("Long", strategy.long)
    last_entry_bar := bar_index

if short_cond and bar_index >= cooldown_bars and last_entry_bar <= bar_index - cooldown_bars and inSession
    strategy.entry("Short", strategy.short)
    last_entry_bar := bar_index

// Exits
stop_dist = atr_val * stop_mult
tp_dist = atr_val * tp_mult

strategy.exit("Exit Long", "Long", stop=close - stop_dist, limit=close + tp_dist)
strategy.exit("Exit Short", "Short", stop=close + stop_dist, limit=close - tp_dist)
'''


class NinjaFeedTests(unittest.TestCase):
    def test_crosstrade_bars_become_epoch_seconds(self):
        from futuresfund.ninjatrader import bars_from

        rows = bars_from({"bars": [{
            "time": "2026-03-18T20:00:00.0000000Z",
            "epoch": 1773864000000,
            "open": 6731.75,
            "high": 6732.0,
            "low": 6731.5,
            "close": 6731.75,
            "volume": 1,
        }]})
        self.assertEqual(rows, [{"t": 1773864000, "o": 6731.75, "h": 6732.0, "l": 6731.5, "c": 6731.75, "v": 1.0}])

    def test_the_quantitative_trader_reads_desk_minutes_after_the_download(self):
        downloaded = [{"t": START, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1, "source": "databento"}]
        minutes = {START + 60: (2.0, 3.0, 1.5, 2.5, 10.0)}
        with mock.patch.object(live, "_downloaded", return_value=(downloaded, START)), \
                mock.patch.object(live, "_ninja_minutes", side_effect=AssertionError("NinjaTrader")), \
                mock.patch("futuresfund.research_bars._live", return_value=minutes):
            series = live._execution_series("ES", "1m", START, feed=live.FEED_TV, instrument="ES 12-26")
        self.assertEqual([row["t"] for row in series], [START, START + 60])
        self.assertEqual(series[-1]["source"], "live")
        self.assertEqual(series[-1]["c"], 2.5)

    def test_the_floor_trader_does_not_read_ninjatrader(self):
        with mock.patch.object(live, "_downloaded", return_value=([], 0)), \
                mock.patch.object(live, "_ninja_minutes", side_effect=AssertionError("the floor trader stays on TradingView")), \
                mock.patch("futuresfund.research_bars._live", return_value={}):
            self.assertEqual(live._execution_series("ES", "1m", 0), [])

    def test_a_matching_trade_and_a_different_trade_are_named(self):
        chart = {"title": "Up Bar", "contract": "ES1!", "timeframe": "5m", "tick": 0.25}
        floor = {"contracts": 1, "average_price": 100.0, "entry_time": "2026-10-08 10:15"}
        same = {"contracts": 1, "average_price": 100.25, "entry_time": "2026-10-08 10:15"}
        other = {"contracts": -1, "average_price": 100.0, "entry_time": "2026-10-08 10:15"}
        self.assertIn("agree", live.comparison_text(floor, same, watching=True, **chart))
        self.assertIn("differ", live.comparison_text(floor, other, watching=True, **chart))
        self.assertIn("both flat", live.comparison_text(None, None, watching=True, **chart))
        self.assertIn("no TradingView trade", live.comparison_text(None, None, watching=False, **chart))


class NinjaDeskTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(book_module, "BOOK_PATH", Path(self.folder.name) / "book.json"),
            mock.patch.object(live, "_queue", lambda board, slot_id: None),
            mock.patch("futuresfund.crosstrade._post", side_effect=AssertionError("no order may reach CrossTrade in a test")),
            mock.patch.object(live, "_email", lambda *args, **kwargs: None),
        ]
        for patch in self.patches:
            patch.start()
        self.board = Board()
        book = book_module.load()
        book["active_strategies"] = [
            {"id": "card-1", "account": "paper", "instrument": "ES1!", "timeframe": "5m", "order_instrument": "ES 12-26", "title": "Up Bar", "text": UP_BAR, "locked": False},
            {"id": "card-2", "account": "paper", "instrument": "ES1!", "timeframe": "5m", "order_instrument": "ES 12-26", "title": "Up Bar", "text": UP_BAR, "locked": False},
        ]
        book_module.save(book)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.folder.cleanup()

    def test_the_quantitative_trader_books_the_desk_chart_on_paper_while_live_is_on(self):
        floor = live.start(self.board, "card-1")
        quant = live.start(self.board, "card-2")
        self.assertEqual((floor["trader"], quant["trader"]), ("Floor Trader", "Quantitative Trader"))
        self.assertEqual((floor["feed"], quant["feed"]), (live.FEED_TV, live.FEED_TV))
        self.assertIn("desk chart", self.board.posts[1][1])
        self.assertNotIn("NinjaTrader", self.board.posts[1][1])
        live._apply(self.board, "card-1", {
            "do": "enter", "entry_ms": START * 1000, "side": 1, "qty": 1, "price": 101.0, "live_price": 101.0,
            "time": START + 300, "signal": "Long", "fee": 5.5,
        })
        book = book_module.load()
        book["trading_mode"] = "live"
        book_module.save(book)
        with mock.patch("futuresfund.ninjatrader.recent_minutes", side_effect=AssertionError("NinjaTrader")), \
             mock.patch.object(live, "_send_entry", side_effect=AssertionError("the quantitative trader stays on paper")):
            live._apply(self.board, "card-2", {
                "do": "enter", "entry_ms": START * 1000, "side": 1, "qty": 1, "price": 101.25, "live_price": 101.5,
                "time": START + 300, "signal": "Long", "fee": 5.5, "stop": 99.0, "limit": 105.0,
            })
        position = book_module.load()["positions"]["paper|ES1!|card-2"]
        self.assertEqual((position["mode"], position["trader"], position["average_price"]), ("paper", "Quantitative Trader", 101.25))


if __name__ == "__main__":
    unittest.main()
