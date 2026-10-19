"""Active strategies the traders execute on the live candles.

Start on an active strategy card hands its Pine script to a trader. The first
strategy started is the floor trader's and the second is the quantitative
trader's. Two is the most. Each trader has a thread. The thread checks the
chart on its own and also when ingestion records a 1-minute candle. It runs
the script on the bars since the day it started, the way a strategy stays on a
live TradingView chart, and does what the script did on the newest bar. A stop
or a limit the script is waiting on is checked against every minute, so it
fills inside the bar the way TradingView fills it in real time.

On paper, a fill is booked at the script's fill price. Live, the order goes
to CrossTrade on the card's account. TradingView sends the candles only.
The entry, the stop, the take profit, and the exit are the ones in the script.
A trade the script already closed before Start is not replayed. A position the
script is still in is taken at the latest incoming price, including a reversal
the script is still holding.
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from futuresfund.book import BOOK_LOCK, _account_book, _account_size, _refresh_paper, active_strategies, load, save
from futuresfund.contracts import POINT_VALUE, TICK_SIZE, root_of

ET = ZoneInfo("America/New_York")
TRADERS = ("Floor Trader", "Quantitative Trader")
FEED_TV = "tradingview"
FEED_NT = "ninjatrader"
CONTRACTS = {"ES1!": "ES", "NQ1!": "NQ", "MNQ1!": "MNQ", "GC1!": "GC", "QO1!": "QO"}
WIDTH = {"1m": 60, "2m": 120, "5m": 300, "15m": 900}
PAPER = "paper"
PAPER_KRONOS = "paper-2"
LIVE = "live"
KRONOS_ID = "kronos-paper"
_QUARTERLY = {"ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K"}
_HANDLED_KEPT = 200

_queues: dict[str, queue.Queue] = {}
_waiting: set[str] = set()
_wait_lock = threading.Lock()
_runs: dict[tuple, tuple] = {}
_RUNS_KEPT = 4
_charts: dict[tuple, tuple] = {}
# A trading engine process sets this. The desk and the tests read the download on every run.
_CHART_SECONDS = 0.0
_WATCH_SECONDS = 30


def trading_mode(book: dict | None = None) -> str:
    book = book if book is not None else load()
    return LIVE if book.get("trading_mode") == LIVE else PAPER


def front_month(root: str, today: date | None = None) -> str:
    """NinjaTrader's name for the quarterly contract trading now. It rolls eight days before the third Friday."""
    if root not in _QUARTERLY:
        return ""
    day = today or datetime.now(ET).date()
    for year in (day.year, day.year + 1):
        for month in (3, 6, 9, 12):
            first = date(year, month, 1)
            third_friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
            if day < third_friday - timedelta(days=8):
                return f"{root} {month:02d}-{year % 100:02d}"
    return ""


def _must_be_flat(held: dict, market_time: int, on_close: bool, width: int) -> bool:
    """A position is flat from 16:45 ET, and it stays flat if the desk wakes up after the 18:00 reopen."""
    if halt_window(market_time):
        return True
    entry_ms = held.get("entry_ms")
    if not entry_ms:
        return False
    entered = int(entry_ms) // 1000 + (width if on_close else 0)
    if entered < 1_000_000_000:
        return False
    from futuresfund.tv_broker import session_deadline

    entry_when = datetime.fromtimestamp(entered, timezone.utc).astimezone(ET)
    now = datetime.fromtimestamp(int(market_time), timezone.utc).astimezone(ET)
    return now >= session_deadline(entry_when)


def halt_window(epoch: float) -> bool:
    """From 4:45pm ET to the 6:00pm open, and while the market is shut for the weekend. No new risk, and open trades close."""
    when = datetime.fromtimestamp(epoch, timezone.utc).astimezone(ET)
    minute = when.hour * 60 + when.minute
    day = when.weekday()
    if day == 5 or (day == 6 and minute < 18 * 60) or (day == 4 and minute >= 16 * 60 + 45):
        return True
    return 16 * 60 + 45 <= minute < 18 * 60


def halt_clears(epoch: float) -> float:
    """When the halt that contains this moment ends and new risk is allowed again."""
    when = datetime.fromtimestamp(float(epoch), timezone.utc).astimezone(ET)
    minute = when.hour * 60 + when.minute
    day = when.weekday()
    if (day == 4 and minute >= 16 * 60 + 45) or day == 5 or (day == 6 and minute < 18 * 60):
        sunday = when if day == 6 else when + timedelta(days=(6 - day))
        reopen = sunday.replace(hour=18, minute=0, second=0, microsecond=0)
        return reopen.timestamp()
    reopen = when.replace(hour=18, minute=0, second=0, microsecond=0)
    if minute >= 18 * 60:
        reopen += timedelta(days=1)
    return reopen.timestamp()


# What the page and the desks read.


def executing_view(book: dict | None = None, positions: list[dict] | None = None) -> list[dict]:
    from futuresfund.trading_engine import engines_view

    book = book if book is not None else load()
    rows = list(positions) if positions is not None else list((book.get("positions") or {}).values())
    engines = {row["trader"]: row for row in engines_view()}
    view = []
    for slot in _slots(book):
        held = next((row for row in rows if row.get("strategy_id") == slot["id"]), None)
        position = None
        if held:
            contracts = int(held.get("contracts") or 0)
            position = {
                "side": "long" if contracts > 0 else "short",
                "qty": abs(contracts),
                "entry": held.get("average_price"),
                "last": held.get("last_price"),
                "pnl": held.get("pnl") if "pnl" in held else _open_pnl(held),
                "stop": held.get("stop"),
                "take_profit": held.get("take_profit"),
                "entry_time": held.get("entry_time"),
                "mode": held.get("mode") or PAPER,
                "account": held.get("account"),
            }
        view.append({
            "id": slot["id"],
            "kind": slot.get("kind") or "strategy",
            "title": slot["title"],
            "contract": slot["contract"],
            "timeframe": slot["timeframe"],
            "account": slot["account"],
            "order_instrument": slot.get("order_instrument") or "",
            "trader": slot["trader"],
            "feed": _feed(slot),
            "started": slot["started"],
            "trades": int(slot.get("trades") or 0),
            "wins": int(slot.get("wins") or 0),
            "realized": round(float(slot.get("realized") or 0), 2),
            "note": slot.get("note") or "",
            "last_bar": _clock(slot["last_bar"]) if slot.get("last_bar") else "",
            "position": position,
            "engine": engines.get(slot["trader"]),
        })
    return view


def record_lines(book: dict | None = None) -> list[str]:
    """Lines for the firm record, so every desk knows what is executing."""
    book = book if book is not None else load()
    lines = [f"Trading mode for new entries: {trading_mode(book)}."]
    view = executing_view(book)
    if not view:
        lines.append("No active strategy is executing.")
    for row in view:
        held = row["position"]
        trade = (
            f"{held['side']} {held['qty']} from {held['entry']} on {held['account']} ({held['mode']})"
            if held else "flat"
        )
        if row.get("kind") == "kronos":
            lines.append(
                f"{row['trader']} is paper trading {row['title']} on the {row['timeframe']} {row['contract']} chart. "
                f"{trade}. {row['trades']} closed trades since {row['started']}, realized {row['realized']}. No order is sent."
            )
        else:
            lines.append(
                f"{row['trader']} is executing {row['title']} on the {row['timeframe']} {row['contract']} chart from {'NinjaTrader' if row.get('feed') == FEED_NT else 'TradingView'} for account {row['account']}: "
                f"{trade}. {row['trades']} closed trades since {row['started']}, realized {row['realized']}."
            )
    return lines


# Controls.


