"""Initial research saves a strategy. Interval alerts trade it. Meetings keep it or adjust it."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from futuresfund.board import Board
from futuresfund.book import apply_target, load, record_interval, record_order, save
from futuresfund.contracts import yahoo_symbol
from futuresfund.ingestion import ingest_alert
from futuresfund.research import trade_plan
from futuresfund.trade_confirmation import request_confirmation
from futuresfund.trade_manager import deliver, liquidation_reason, live_unrealized, plan_from_alert, point_value, reconcile_position
from futuresfund.strategy import store_strategy
from futuresfund.timeframe import bucket_open


def portfolio_for(signal: dict | None) -> dict | None:
    if not signal or signal.get("position") is None:
        return None
    return {
        "currency": "USD",
        "positions": [{
            "ticker": signal["yahoo"],
            "quantity": signal["position"],
            "average_price": signal.get("price"),
        }],
    }


ET = ZoneInfo("America/New_York")
HALT = (
    "No new risk after 4:45pm ET, 15 minutes before the 5:00pm ET futures halt. "
    "Open contracts are flattened and stay flat until 6:00pm ET."
)


def trading_halt(moment: datetime | None = None) -> str | None:
    """The daily halt window. None means the floor may still send an order."""
    current = moment or datetime.now(ET)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ET)
    else:
        current = current.astimezone(ET)
    minutes = current.hour * 60 + current.minute
    if 16 * 60 + 45 <= minutes < 18 * 60:
        return HALT
    return None


def handle_interval(board: Board, signal: dict) -> dict:
    """Ingestion stores the bar. The floor trader places or manages the order."""
    from futuresfund.alerts import BAR_FEED_ID

    if signal.get("id") == BAR_FEED_ID:
        return _record_feed(board, signal)
    ingested = ingest_alert(board, signal)
    if not ingested.get("ok"):
        return {"ok": True, "sent": False, "reason": ingested.get("reason")}

    strategy = ingested["strategy"]
    frame = ingested["frame"]
    alert_frame = ingested["alert_frame"]
    bar = ingested["bar"]
    closed = ingested["closed"]
    bars = ingested["bars"]
    held, entry = reconcile_position(signal, *_desk_position(strategy, signal))
    point = point_value(signal["instrument"])
    bucket = bucket_open(bar["t"], frame)
    updates = load().get("prop_updates") or {}
    account_size = float(strategy.get("account_size") or 50000)

    if trading_halt() and int(held or 0) != 0:
        _remember(strategy, signal, 0, None, None)
        note = f"Floor trader, close this trade. The market halt can liquidate the account. {HALT}"
        return _flatten_now(board, strategy, signal, held, note, "Trading Analyst")

    from futuresfund.prop_rules import active_limits

    account_rules = active_limits(account_size)
    loss_note = liquidation_reason(
        account_size,
        held,
        entry,
        signal.get("price"),
        point,
        live_unrealized(signal),
        updates.get("daily_loss_limit"),
        account_rules["max_drawdown"],
    )
    if loss_note:
        _remember(strategy, signal, 0, None, bucket)
        return _flatten_now(board, strategy, signal, held, f"Floor trader, close this trade. {loss_note}", "Trading Analyst")

    located = plan_from_alert(signal, held, int(strategy.get("qty") or 1))
    if located is not None:
        return _execute_located(board, strategy, signal, held, entry, located, account_size, bars)

    if not closed:
        store_strategy(strategy)
        note = (
            f"{alert_frame} price {signal.get('price')} is inside the open {frame} candle. "
            "The strategy waits for that candle to close."
        )
        record_interval(load(), _interval_row(signal, held, "watch", {"sent": False, "reason": note}))
        board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")
        _report_trade(board, strategy, signal, bar)
        return {"ok": True, "sent": False, "action": "watch", "reason": note, "bars": len(bars)}

    if trading_halt():
        store_strategy(strategy)
        record_interval(load(), _interval_row(signal, held, "bar", {"sent": False, "reason": HALT}))
        board.post("Compliance & Operations", f"The {frame} candle is stored. {HALT}", kind="report", channel="Compliance & Operations")
        return {"ok": True, "sent": False, "action": "bar", "reason": HALT, "bars": len(bars)}

    store_strategy(strategy)
    note = (
        f"The {frame} candle {closed[-1]['t']} is stored. The alert had no buy or sell. "
        "The stop is in the Pine strategy. The floor trader sends a CrossTrade webhook only when TradingView sends that order."
    )
    record_interval(load(), _interval_row(signal, held, "bar", {"sent": False, "reason": note}))
    board.post("Floor Trader", note, kind="report", channel="Floor Trader")
    return {"ok": True, "sent": False, "action": "bar", "reason": note, "bars": len(bars)}


def _record_feed(board: Board, signal: dict) -> dict:
    """A desk-bar order is the closed candle. Record it. Do not send it."""
    ingested = ingest_alert(board, signal)
    if not ingested.get("ok"):
        return {"ok": True, "sent": False, "action": "bar", "reason": ingested.get("reason")}
    bar = ingested["bar"]
    note = (
        f"Ingestion recorded the {ingested.get('alert_frame') or 'chart'} candle {bar['t']}: "
        f"open {bar['o']}, high {bar['h']}, low {bar['l']}, close {bar['c']}, volume {bar['v']}. "
        "Marked desk-bar, so no order was sent."
    )
    board.post("Ingestion", note, kind="report", channel="Ingestion")
    _report_trade(board, ingested.get("strategy") or {}, signal, bar)
    return {"ok": True, "sent": False, "action": "bar", "reason": note, "bars": len(ingested["bars"])}


def _execute_located(board, strategy, signal, held, entry, plan, account_size, bars) -> dict:
    """The trade is already in the TradingView alert. The floor trader sends that webhook."""
    if plan["target"] == 0:
        _remember(strategy, signal, 0, None, None)
    elif plan["action"] == "place" and (int(held or 0) == 0 or (int(held) > 0) != (plan["target"] > 0)):
        _remember(strategy, signal, plan["target"], signal.get("price"), None)
    else:
        _remember(strategy, signal, plan["target"], entry, None)
    store_strategy(strategy)
    signal = {
        **signal,
        "qty": plan["qty"] or signal.get("qty") or 1,
        "flatten_first": plan["flatten_first"],
        "yahoo": yahoo_symbol(signal["instrument"]),
    }
    side = plan.get("side") or "flat"
    if plan["action"] == "hold":
        note = f"The TradingView alert is {side} and the position already matches. No webhook."
        record_interval(load(), _interval_row(signal, held, "hold", {"sent": False, "reason": note}, plan))
        board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")
        board.post("Floor Trader", note, kind="report", channel="Floor Trader")
        return {"ok": True, "sent": False, "action": "hold", "reason": note, "bars": len(bars)}
    result = _execute(signal, plan, account_size)
    _attach_confirmation(board, signal, result, closing=plan["action"] == "close")
    from futuresfund.prop_rules import rules_sentence

    from futuresfund.chart_view import match_candle

    try:
        matched = match_candle(signal)
    except ValueError:
        matched = ""
    if matched:
        board.post("Floor Trader", matched, kind="report", channel="Floor Trader")
        board.post("Quantitative Trader", matched, kind="report", channel="Quantitative Trader")
    named = signal.get("strategy") or "The strategy"
    stop = signal.get("stop_loss") or signal.get("stop_price")
    stop_line = f" Strategy stop {stop}." if stop else " The stop is the one in the Pine strategy."
    note = (
        f"{named} sent {side} {signal['qty']} {signal['instrument']} on {signal['account']}."
        f"{stop_line} Floor trader, send the CrossTrade webhook. {rules_sentence(account_size)}"
    )
    result["reason"] = f"{note} {result.get('reason') or ''}".strip()
    book = load()
    if result.get("sent") or result.get("dry_run"):
        apply_target(book, signal, plan["target"], signal.get("price") if plan["target"] else None)
    result["managed"] = True
    record_order(book, signal, plan["action"], result)
    record_interval(book, _interval_row(signal, held, plan["action"], result, plan))
    _report_trade(board, strategy, signal, {
        "c": signal.get("price"),
        "h": signal.get("high"),
        "l": signal.get("low"),
        "v": signal.get("volume"),
    })
    _show_webhook(board, signal, result)
    board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")
    board.post(
        "Floor Trader",
        f"{signal['account']} {signal['instrument']} {plan['action']}: {result.get('reason')}",
        kind="report",
        channel="Floor Trader",
    )
    return {"ok": True, "sent": bool(result.get("sent")), "action": plan["action"], "reason": result.get("reason"), "bars": len(bars)}


learning_pause = threading.Event()


def meeting_note() -> str:
    """The desk clock and every agent use this schedule."""
    return (
        "Meetings are 8:00am ET Monday through Friday, ahead of the 9:30 open, "
        "and 5:00pm ET Sunday through Friday, while the futures market is closed. "
        "Saturday has no meeting."
    )


def meeting_slot(now: datetime | None = None) -> str | None:
    """Return the meeting name when the clock is inside its two-minute window."""
    if now is None:
        et = datetime.now(ET)
    elif now.tzinfo is None:
        et = now.replace(tzinfo=ET)
    else:
        et = now.astimezone(ET)
    if et.weekday() == 5:
        return None
    if et.hour == 8 and et.minute < 2 and et.weekday() != 6:
        return "8:00am"
    if et.hour == 17 and et.minute < 2:
        return "5:00pm"
    return None


def strategy_meeting(board: Board, when: str) -> None:
    """The vote is at 8:00am or 5:00pm. Research before that does not open a meeting."""
    if when not in {"8:00am", "5:00pm"}:
        board.post(
            "Portfolio Manager",
            f"{meeting_note()} Research continues until three rules pass risk.",
            kind="report",
            channel="Portfolio Manager",
        )
        return
    from futuresfund.discuss import hold_meeting

    hold_meeting(board, when)


def _desk_position(strategy: dict, signal: dict) -> tuple[int, float | None]:
    """The position this desk is managing for this account. The alert's own buy or sell text is not the position."""
    slot = _slot_for(strategy, signal)
    contracts = slot.get("contracts")
    if contracts not in (None, 0, 0.0):
        entry = slot.get("entry")
        return int(contracts), (float(entry) if entry is not None else None)
    held = int(_held(strategy, signal) or 0)
    if held == 0 and signal.get("position") not in (None, 0, 0.0):
        held = int(signal.get("position") or 0)
    key = f"{signal['account']}|{signal['instrument']}"
    position = (load().get("positions") or {}).get(key) or {}
    entry = position.get("average_price")
    return held, (float(entry) if entry is not None else None)