def set_mode(board, mode: str) -> dict:
    """Paper or live for the next entry. A trade already open finishes the way it was opened."""
    chosen = LIVE if str(mode or "").strip().lower() == LIVE else PAPER
    with BOOK_LOCK:
        book = load()
        if chosen == LIVE:
            from futuresfund.config import public_settings

            if not public_settings().get("crosstrade_configured"):
                raise ValueError("CrossTrade needs its webhook URL and key before live trading.")
            for slot in _slots(book):
                problem = _live_problem(book, slot)
                if problem:
                    raise ValueError(f"{slot['title']}: {problem}")
        book["trading_mode"] = chosen
        save(book)
    if chosen == LIVE:
        from futuresfund.config import dry_run

        note = (
            "Live trading is on. The floor trader's next entry goes to CrossTrade on that card's account. "
            "The quantitative trader still books the desk chart on the paper account. "
            "A trade already open on paper finishes on paper."
        )
        if dry_run():
            note += " DRY_RUN is on, so CrossTrade will be shown the order and will not receive it."
    else:
        note = (
            "Paper trading is on. The floor trader books the next entry on the paper account at the TradingView fill. "
            "The quantitative trader books the next entry on the paper account at the NinjaTrader fill. "
            "A live trade already open is still managed live."
        )
    for name in (*TRADERS, "Compliance & Operations"):
        board.post(name, note, kind="report", channel=name)
    return {"mode": chosen, "note": note}


def start(board, card_id: str) -> dict:
    """Hand a card to the next free trader. The floor trader takes the first, the quantitative trader the second."""
    from futuresfund.backtrader_engine import tester_range
    from futuresfund.pine_runtime import PineUnsupported, compile_pine

    with BOOK_LOCK:
        book = load()
        cards = active_strategies(book)
        card = next((row for row in cards if str(row.get("id")) == str(card_id)), None)
        if card is None:
            raise ValueError("That strategy is not on the active list.")
        slots = _slots(book)
        found = _slot(book, card["id"])
        if found is not None:
            return found
        if len(slots) >= len(TRADERS):
            raise ValueError("Two strategies are executing. Stop one before starting another.")
        text = str(card.get("text") or "")
        if "strategy(" not in text:
            raise ValueError("Paste the Pine strategy into the card first.")
        try:
            compile_pine(text)
        except PineUnsupported as exc:
            raise ValueError(f"The engine cannot run this script yet: {exc}") from exc
        contract = str(card.get("instrument") or "ES1!").upper()
        if contract not in CONTRACTS:
            raise ValueError("Choose ES1!, NQ1!, MNQ1!, or GC1! for the chart.")
        timeframe = str(card.get("timeframe") or "5m")
        if timeframe not in WIDTH:
            raise ValueError("Choose the 1m, 2m, 5m, or 15m chart.")
        root = CONTRACTS[contract]
        taken = {slot["trader"] for slot in slots}
        trader = next(name for name in TRADERS if name not in taken)
        feed = FEED_TV
        since, since_label = tester_range()
        now = time.time()
        slot = {
            "id": str(card["id"]),
            "title": _title(text) or str(card.get("title") or "Strategy"),
            "contract": contract,
            "root": root,
            "timeframe": timeframe,
            "account": str(card.get("account") or PAPER),
            "order_instrument": str(card.get("order_instrument") or "").strip() or front_month(root),
            "trader": trader,
            "feed": feed,
            "source": text,
            "since": since,
            "since_label": since_label,
            "started": _clock(now),
            "started_epoch": int(now),
            "was_locked": bool(card.get("locked")),
            "handled": [],
            "trades": 0,
            "wins": 0,
            "realized": 0.0,
            "last_bar": None,
            "note": "",
        }
        mode = trading_mode(book)
        if mode == LIVE and trader != "Quantitative Trader":
            problem = _live_problem(book, slot)
            if problem:
                raise ValueError(problem)
        card["locked"] = True
        card["order_instrument"] = slot["order_instrument"]
        book["active_strategies"] = cards
        book["executing"] = slots + [slot]
        save(book)
    if trader == "Quantitative Trader":
        where = "the paper account from the desk chart"
        source_line = (
            "The script runs on the bars since the download and every new desk candle. "
            "This trader does not send a CrossTrade order."
        )
    else:
        where = f"account {slot['account']} through CrossTrade as {slot['order_instrument']}" if mode == LIVE else "the paper account from the desk chart"
        source_line = "The script runs on the bars since the download and every new desk candle."
    note = (
        f"{trader} is executing {slot['title']} on the {timeframe} {contract} chart, on {where}. "
        f"{source_line} "
        "A position the script is still in is taken at the latest price. A trade it already closed is not replayed."
    )
    board.post(trader, note, kind="report", channel=trader)
    board.log(trader, f"Executing {slot['title']}")
    _queue(board, slot["id"])
    return slot


def available_trader(book: dict | None = None) -> str | None:
    """The trader who is not executing a strategy. The floor trader is first when both are free."""
    taken = {slot.get("trader") for slot in _slots(book if book is not None else load())}
    return next((name for name in TRADERS if name not in taken), None)


def start_kronos(board, contract: str, timeframe: str) -> dict:
    """Hand Kronos buy and sell signals to the free trader. The fill is paper, including while live mode is on."""
    from futuresfund.timeframe import parse_timeframe

    root = root_of(str(contract or ""))
    if root not in POINT_VALUE or root not in TICK_SIZE:
        raise ValueError("Choose a contract the desk charts.")
    frame = parse_timeframe(timeframe)
    symbol = str(contract or "").upper()
    with BOOK_LOCK:
        book = load()
        existing = _slot(book, KRONOS_ID)
        if existing is not None:
            raise ValueError(
                f"{existing['trader']} is already paper trading Kronos on the {existing['timeframe']} {existing['contract']} chart."
            )
        trader = available_trader(book)
        if trader is None:
            raise ValueError("Both traders are executing a strategy. Stop one before paper trading Kronos.")
        now = time.time()
        note = (
            f"{trader} is paper trading Kronos buy and sell signals on the {frame} {symbol} chart. "
            "One contract. The trade closes after those 6 candles, or sooner at the $1,000 stop or the $1,500 target. "
            "The fill stays on the paper account. No order is sent."
        )
        slot = {
            "id": KRONOS_ID,
            "kind": "kronos",
            "title": "Kronos direction",
            "contract": symbol,
            "root": root,
            "timeframe": frame,
            "account": PAPER_KRONOS,
            "order_instrument": "",
            "trader": trader,
            "feed": "kronos",
            "source": "",
            "since": 0,
            "since_label": "",
            "started": _clock(now),
            "started_epoch": int(now),
            "was_locked": False,
            "handled": [],
            "traded": [],
            "trades": 0,
            "wins": 0,
            "realized": 0.0,
            "last_bar": None,
            "note": note,
            "paper_only": True,
        }
        book["executing"] = _slots(book) + [slot]
        save(book)
    board.post(trader, note, kind="report", channel=trader)
    board.log(trader, f"Paper trading Kronos on {frame} {symbol}")
    _queue(board, KRONOS_ID)
    return slot


def stop(board, card_id: str) -> dict:
    """Stop executing a card. An open trade is closed first."""
    with BOOK_LOCK:
        book = load()
        slot = _slot(book, card_id)
        if slot is None:
            raise ValueError("That strategy is not executing.")
        _key, position = _held(book, slot["id"])
    if position is not None:
        side = 1 if int(position.get("contracts") or 0) > 0 else -1
        last = float(position.get("last_price") or position.get("average_price") or 0)
        tick = TICK_SIZE.get(slot["root"], 0.25)
        closed = _exit(board, slot["id"], {
            "price": last - tick * side,
            "live_price": last,
            "time": time.time(),
            "signal": "Stopped",
            "fee": float(position.get("entry_fee") or 0),
            "reason": "Stopped from the active strategy page.",
        })
        if not closed:
            raise ValueError("The open trade was not closed, so the strategy is still executing. The trader's channel has the reason.")
    with BOOK_LOCK:
        book = load()
        book["executing"] = [row for row in _slots(book) if row["id"] != slot["id"]]
        for card in book.get("active_strategies") or []:
            if str(card.get("id")) == slot["id"] and not slot.get("was_locked"):
                card["locked"] = False
        save(book)
    note = f"{slot['trader']} stopped executing {slot['title']}. {slot.get('trades') or 0} closed trades, realized {round(float(slot.get('realized') or 0), 2)}."
    board.post(slot["trader"], note, kind="report", channel=slot["trader"])
    board.log(slot["trader"], f"Stopped {slot['title']}")
    return {"ok": True, "note": note}


def resume(board) -> None:
    """After the desk restarts, the traders go on executing what they were executing."""
    for slot in _slots(load()):
        if slot.get("kind") == "kronos":
            text = (
                f"The desk restarted. {slot['trader']} is still paper trading Kronos signals on the {slot['timeframe']} {slot['contract']} chart. "
                "Fills stay on the paper account."
            )
        else:
            text = (
                f"The desk restarted. {slot['trader']} is still executing {slot['title']} on the {slot['timeframe']} {slot['contract']} chart from {'NinjaTrader' if _feed(slot) == FEED_NT else 'TradingView'}."
            )
        board.post(slot["trader"], text, kind="report", channel=slot["trader"])
        _queue(board, slot["id"])


def on_candle(board, signal: dict) -> None:
    """Ingestion recorded a candle. Each trader whose strategy is on that contract checks the script."""
    try:
        root = root_of(str(signal.get("instrument") or ""))
    except ValueError:
        return
    for slot in _slots(load()):
        if slot.get("root") == root:
            _queue(board, slot["id"])


# The trader's work on each candle.


def _execution_series(root: str, frame: str, since: int = 0, *, feed: str = FEED_TV, instrument: str = "") -> list[dict]:
    """The downloaded chart from since on, then every closed bar from this trader's live feed.

    Both traders add closed minutes from the desk chart. The bar still forming is
    left off; its minutes are what the stop and the target are checked against.
    """
    from futuresfund.research_bars import _fold, _live, research_series

    rows, last = _downloaded(root, frame, research_series, int(since or 0))
    series = [dict(row) for row in rows]
    width = WIDTH[frame]
    if feed == FEED_NT:
        newer = _ninja_minutes(instrument or front_month(root), last)
    else:
        newer = {epoch: row for epoch, row in _live(root).items() if int(epoch) > last}
    if not newer:
        return series
    last_minute = max(newer)
    for epoch, row in sorted(_fold(newer, width, partial=True).items()):
        if int(epoch) <= last or int(epoch) + width > last_minute + 60:
            continue
        series.append({
            "t": int(epoch),
            "o": row[0],
            "h": row[1],
            "l": row[2],
            "c": row[3],
            "v": row[4],
            "source": "ninjatrader" if feed == FEED_NT else "live",
        })
    return series


def _downloaded(root: str, frame: str, research_series, since: int) -> tuple[list[dict], int]:
    """The downloaded bars from since on, and the epoch of the download's last bar.

    A trading engine process keeps them for a few minutes. The live candles
    after the download are added on every run.
    """
    import bisect

    key = (root, frame, since)
    if _CHART_SECONDS > 0:
        kept = _charts.get(key)
        if kept is not None and time.time() - kept[0] < _CHART_SECONDS:
            return kept[1], kept[2]
    rows = research_series(root, frame)
    last = int(rows[-1]["t"]) if rows else 0
    if since:
        rows = rows[bisect.bisect_left(rows, since, key=lambda row: int(row["t"])):]
    if _CHART_SECONDS > 0:
        _charts.pop(key, None)
        while len(_charts) >= _RUNS_KEPT:
            _charts.pop(next(iter(_charts)))
        _charts[key] = (time.time(), rows, last)
    return rows, last


def script_run(source: str, root: str, frame: str, since: int, *, feed: str = FEED_TV, instrument: str = "") -> dict:
    """The script on the chart since the trader's start: what it did, and the newest bar it saw.

    The trader's trading engine process runs this. Everything returned is plain
    data, so it crosses back to the desk's trader thread.
    """
    from futuresfund.backtrader_engine import Bar
    from futuresfund.pine_runtime import run_pine_live

    series = _execution_series(root, frame, int(since), feed=feed, instrument=instrument)
    bars = [Bar(int(row["t"]), float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row["v"])) for row in series if int(row["t"]) >= int(since)]
    if not bars:
        return {"count": 0}
    last = bars[-1]
    newest = (last.epoch, last.o, last.h, last.l, last.c, last.v)
    key = (hashlib.sha1(source.encode("utf-8", errors="replace")).hexdigest(), root, frame, int(since), feed)
    stamp = (len(bars), newest)
    cached = _runs.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    from futuresfund.risk_style import merge_risk

    state = run_pine_live(source, bars, merge_risk({}, root), root=root, timeframe=frame)
    broker = state.pop("broker")
    state["pricing"] = {
        "point": broker.point,
        "commission": broker.commission,
        "commission_type": broker.commission_type,
        "tick": broker.tick,
        "slip": broker.slip,
        "on_close": broker.on_close,
    }
    state["risk"] = broker._risk
    result = {"count": len(bars), "last": newest, "state": state}
    _runs.pop(key, None)
    while len(_runs) >= _RUNS_KEPT:
        _runs.pop(next(iter(_runs)))
    _runs[key] = (stamp, result)
    return result


def _priced(state: dict) -> dict:
    """The script's state with a broker for its commission and exit prices, as run_pine_live returns it."""
    from futuresfund.tv_broker import Broker

    pricing = state.get("pricing") or {}
    broker = Broker(
        [],
        float(pricing.get("point") or 0),
        float(pricing.get("commission") or 0),
        0,
        tick=float(pricing.get("tick") or 0.25),
        on_close=bool(pricing.get("on_close")),
        commission_type=str(pricing.get("commission_type") or "cash_per_contract"),
    )
    broker.slip = float(pricing.get("slip") or 0)
    if state.get("risk"):
        broker.arm_risk(state["risk"])
    return {**state, "broker": broker}


def _evaluate_kronos(board, slot_id: str) -> list[dict]:
    """Follow the stored Kronos direction on this trader's chart. The script engine is not used."""
    slot = _slot(load(), slot_id)
    if slot is None:
        return []
    from futuresfund.chart_view import _candles

    candles = _candles(slot["root"], slot["timeframe"])
    return _follow_kronos(board, slot_id, candles)


def _kronos_prediction(candles: list[dict], root: str, timeframe: str) -> dict:
    from futuresfund import kronos_replay

    if not kronos_replay._live:
        kronos_replay.enable_live()
    return kronos_replay.prediction_for(list(candles)[-450:], root, timeframe)


def _follow_kronos(board, slot_id: str, candles: list[dict]) -> list[dict]:
    """Record the forecast, then paper-trade a buy or sell for the six candles that follow it."""
    slot = _slot(load(), slot_id)
    if slot is None:
        return []
    from futuresfund.kronos_track import entry_signal, observe

    root, frame = slot["root"], slot["timeframe"]
    try:
        prediction = _kronos_prediction(candles, root, frame)
    except Exception:
        prediction = {}
    try:
        observe(root, frame, candles, prediction, contract=slot["contract"])
    except Exception:
        pass
    board.set_task(slot["trader"], f"Checking Kronos on the {frame} {slot['contract']} chart.", working=True)
    _key, position = _held(load(), slot_id)
    if position is not None:
        _guard_kronos(board, slot_id, candles)
        return []
    closed = [candle for candle in candles if not candle.get("forming")]
    signal = entry_signal(root, frame, closed, set(slot.get("traded") or []))
    if signal is None:
        _update(slot_id, note=f"Waiting for a Kronos buy or sell on {slot['contract']} {frame}.")
        return []
    if halt_window(signal["bar_epoch"]):
        _remember_kronos(slot_id, signal["anchor"], "The next candle is inside the 4:45pm halt, so no paper trade was opened.")
        return []
    _open_kronos(board, slot_id, signal)
    if _held(load(), slot_id)[1] is not None:
        _guard_kronos(board, slot_id, candles)
    return []