def _slot_for(strategy: dict, signal: dict) -> dict:
    account = str(signal.get("account") or strategy.get("account") or "")
    booked = strategy.get("positions_working") or {}
    if isinstance(booked, dict):
        slot = booked.get(account)
        if isinstance(slot, dict):
            return slot
    working = strategy.get("working") or {}
    if isinstance(working, dict) and "contracts" in working:
        return working
    return {}


def _remember(strategy: dict, signal: dict, contracts: int, entry: float | None, stopped_bucket: str | None) -> None:
    account = str((signal or {}).get("account") or strategy.get("account") or "")
    booked = strategy.get("positions_working")
    if not isinstance(booked, dict):
        booked = {}
    else:
        booked = dict(booked)
    if contracts:
        booked[account] = {"contracts": int(contracts), "entry": entry}
        strategy["stopped_bucket"] = None
        strategy["working"] = {"contracts": int(contracts), "entry": entry}
    else:
        booked.pop(account, None)
        strategy["working"] = None
        if stopped_bucket is not None:
            strategy["stopped_bucket"] = stopped_bucket
    strategy["positions_working"] = booked or None


def _flatten_now(board: Board, strategy: dict, signal: dict, held: int, note: str, speaker: str) -> dict:
    store_strategy(strategy)
    plan = trade_plan(0, held, int(strategy.get("qty") or 1))
    signal = {**signal, "qty": plan["qty"] or signal["qty"], "flatten_first": False, "yahoo": yahoo_symbol(signal["instrument"])}
    result = _execute(signal, plan, float(strategy.get("account_size") or 50000))
    _attach_confirmation(board, signal, result, closing=True)
    result["reason"] = f"{note} {result.get('reason') or ''}".strip()
    book = load()
    if result.get("sent") or result.get("dry_run"):
        apply_target(book, signal, 0, None)
    result["managed"] = True
    record_order(book, signal, "close", result)
    record_interval(book, _interval_row(signal, held, "close", result, plan))
    _report_trade(board, strategy, signal, {
        "c": signal.get("price"),
        "h": signal.get("high"),
        "l": signal.get("low"),
        "v": signal.get("volume"),
    })
    board.post(speaker, note, kind="report", channel=speaker)
    _show_webhook(board, signal, result)
    board.post("Floor Trader", f"Flatten {signal['account']} {signal['instrument']}: {result.get('reason')}", kind="report", channel="Floor Trader")
    return {"ok": True, "sent": bool(result.get("sent")), "action": "close", "reason": result.get("reason")}


def _show_webhook(board: Board, signal: dict, result: dict) -> None:
    """The floor trader posts the CrossTrade message before anyone treats it as sent."""
    preview = (result.get("preview") or "").strip()
    if not preview:
        return
    side = result.get("side") or signal.get("hinted_action") or "order"
    note = f"CrossTrade webhook for {side} {signal.get('qty')} {signal.get('instrument')}:\n{preview}"
    board.log("Floor Trader", f"CrossTrade webhook for {side} {signal.get('instrument')}")
    board.post("Floor Trader", note, kind="report", channel="Floor Trader")


def _report_trade(board: Board, strategy: dict, signal: dict, bar: dict) -> None:
    """The trading analyst updates the account and the open trade. A flat book still gets the account."""
    from futuresfund.book import paper_status
    from futuresfund.prop_rules import rules_sentence

    paper = paper_status()
    price = bar.get("c") if bar.get("c") is not None else signal.get("price")
    point = point_value(signal.get("instrument") or "")
    stored = load().get("strategy")
    source = stored if isinstance(stored, dict) else (strategy or {})
    lines = [
        rules_sentence(),
        f"Equity ${paper['equity']:,.2f}. Active P&L ${paper['active_pnl']:,.2f}. Drawdown ${paper['drawdown']:,.2f}.",
    ]
    trades = _open_positions(source, signal)
    if not trades:
        lines.append("No trade is open. The floor trader is waiting for the strategy's buy or sell.")
    for account, instrument, contracts, entry in trades:
        pnl = None
        if entry is not None and price is not None:
            pnl = (float(price) - float(entry)) * contracts * point
        side = "long" if contracts > 0 else "short"
        money = "not available" if pnl is None else f"${pnl:,.2f}"
        stop = signal.get("stop_loss") or signal.get("stop_price")
        stop_line = f" Strategy stop {stop}." if stop else " The stop is in the Pine strategy."
        lines.append(
            f"{account} {instrument} is {side} {abs(contracts)} from {entry}. "
            f"Last {price}, high {bar.get('h')}, low {bar.get('l')}, volume {bar.get('v')}. "
            f"Open P&L {money}.{stop_line}"
        )
    note = " ".join(lines)
    board.log("Trading Analyst", note)
    board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")