def _open_kronos(board, slot_id: str, signal: dict) -> None:
    """Book one paper contract at the next bar's open. This does not call the live order path."""
    from futuresfund.risk_style import RISK_DEFAULTS

    slot = _slot(load(), slot_id)
    if slot is None:
        return
    side = 1 if signal["direction"] == "Buy" else -1
    tick = float(TICK_SIZE.get(slot["root"], 0.25))
    point = float(POINT_VALUE.get(slot["root"], 50))
    fill = _tick_price(float(signal["bar"]["o"]) + tick * side, tick)
    stop = _tick_price(fill - (float(RISK_DEFAULTS["stop_dollars"]) / point) * side, tick)
    limit = _tick_price(fill + (float(RISK_DEFAULTS["tp_dollars"]) / point) * side, tick)
    word = "BUY" if side > 0 else "SELL"
    anchor = int(signal["anchor"])
    key = f"{PAPER_KRONOS}|{slot['contract']}|{slot_id}"
    with BOOK_LOCK:
        book = load()
        found = _slot(book, slot_id)
        if found is None or _held(book, slot_id)[1] is not None:
            return
        traded = [int(value) for value in found.get("traded") or []]
        if anchor in traded:
            return
        traded.append(anchor)
        found["traded"] = traded[-_HANDLED_KEPT:]
        book.setdefault("positions", {})[key] = {
            "account": PAPER_KRONOS,
            "instrument": slot["contract"],
            "yahoo": f"{slot['root']}=F",
            "contracts": side,
            "average_price": fill,
            "last_price": fill,
            "updated": _now(),
            "strategy": slot["title"],
            "strategy_id": slot_id,
            "trader": slot["trader"],
            "mode": PAPER,
            "order_instrument": "",
            "entry_ms": anchor * 1000,
            "entry_time": _clock(signal["bar_epoch"]),
            "entry_signal": f"Kronos {signal['direction']}",
            "entry_fee": 5.5,
            "stop": stop,
            "take_profit": limit,
            "kronos_anchor": anchor,
            "kronos_entry": int(signal["bar_epoch"]),
            "kronos_managed": 0,
        }
        reason = (
            f"{slot['title']} {'long' if side > 0 else 'short'} 1 {slot['contract']} at {fill}, filled on paper. "
            f"Stop {stop}. Target {limit}."
        )
        _order(book, found, PAPER_KRONOS, "entry", word, 1, fill, {"sent": False, "dry_run": False}, reason)
        found["note"] = reason
        _refresh_paper(book, PAPER_KRONOS, slot["contract"])
        save(book)
    board.post(slot["trader"], f"{slot['trader']}: {reason}", kind="report", channel=slot["trader"])
    board.log(slot["trader"], f"{word} 1 {slot['contract']} for {slot['title']}")


def _guard_kronos(board, slot_id: str, candles: list[dict]) -> None:
    """Check the dollar stop on every new bar, then close the trade when its six candles have printed."""
    from futuresfund.kronos_track import epoch_of

    book = load()
    slot = _slot(book, slot_id)
    _key, position = _held(book, slot_id)
    if slot is None or position is None:
        return
    side = 1 if int(position.get("contracts") or 0) > 0 else -1
    tick = float(TICK_SIZE.get(slot["root"], 0.25))
    anchor = int(position.get("kronos_anchor") or 0)
    entry_epoch = int(position.get("kronos_entry") or 0)
    managed = int(position.get("kronos_managed") or 0)
    stop = position.get("stop")
    limit = position.get("take_profit")
    closed_after = []
    forming = None
    for candle in candles:
        stamp = epoch_of(candle.get("t"))
        if stamp is None or stamp <= anchor:
            continue
        if candle.get("forming"):
            forming = candle
            continue
        closed_after.append(candle)
    pending = [candle for candle in closed_after if epoch_of(candle.get("t")) >= entry_epoch and epoch_of(candle.get("t")) > managed]
    if forming is not None:
        stamp = epoch_of(forming.get("t"))
        if stamp is not None and stamp >= entry_epoch and stamp > managed:
            pending.append(forming)
    last_closed = managed
    for candle in pending:
        stamp = epoch_of(candle.get("t"))
        if stamp is None:
            continue
        if halt_window(stamp):
            _exit_kronos(board, slot_id, candle, float(candle["o"]) - tick * side, "Halt", "No risk is held from 4:45pm ET until the 6:00pm open.")
            return
        price, why = _kronos_level(candle, side, stop, limit, tick)
        if price is not None:
            reason = "The $1,000 stop was hit." if why == "Stop" else "The $1,500 target was hit."
            _exit_kronos(board, slot_id, candle, price, why, reason)
            return
        if not candle.get("forming"):
            last_closed = stamp
            if closed_after.index(candle) >= 5:
                _exit_kronos(
                    board,
                    slot_id,
                    candle,
                    float(candle["c"]) - tick * side,
                    "Six candles",
                    "The six candles in the forecast have printed.",
                )
                return
    if last_closed != managed:
        _touch_kronos(slot_id, candles, last_closed)


def _kronos_level(candle: dict, side: int, stop, limit, tick: float):
    """The stop is checked before the target. A gap through either fills at the open."""
    if stop is None:
        return None, ""
    open_, high, low = float(candle["o"]), float(candle["h"]), float(candle["l"])
    stop, limit = float(stop), float(limit) if limit is not None else None
    if side > 0:
        if open_ <= stop:
            return open_, "Stop"
        if low <= stop:
            return _tick_price(stop - tick, tick), "Stop"
        if limit is not None and open_ >= limit:
            return open_, "Target"
        if limit is not None and high >= limit:
            return _tick_price(limit, tick), "Target"
    else:
        if open_ >= stop:
            return open_, "Stop"
        if high >= stop:
            return _tick_price(stop + tick, tick), "Stop"
        if limit is not None and open_ <= limit:
            return open_, "Target"
        if limit is not None and low <= limit:
            return _tick_price(limit, tick), "Target"
    return None, ""


def _exit_kronos(board, slot_id: str, candle: dict, price: float, signal: str, reason: str) -> None:
    from futuresfund.kronos_track import epoch_of

    _exit(board, slot_id, {
        "price": price,
        "time": epoch_of(candle.get("t")) or time.time(),
        "signal": signal,
        "fee": 5.5,
        "reason": reason,
    })


def _touch_kronos(slot_id: str, candles: list[dict], managed: int) -> None:
    from futuresfund.kronos_track import epoch_of

    last = None
    for candle in candles:
        stamp = epoch_of(candle.get("t"))
        if stamp is not None and stamp <= managed and not candle.get("forming"):
            last = candle
    with BOOK_LOCK:
        book = load()
        _key, position = _held(book, slot_id)
        if position is None:
            return
        position["kronos_managed"] = int(managed)
        if last is not None:
            position["last_price"] = float(last["c"])
        save(book)


def _remember_kronos(slot_id: str, anchor: int, note: str) -> None:
    with BOOK_LOCK:
        book = load()
        slot = _slot(book, slot_id)
        if slot is None:
            return
        traded = [int(value) for value in slot.get("traded") or []]
        if int(anchor) not in traded:
            traded.append(int(anchor))
        slot["traded"] = traded[-_HANDLED_KEPT:]
        slot["note"] = note
        save(book)


def _tick_price(price: float, tick: float) -> float:
    if tick <= 0:
        return float(price)
    return round(round(float(price) / tick) * tick, 10)