def _open_positions(strategy: dict, signal: dict) -> list[tuple]:
    found = []
    for position in (load().get("positions") or {}).values():
        try:
            contracts = int(position.get("contracts") or 0)
        except (TypeError, ValueError):
            continue
        if contracts == 0:
            continue
        found.append((
            position.get("account") or signal.get("account"),
            position.get("instrument") or signal.get("instrument"),
            contracts,
            position.get("average_price"),
        ))
    if found:
        return found
    for account, slot in _open_slots(strategy):
        try:
            contracts = int(slot.get("contracts") or 0)
        except (TypeError, ValueError):
            continue
        if contracts == 0:
            continue
        found.append((account, signal.get("instrument"), contracts, slot.get("entry")))
    return found


def _open_slots(strategy: dict) -> list[tuple[str, dict]]:
    booked = strategy.get("positions_working") or {}
    if isinstance(booked, dict) and booked and "contracts" not in booked:
        return [(str(account), slot) for account, slot in booked.items() if isinstance(slot, dict)]
    working = strategy.get("working") or {}
    if isinstance(working, dict) and working.get("contracts"):
        return [(str(strategy.get("account") or ""), working)]
    return []


def _execute(signal: dict, plan: dict, account_size: float) -> dict:
    return deliver(signal, plan, account_size)


def _attach_confirmation(board: Board, signal: dict, result: dict, *, closing: bool) -> None:
    confirmation = request_confirmation(board, signal, result, closing=closing)
    result["confirmation"] = confirmation.get("status")
    if confirmation.get("reason"):
        result["reason"] = f"{result.get('reason') or ''} {confirmation['reason']}".strip()


def _interval_row(signal: dict, held, action: str, result: dict, plan: dict | None = None) -> dict:
    return {
        "time": _now(),
        "price": signal.get("price"),
        "account": signal["account"],
        "instrument": signal["instrument"],
        "held": held,
        "target": None if plan is None else plan.get("target"),
        "action": action,
        "side": None if plan is None else plan.get("side"),
        "sent": bool(result.get("sent")),
        "reason": result.get("reason") or "",
    }


def _held(strategy: dict, signal: dict) -> float:
    key = f"{signal['account']}|{signal['instrument']}"
    position = (load().get("positions") or {}).get(key) or {}
    if position.get("contracts") is not None:
        return float(position["contracts"])
    return 0.0


def scheduler(board: Board) -> None:
    while True:
        now = datetime.now(ET)
        slot = meeting_slot(now)
        if slot:
            book = load()
            today = now.strftime("%Y-%m-%d")
            key = "last_morning" if slot == "8:00am" else "last_evening"
            if not str(book.get(key, "")).startswith(today):
                book["previous_meeting"] = book.get(key) or ""
                book[key] = f"{today} {slot}"
                save(book)
                learning_pause.set()
                try:
                    strategy_meeting(board, slot)
                finally:
                    learning_pause.clear()
        time.sleep(20)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