def evaluate(board, slot_id: str) -> list[dict]:
    """Run the script on the newest bars in the trader's trading engine, then do what it did."""
    from futuresfund.backtrader_engine import Bar
    from futuresfund.trading_engine import ScriptStopped, run_for_trader

    slot = _slot(load(), slot_id)
    if slot is None:
        return []
    if slot.get("kind") == "kronos":
        return _evaluate_kronos(board, slot_id)
    root, frame = slot["root"], slot["timeframe"]
    feed = _feed(slot)
    instrument = str(slot.get("order_instrument") or "").strip() or front_month(root)
    board.set_task(slot["trader"], f"Checking {slot['title']} on the {frame} {slot['contract']} chart.", working=True)
    width = WIDTH[frame]
    try:
        run = run_for_trader(slot["trader"], slot["source"], root, frame, int(slot["since"]), feed=feed, instrument=instrument)
    except ScriptStopped as exc:
        note = f"The script stopped on the newest bars: {exc}"
        if slot.get("note") != note:
            board.post(slot["trader"], f"{slot['title']}: {note}", kind="error", channel=slot["trader"])
        _update(slot_id, note=note)
        return []
    if not run.get("count"):
        if feed == FEED_NT:
            _report_feed(board, slot_id, instrument, int(slot["since"]))
        _update(slot_id, note=f"No {frame} {slot['contract']} bars since {slot.get('since_label')} yet.")
        return []
    bars = [Bar(*run["last"])]
    state = _priced(run["state"])
    if feed == FEED_NT:
        minutes = [(epoch, *row[:4]) for epoch, row in sorted(_ninja_minutes(instrument, bars[-1].epoch + width).items())]
    else:
        minutes = _minutes(root, bars[-1].epoch + width)
    with BOOK_LOCK:
        book = load()
        slot = _slot(book, slot_id)
        if slot is None:
            return []
        _key, held = _held(book, slot_id)
        mode = trading_mode(book)
        limit = _loss_limit(book, held)
    actions = decide(slot, held, state, bars, minutes, width, mode=mode, loss_limit=limit)
    first = slot.get("last_bar") is None
    for action in actions:
        _apply(board, slot_id, action)
    note = None
    if first and not actions:
        note = _waiting_note(slot, state, width)
        board.post(slot["trader"], f"{slot['title']}: {note}", kind="report", channel=slot["trader"])
    _update(slot_id, last_bar=bars[-1].epoch, note=note)
    if feed == FEED_NT:
        _report_feed(board, slot_id, instrument, bars[-1].epoch)
        _compare(board, slot_id)
    when = _clock(bars[-1].epoch)
    if actions:
        board.set_task(slot["trader"], f"{slot['title']}: working the {frame} {slot['contract']} chart. Last bar {when}.", working=True)
    else:
        watching = note or slot.get("note") or "The script is flat. The trader waits for its next entry."
        board.set_task(
            slot["trader"],
            f"{slot['title']} on the {frame} {slot['contract']} chart. Checked {_now()}. Last bar {when}. {watching}",
            working=False,
        )
    return actions


def decide(slot: dict, held: dict | None, state: dict, bars: list, minutes: list[tuple], width: int, *, mode: str = PAPER, loss_limit: float | None = None) -> list[dict]:
    """What the trader does after this candle.

    held is the desk's open trade for this strategy, or None. minutes are the
    1-minute candles (epoch, open, high, low, close) after the newest finished
    bar. Each action carries the paper price, TradingView's fill, and the live
    price, the market now.
    """
    from futuresfund.tv_broker import bar_path, touch

    trades = state["trades"]
    engine_open = state["open"]
    pending = state["pending"]
    on_close = state["on_close"]
    slip = float(state["slip"])
    broker = state.get("broker")
    handled = set(int(value) for value in slot.get("handled") or [])
    started = int(slot["started_epoch"])
    bar_end = bars[-1].epoch + width
    last_price = float(minutes[-1][4]) if minutes else float(bars[-1].c)
    market_time = (minutes[-1][0] + 60) if minutes else bar_end
    actions: list[dict] = []

    def filled_at(trade: dict) -> int:
        return int(trade["entry_ms"]) // 1000 + (width if on_close else 0)

    def fee(price: float, qty: int) -> float:
        return float(broker.fee(price, qty)) if broker is not None else 0.0

    if held is not None:
        side = 1 if int(held["contracts"]) > 0 else -1
        qty = abs(int(held["contracts"]))
        mine = int(held.get("entry_ms") or 0)
        handled.add(mine)
        closed = next((trade for trade in trades if int(trade["entry_ms"]) == mine and (trade["size"] > 0) == (side > 0)), None)
        levels = (held.get("stop"), held.get("take_profit"))
        exiting = None
        if closed is not None:
            exiting = {
                "price": float(closed["exit_price"]),
                "time": int(closed["exit_ms"]) // 1000,
                "signal": closed["exit_signal"],
                "fee": round(float(closed["commission"]) - float(closed["entry_fee"]), 2),
                "reason": f"The script closed the trade: {closed['exit_signal']}.",
            }
        elif engine_open is not None and int(engine_open["entry_ms"]) == mine:
            if pending.get("exit"):
                levels = (pending["exit"]["stop"], pending["exit"]["limit"])
        elif mine >= bar_end * 1000:
            pass
        elif engine_open is not None and (engine_open["size"] > 0) == (side > 0):
            actions.append({"do": "adopt", "entry_ms": int(engine_open["entry_ms"])})
            if pending.get("exit"):
                levels = (pending["exit"]["stop"], pending["exit"]["limit"])
        else:
            exiting = {
                "price": last_price - slip * side,
                "time": market_time,
                "signal": "Script flat",
                "fee": fee(last_price, qty),
                "reason": "On the newest bars the script holds no trade like this one, so the trader closed it.",
            }
        if exiting is None and _must_be_flat(held, market_time, on_close, width):
            exiting = {
                "price": last_price - slip * side,
                "time": market_time,
                "signal": "Halt",
                "fee": fee(last_price, qty),
                "reason": "No risk is held from 4:45pm ET until the 6:00pm open.",
            }
        if exiting is None and loss_limit is not None:
            point = float(POINT_VALUE.get(slot["root"], 50))
            loss = -(last_price - float(held["average_price"])) * side * qty * point
            if loss >= loss_limit:
                exiting = {
                    "price": last_price - slip * side,
                    "time": market_time,
                    "signal": "Drawdown",
                    "fee": fee(last_price, qty),
                    "reason": f"The open loss of ${loss:,.2f} reached the account's ${loss_limit:,.0f} drawdown.",
                }
        if exiting is None and not on_close and engine_open is not None and int(engine_open["entry_ms"]) == mine:
            for order in pending.get("queued") or []:
                closing = order["kind"] == "close" and order.get("id") in (None, engine_open["entry_signal"])
                flipping = order["kind"] == "entry" and (order["side"] > 0) != (side > 0)
                if not (closing or flipping):
                    continue
                opening = next((row for row in minutes if int(row[0]) == bar_end), None)
                if mode == LIVE or opening is not None:
                    price = float(opening[1]) if opening is not None else last_price
                    exiting = {
                        "price": price - slip * side,
                        "time": bar_end,
                        "signal": order.get("comment") or order.get("id") or "Close",
                        "fee": fee(price, qty),
                        "reason": "The script closed the trade at the next bar's open.",
                    }
                break
        if exiting is None:
            watch_from = max(bar_end, filled_at(held) if mine else bar_end)
            stop, limit = levels
            for row in minutes:
                if int(row[0]) < watch_from:
                    continue
                candle = _Candle(*row)
                hit = None
                for price in bar_path(candle):
                    hit = touch(stop, limit, side, price, price == candle.o, slip)
                    if hit is not None:
                        break
                if hit is not None:
                    exiting = {
                        "price": hit[0],
                        "time": int(row[0]),
                        "signal": (pending.get("exit") or {}).get("label") or hit[1],
                        "fee": fee(hit[0], qty),
                        "reason": f"The {hit[1]} at {stop if hit[1] == 'stop' else limit} was reached in the {_clock(int(row[0]))} minute.",
                    }
                    break
        if exiting is not None:
            exiting["do"] = "exit"
            exiting["live_price"] = last_price
            actions.append(exiting)
            held = None
        elif levels != (held.get("stop"), held.get("take_profit")):
            actions.append({"do": "levels", "stop": levels[0], "limit": levels[1]})

    if held is not None:
        return actions
    for trade in trades:
        mark = int(trade["entry_ms"])
        if mark in handled or filled_at(trade) <= started:
            continue
        handled.add(mark)
        actions.append({"do": "skip", "entry_ms": mark, "reason": f"The script opened and closed a trade at {trade['entry_time']} between candles the desk received, so it was not taken."})
    if engine_open is not None:
        mark = int(engine_open["entry_ms"])
        if mark not in handled:
            side = 1 if engine_open["size"] > 0 else -1
            qty = abs(int(engine_open["size"]))
            fresh = filled_at(engine_open) > started
            entered_in_halt = halt_window(filled_at(engine_open))
            if fresh and (entered_in_halt or halt_window(market_time)):
                actions.append({"do": "skip", "entry_ms": mark, "reason": "The script entered inside the 4:45pm to 6:00pm ET window, so the trader did not take it."})
                return actions
            if not fresh and (entered_in_halt or halt_window(market_time)):
                actions.append({"do": "skip", "entry_ms": mark, "reason": "The session is closed from 4:45pm ET, so the trader does not open a trade."})
                return actions
            exit_levels = pending.get("exit") or {}
            if fresh:
                price = float(engine_open["entry_price"])
                when = filled_at(engine_open)
                caught_up = False
            else:
                price = last_price + slip * side
                when = market_time
                caught_up = True
            actions.append({
                "do": "enter",
                "entry_ms": mark,
                "side": side,
                "qty": qty,
                "price": price,
                "live_price": last_price + slip * side,
                "time": when,
                "signal": engine_open["entry_signal"],
                "fee": float(engine_open["entry_fee"]),
                "stop": exit_levels.get("stop"),
                "limit": exit_levels.get("limit"),
                "caught_up": caught_up,
            })
            return actions
    if not on_close:
        for order in pending.get("queued") or []:
            if order["kind"] != "entry":
                continue
            mark = bar_end * 1000
            if mark in handled:
                break
            if engine_open is not None and (engine_open["size"] > 0) == (order["side"] > 0):
                break
            side = 1 if order["side"] > 0 else -1
            if halt_window(bar_end) or halt_window(market_time):
                actions.append({"do": "skip", "entry_ms": mark, "reason": "The script entered inside the 4:45pm to 6:00pm ET window, so the trader did not take it."})
                break
            opening = next((row for row in minutes if int(row[0]) == bar_end), None)
            if mode != LIVE and opening is None:
                break
            price = (float(opening[1]) if opening is not None else last_price) + slip * side
            stop = limit = None
            if order.get("exit") and broker is not None:
                stop, limit = broker.exit_levels(order["exit"], side, price)
            actions.append({
                "do": "enter",
                "entry_ms": mark,
                "side": side,
                "qty": int(order["qty"]),
                "price": price,
                "live_price": last_price + slip * side,
                "time": bar_end,
                "signal": order["id"],
                "fee": fee(price, int(order["qty"])),
                "stop": stop,
                "limit": limit,
            })
            break
    return actions


class _Candle:
    __slots__ = ("epoch", "o", "h", "l", "c")

    def __init__(self, epoch, o, h, l, c) -> None:
        self.epoch, self.o, self.h, self.l, self.c = int(epoch), float(o), float(h), float(l), float(c)


def _apply(board, slot_id: str, action: dict) -> None:
    kind = action["do"]
    if kind == "enter":
        _enter(board, slot_id, action)
    elif kind == "exit":
        _exit(board, slot_id, action)
    elif kind in {"levels", "adopt"}:
        with BOOK_LOCK:
            book = load()
            key, position = _held(book, slot_id)
            if position is None:
                return
            if kind == "levels":
                position["stop"] = action["stop"]
                position["take_profit"] = action["limit"]
            else:
                position["entry_ms"] = action["entry_ms"]
                _handle(book, slot_id, action["entry_ms"])
            save(book)
    elif kind == "skip":
        with BOOK_LOCK:
            book = load()
            _handle(book, slot_id, action["entry_ms"])
            slot = _slot(book, slot_id)
            if slot is not None:
                slot["note"] = action["reason"]
            save(book)
        slot = _slot(load(), slot_id)
        if slot is not None:
            board.post(slot["trader"], f"{slot['title']}: {action['reason']}", kind="report", channel=slot["trader"])


def _enter(board, slot_id: str, action: dict) -> None:
    book = load()
    slot = _slot(book, slot_id)
    if slot is None:
        return
    mode = PAPER if slot.get("trader") == "Quantitative Trader" or slot.get("paper_only") else trading_mode(book)
    side = int(action["side"])
    qty = int(action["qty"])
    word = "BUY" if side > 0 else "SELL"
    trader = slot["trader"]
    if mode == LIVE:
        problem = _live_problem(book, slot)
        if problem:
            result = {"sent": False, "reason": problem}
        else:
            result = _send_entry(board, slot, side, qty, action["live_price"])
        if not (result.get("sent") or result.get("dry_run")):
            reason = f"The {word.lower()} was not sent: {result.get('reason')}"
            with BOOK_LOCK:
                book = load()
                _handle(book, slot_id, action["entry_ms"])
                _order(book, slot, slot["account"], "entry", word, qty, action["live_price"], result, reason)
                found = _slot(book, slot_id)
                if found is not None:
                    found["note"] = reason
                save(book)
            board.post(trader, f"{slot['title']}: {reason}", kind="error", channel=trader)
            return
        price = float(action["live_price"])
        account = slot["account"]
        how = "sent to CrossTrade" if result.get("sent") else "shown to CrossTrade (DRY_RUN is on)"
    else:
        result = {"sent": False, "dry_run": False}
        price = round(float(action["price"]), 2)
        account = PAPER
        how = "filled on paper from the desk chart"
    root = slot["root"]
    stop, limit = action.get("stop"), action.get("limit")
    with BOOK_LOCK:
        book = load()
        found = _slot(book, slot_id)
        if found is None:
            return
        book.setdefault("positions", {})[f"{account}|{slot['contract']}|{slot_id}"] = {
            "account": account,
            "instrument": slot["contract"],
            "yahoo": f"{root}=F",
            "contracts": side * qty,
            "average_price": price,
            "last_price": price,
            "updated": _now(),
            "strategy": slot["title"],
            "strategy_id": slot_id,
            "trader": trader,
            "mode": mode,
            "order_instrument": slot.get("order_instrument") if mode == LIVE else "",
            "entry_ms": int(action["entry_ms"]),
            "entry_time": _clock(action["time"]),
            "entry_signal": action.get("signal") or "",
            "entry_fee": round(float(action.get("fee") or 0), 2),
            "stop": stop,
            "take_profit": limit,
        }
        _handle(book, slot_id, action["entry_ms"])
        levels = _levels_text(stop, limit)
        reason = f"{slot['title']} {'long' if side > 0 else 'short'} {qty} {slot['contract']} at {price}, {how}.{levels}"
        _order(book, found, account, "entry", word, qty, price, result, reason)
        found["note"] = reason
        _refresh_paper(book, account, slot["contract"])
        save(book)
    board.post(trader, f"{trader}: {reason}", kind="report", channel=trader)
    board.log(trader, f"{word} {qty} {slot['contract']} for {slot['title']}")
    if mode == LIVE:
        _email(board, slot, account, word, qty, price, result, closing=False)


def _exit(board, slot_id: str, action: dict) -> bool:
    book = load()
    slot = _slot(book, slot_id)
    if slot is None:
        return False
    key, position = _held(book, slot_id)
    if position is None:
        return True
    trader = slot["trader"]
    contracts = int(position.get("contracts") or 0)
    side = 1 if contracts > 0 else -1
    qty = abs(contracts)
    word = "SELL" if side > 0 else "BUY"
    mode = position.get("mode") or PAPER
    result: dict = {"sent": False, "dry_run": False}
    if mode == LIVE:
        result = _send_exit(board, slot, position)
        if not (result.get("sent") or result.get("dry_run")):
            reason = f"The exit was not sent: {result.get('reason')}. The trader tries again on the next candle."
            with BOOK_LOCK:
                book = load()
                _order(book, slot, position["account"], "exit", word, qty, action.get("live_price"), result, reason)
                found = _slot(book, slot_id)
                if found is not None:
                    found["note"] = reason
                save(book)
            board.post(trader, f"{slot['title']}: {reason}", kind="error", channel=trader)
            return False
        price = float(action.get("live_price") or action["price"])
        how = "sent to CrossTrade" if result.get("sent") else "shown to CrossTrade (DRY_RUN is on)"
    else:
        price = round(float(action["price"]), 2)
        how = "filled on paper from the desk chart"
    point = float(POINT_VALUE.get(slot["root"], 50))
    entry = float(position.get("average_price") or 0)
    fees = float(position.get("entry_fee") or 0) + float(action.get("fee") or 0)
    pnl = round((price - entry) * side * qty * point - fees, 2)
    account = position["account"]
    with BOOK_LOCK:
        book = load()
        book.get("positions", {}).pop(key, None)
        state = _account_book(book, account)
        state["realized"] = round(float(state.get("realized") or 0) + pnl, 2)
        book.setdefault("trades", []).append({
            "time": _now(),
            "account": account,
            "instrument": slot["contract"],
            "side": "long" if side > 0 else "short",
            "qty": qty,
            "entry": entry,
            "exit": price,
            "pnl": pnl,
            "strategy": slot["title"],
            "trader": trader,
            "mode": mode,
            "entry_time": position.get("entry_time"),
            "exit_time": _clock(action["time"]),
            "exit_signal": action.get("signal") or "",
        })
        book["trades"] = book["trades"][-40:]
        found = _slot(book, slot_id)
        reason = f"{slot['title']} closed {qty} {slot['contract']} at {price}, {how}. P&L ${pnl:,.2f} after commission. {action.get('reason') or ''}".strip()
        if found is not None:
            found["trades"] = int(found.get("trades") or 0) + 1
            found["wins"] = int(found.get("wins") or 0) + (1 if pnl > 0 else 0)
            found["realized"] = round(float(found.get("realized") or 0) + pnl, 2)
            found["note"] = reason
        _order(book, slot, account, "exit", word, qty, price, result, reason)
        _refresh_paper(book, account, slot["contract"])
        save(book)
    board.post(trader, f"{trader}: {reason}", kind="report", channel=trader)
    board.log(trader, f"{word} {qty} {slot['contract']} to close {slot['title']}")
    if mode == LIVE:
        _email(board, slot, account, word, qty, price, result, closing=True)
    return True


# CrossTrade.


def _order_signal(slot: dict, account: str, instrument: str, qty: int, price, side: str | None) -> dict:
    return {
        "account": account,
        "instrument": instrument,
        "qty": int(qty),
        "price": price,
        "order_type": "MARKET",
        "tif": "DAY",
        "strategy": slot["title"],
        "hinted_action": side,
        "yahoo": f"{slot['root']}=F",
    }


def _send_entry(board, slot: dict, side: int, qty: int, price) -> dict:
    from futuresfund.config import max_qty
    from futuresfund.crosstrade import send
    from futuresfund.trade_confirmation import request_confirmation
    from futuresfund.trade_manager import contract_cap

    word = "BUY" if side > 0 else "SELL"
    signal = _order_signal(slot, slot["account"], slot["order_instrument"], qty, price, word)
    cap = min(contract_cap(_account_size(load(), slot["account"])), max_qty())
    if qty > cap:
        return {"sent": False, "reason": f"{qty} contracts is above the account's cap of {cap}."}
    try:
        result = send(signal, "Buy" if side > 0 else "Sell")
    except Exception as exc:
        result = {"sent": False, "reason": str(exc)}
    request_confirmation(board, signal, result, closing=False)
    return result


def _send_exit(board, slot: dict, position: dict) -> dict:
    from futuresfund.trade_confirmation import request_confirmation
    from futuresfund.trade_manager import _close

    contracts = int(position.get("contracts") or 0)
    signal = _order_signal(
        slot,
        position["account"],
        position.get("order_instrument") or slot["order_instrument"],
        abs(contracts),
        position.get("last_price"),
        "SELL" if contracts > 0 else "BUY",
    )
    try:
        result = _close(signal)
    except Exception as exc:
        result = {"sent": False, "reason": str(exc)}
    request_confirmation(board, signal, result, closing=True)
    return result


def _email(board, slot: dict, account: str, word: str, qty: int, price: float, result: dict, *, closing: bool) -> None:
    from futuresfund.session import _email_trade

    signal = _order_signal(slot, account, slot["contract"], qty, price, word)
    plan = {"action": "close" if closing else "place", "side": word, "qty": qty, "target": 0 if closing else qty}
    _email_trade(board, signal, plan, result, 1 if closing else 0, load())


def _live_problem(book: dict, slot: dict) -> str | None:
    from futuresfund.config import prop_accounts

    account = str(slot.get("account") or "").strip()
    if not account or account.lower() == PAPER:
        return "Choose the live account on the card. Paper is not a CrossTrade account."
    known = set(prop_accounts())
    rules = (book.get("account_rules") or {}).get("account")
    if rules:
        known.add(str(rules))
    if known and account not in known:
        return f"{account} is not one of the configured accounts."
    instrument = str(slot.get("order_instrument") or "").strip()
    if not instrument:
        return "Type the contract CrossTrade trades on the card, such as ES 12-26."
    try:
        if root_of(instrument) != slot["root"]:
            return f"The order contract {instrument} is not {slot['contract']}."
    except ValueError as exc:
        return str(exc)
    for other in _slots(book):
        if other["id"] != slot.get("id") and other.get("account") == account and other.get("root") == slot["root"]:
            return f"{other['title']} trades {slot['contract']} on {account} too. Two live strategies on one contract and account would net against each other."
    return None


# The book.


def _slots(book: dict) -> list[dict]:
    return [row for row in book.get("executing") or [] if isinstance(row, dict) and row.get("id")]


def _slot(book: dict, slot_id) -> dict | None:
    return next((row for row in _slots(book) if row["id"] == str(slot_id)), None)


def _held(book: dict, slot_id) -> tuple[str | None, dict | None]:
    for key, position in (book.get("positions") or {}).items():
        if position.get("strategy_id") == str(slot_id) and int(position.get("contracts") or 0):
            return key, position
    return None, None


def _handle(book: dict, slot_id: str, entry_ms) -> None:
    slot = _slot(book, slot_id)
    if slot is None:
        return
    handled = [int(value) for value in slot.get("handled") or []]
    if int(entry_ms) not in handled:
        handled.append(int(entry_ms))
    slot["handled"] = handled[-_HANDLED_KEPT:]


def _update(slot_id: str, *, last_bar: int | None = None, note: str | None = None) -> None:
    with BOOK_LOCK:
        book = load()
        slot = _slot(book, slot_id)
        if slot is None:
            return
        if last_bar is not None:
            slot["last_bar"] = int(last_bar)
        if note is not None:
            slot["note"] = note
        save(book)


def _order(book: dict, slot: dict, account: str, kind: str, side: str, qty: int, price, result: dict, reason: str) -> None:
    book.setdefault("orders", []).append({
        "time": _now(),
        "account": account,
        "instrument": slot["contract"],
        "qty": qty,
        "rating": f"{slot['trader']} {kind}",
        "side": side,
        "price": price,
        "bar_time": None,
        "sent": bool(result.get("sent")),
        "dry_run": bool(result.get("dry_run")),
        "reason": reason,
    })
    book["orders"] = book["orders"][-40:]


def _loss_limit(book: dict, held: dict | None) -> float | None:
    if held is None:
        return None
    from futuresfund.prop_rules import active_limits

    try:
        return float(active_limits(_account_size(book, str(held.get("account") or PAPER)))["max_drawdown"])
    except (KeyError, TypeError, ValueError):
        return None


def _feed(slot: dict) -> str:
    """Strategy checks read the desk chart. Kronos paper trades do too. Neither asks NinjaTrader for bars."""
    if slot.get("kind") == "kronos" or slot.get("feed") == "kronos":
        return "kronos"
    return FEED_TV


def _ninja_minutes(instrument: str, after: int) -> dict[int, tuple]:
    """Closed NinjaTrader 1-minute bars after one epoch. A minute that has not closed is left out."""
    from futuresfund.ninjatrader import recent_minutes

    rows, reason = recent_minutes(instrument, int(after))
    _ninja_minutes.reason = reason
    now = time.time()
    found = {}
    for row in rows:
        epoch = int(row["t"])
        if epoch <= int(after) or epoch + 60 > now:
            continue
        found[epoch] = (float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row.get("v") or 0))
    return found


_ninja_minutes.reason = ""


def _report_feed(board, slot_id: str, instrument: str, after: int) -> None:
    """Tell the quantitative trader once when NinjaTrader did not return bars."""
    _ninja_minutes(instrument, after)
    reason = getattr(_ninja_minutes, "reason", "")
    if not reason:
        return
    note = f"NinjaTrader bars were not read: {reason}"
    book = load()
    slot = _slot(book, slot_id)
    if slot is None or slot.get("feed_error") == note:
        return
    with BOOK_LOCK:
        book = load()
        found = _slot(book, slot_id)
        if found is None:
            return
        found["feed_error"] = note
        save(book)
    board.post(slot["trader"], f"{slot['title']}: {note}", kind="error", channel=slot["trader"])


def _compare(board, slot_id: str) -> None:
    """Say whether the quantitative trader's NinjaTrader trade matches the floor trader's TradingView trade."""
    book = load()
    slot = _slot(book, slot_id)
    if slot is None or _feed(slot) != FEED_NT:
        return
    twin = next((
        row for row in _slots(book)
        if _feed(row) == FEED_TV and row.get("title") == slot.get("title") and row.get("contract") == slot.get("contract") and row.get("timeframe") == slot.get("timeframe")
    ), None)
    _quant, quant_pos = _held(book, slot_id)
    floor_pos = None
    if twin is not None:
        _floor, floor_pos = _held(book, twin["id"])
    text = comparison_text(
        floor_pos,
        quant_pos,
        title=str(slot.get("title") or "the strategy"),
        contract=str(slot.get("contract") or ""),
        timeframe=str(slot.get("timeframe") or ""),
        tick=TICK_SIZE.get(slot.get("root"), 0.25),
        watching=twin is not None,
    )
    if text == slot.get("compare"):
        return
    with BOOK_LOCK:
        book = load()
        found = _slot(book, slot_id)
        if found is None:
            return
        found["compare"] = text
        save(book)
    board.post(slot["trader"], text, kind="report", channel=slot["trader"])


def comparison_text(floor, quant, *, title: str, contract: str, timeframe: str, tick: float, watching: bool) -> str:
    """One sentence on whether the two feeds are in the same trade."""
    chart = f"{title} {timeframe} {contract}".strip()
    if not watching:
        return f"The quantitative trader is checking {chart} on NinjaTrader bars. The floor trader is not running that same chart, so there is no TradingView trade to compare yet."
    if _same_trade(floor, quant, tick):
        return f"NinjaTrader and TradingView agree on {chart}: both {_open_label(quant)}."
    return (
        f"NinjaTrader and TradingView differ on {chart}. "
        f"TradingView is {_open_label(floor)}. NinjaTrader is {_open_label(quant)}."
    )


def _same_trade(floor, quant, tick: float) -> bool:
    left = int((floor or {}).get("contracts") or 0)
    right = int((quant or {}).get("contracts") or 0)
    if left == 0 and right == 0:
        return True
    if left == 0 or right == 0 or (left > 0) != (right > 0) or abs(left) != abs(right):
        return False
    try:
        return abs(float(floor["average_price"]) - float(quant["average_price"])) <= float(tick) + 1e-9
    except (TypeError, ValueError, KeyError):
        return False


def _open_label(position) -> str:
    contracts = int((position or {}).get("contracts") or 0)
    if contracts == 0:
        return "flat"
    side = "long" if contracts > 0 else "short"
    return f"{side} {abs(contracts)} from {position.get('average_price')} at {position.get('entry_time')} ET"


def _minutes(root: str, after: int) -> list[tuple]:
    """Ingestion's 1-minute candles from the epoch after on, oldest first."""
    from futuresfund.chart_feed import live_bars

    found = {}
    for row in live_bars(root):
        if row.get("timeframe") != "1m" or not row.get("epoch"):
            continue
        epoch = int(row["epoch"]) // 1000
        if epoch >= after:
            found[epoch] = (epoch, float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]))
    return [found[epoch] for epoch in sorted(found)]


def _waiting_note(slot: dict, state: dict, width: int) -> str:
    return "The script is flat. The trader waits for its next entry."


def _open_pnl(position: dict):
    try:
        point = float(POINT_VALUE[root_of(str(position.get("instrument") or ""))])
        return round((float(position["last_price"]) - float(position["average_price"])) * int(position["contracts"]) * point, 2)
    except (KeyError, TypeError, ValueError):
        return None


def _levels_text(stop, limit) -> str:
    parts = []
    if stop is not None:
        parts.append(f"stop {stop}")
    if limit is not None:
        parts.append(f"target {limit}")
    return f" The script's {' and '.join(parts)}." if parts else ""


def _title(source: str) -> str:
    import re

    match = re.search(r"strategy\s*\(\s*(?:title\s*=\s*)?\"([^\"]+)\"", source or "")
    return match.group(1).strip() if match else ""


def _clock(epoch) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# One thread for each trader.


def _queue(board, slot_id: str) -> None:
    slot = _slot(load(), slot_id)
    if slot is None:
        return
    trader = slot["trader"]
    with _wait_lock:
        if slot_id in _waiting:
            return
        _waiting.add(slot_id)
        jobs = _queues.get(trader)
        if jobs is None:
            jobs = queue.Queue()
            _queues[trader] = jobs
            threading.Thread(
                target=_work,
                args=(board, trader, jobs),
                daemon=True,
                name=f"execution-{trader.lower().replace(' ', '-')}",
            ).start()
    jobs.put(slot_id)


def _work(board, trader: str, jobs: queue.Queue) -> None:
    """This trader's thread. A new candle wakes it, and it also checks on its own."""
    while True:
        try:
            slot_id = jobs.get(timeout=_WATCH_SECONDS)
        except queue.Empty:
            for slot in _slots(load()):
                if slot.get("trader") == trader:
                    _queue(board, slot["id"])
            continue
        with _wait_lock:
            _waiting.discard(slot_id)
        try:
            evaluate(board, slot_id)
        except Exception as exc:
            board.post(trader, f"The strategy check stopped: {exc}", kind="error", channel=trader)
        finally:
            jobs.task_done()
