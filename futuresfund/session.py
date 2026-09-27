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
from futuresfund.research import (
    active_rules,
    follow_rules,
    stop_hit,
    tight_stop,
    trade_plan,
)
from futuresfund.trade_confirmation import request_confirmation
from futuresfund.trade_manager import deliver, liquidation_reason, live_unrealized, point_value, reconcile_position
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
    dollars = tight_stop(active_rules(strategy))
    bucket = bucket_open(bar["t"], frame)
    updates = load().get("prop_updates") or {}
    account_size = float(strategy.get("account_size") or 50000)

    if trading_halt() and int(held or 0) != 0:
        _remember(strategy, 0, None, None)
        note = f"Floor trader, close this trade. The market halt can liquidate the account. {HALT}"
        return _flatten_now(board, strategy, signal, held, note, "Trading Analyst")

    loss_note = liquidation_reason(
        account_size,
        held,
        entry,
        signal.get("price"),
        point,
        live_unrealized(signal),
        updates.get("daily_loss_limit"),
        updates.get("max_drawdown"),
    )
    if loss_note:
        _remember(strategy, 0, None, bucket)
        return _flatten_now(board, strategy, signal, held, f"Floor trader, close this trade. {loss_note}", "Trading Analyst")

    if _stop_due(held, entry, bar, dollars, point):
        _remember(strategy, 0, None, bucket)
        note = (
            f"{alert_frame} price {signal.get('price')} is through the ${dollars:,.0f} stop "
            f"from {entry}. The {frame} candle is still the strategy candle."
        )
        return _flatten_now(board, strategy, signal, held, note, "Trading Analyst")

    if not closed:
        store_strategy(strategy)
        note = (
            f"{alert_frame} price {signal.get('price')} is inside the open {frame} candle. "
            "The strategy waits for that candle to close."
        )
        if dollars and int(held or 0) != 0:
            note += f" The ${dollars:,.0f} stop is still watching this price."
        record_interval(load(), _interval_row(signal, held, "watch", {"sent": False, "reason": note}))
        board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")
        return {"ok": True, "sent": False, "action": "watch", "reason": note, "bars": len(bars)}

    if trading_halt():
        store_strategy(strategy)
        record_interval(load(), _interval_row(signal, held, "bar", {"sent": False, "reason": HALT}))
        board.post("Compliance & Operations", f"The {frame} candle is stored. {HALT}", kind="report", channel="Compliance & Operations")
        return {"ok": True, "sent": False, "action": "bar", "reason": HALT, "bars": len(bars)}

    armed = active_rules(strategy)
    if not armed:
        store_strategy(strategy)
        note = f"The {frame} candle {closed[-1]['t']} is stored. No strategy is live, so no order was sent."
        record_interval(load(), _interval_row(signal, held, "bar", {"sent": False, "reason": note}))
        board.post("Floor Trader", note, kind="report", channel="Floor Trader")
        return {"ok": True, "sent": False, "action": "bar", "reason": note, "bars": len(bars)}

    if strategy.get("stopped_bucket") == closed[-1]["t"]:
        store_strategy(strategy)
        note = f"The ${dollars:,.0f} stop already flattened the open {frame} candle. No new entry until the next one closes."
        record_interval(load(), _interval_row(signal, held, "watch", {"sent": False, "reason": note}))
        board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")
        return {"ok": True, "sent": False, "action": "watch", "reason": note, "bars": len(bars)}

    agreed, memory = follow_rules(bars, armed, strategy.get("signal_memory"))
    strategy["signal_memory"] = memory
    if agreed is None:
        held_sign = 1 if int(held or 0) > 0 else (-1 if int(held or 0) < 0 else 0)
        plan = trade_plan(held_sign, held, int(strategy.get("qty") or 1))
        plan["action"] = "hold"
    else:
        plan = trade_plan(agreed, held, int(strategy.get("qty") or 1))
    if plan["target"] == 0:
        _remember(strategy, 0, None, None)
    elif plan["action"] == "place" and (int(held or 0) == 0 or (int(held) > 0) != (plan["target"] > 0)):
        _remember(strategy, plan["target"], signal.get("price"), None)
    else:
        _remember(strategy, plan["target"], entry, None)
    store_strategy(strategy)
    signal = {**signal, "qty": plan["qty"] or signal["qty"], "flatten_first": plan["flatten_first"], "yahoo": yahoo_symbol(signal["instrument"])}
    result = _execute(signal, plan, account_size)
    if plan["action"] in {"place", "close"}:
        _attach_confirmation(board, signal, result, closing=plan["action"] == "close")
    if agreed is None:
        result["reason"] = "Active strategies disagree, so the position stays."
    note = f"The {frame} candle {closed[-1]['t']} closed at {signal.get('price')}."
    result["reason"] = f"{note} {result.get('reason') or ''}".strip()
    book = load()
    if result.get("sent") or result.get("dry_run"):
        apply_target(book, signal, plan["target"], signal.get("price") if plan["target"] else None)
    result["managed"] = True
    record_order(book, signal, plan["action"], result)
    record_interval(book, _interval_row(signal, held, plan["action"], result, plan))
    _tell_floor(board, signal, frame, agreed, plan)
    board.post(
        "Floor Trader",
        f"{signal['account']} {signal['instrument']} {plan['action']}: {result.get('reason')}",
        kind="report",
        channel="Floor Trader",
    )
    return {"ok": True, "sent": bool(result.get("sent")), "action": plan["action"], "reason": result.get("reason"), "bars": len(bars), "added": bool(closed)}


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
    """The position this desk is managing. The alert's own buy or sell text is not the position."""
    working = strategy.get("working") or {}
    contracts = working.get("contracts")
    if contracts not in (None, 0, 0.0):
        entry = working.get("entry")
        return int(contracts), (float(entry) if entry is not None else None)
    held = int(_held(strategy, signal) or 0)
    if held == 0 and signal.get("position") not in (None, 0, 0.0):
        held = int(signal.get("position") or 0)
    key = f"{signal['account']}|{signal['instrument']}"
    position = (load().get("positions") or {}).get(key) or {}
    entry = position.get("average_price")
    return held, (float(entry) if entry is not None else None)


def _stop_due(held: int, entry: float | None, bar: dict, dollars: float | None, point: float) -> bool:
    if not dollars or entry is None or int(held or 0) == 0:
        return False
    return stop_hit(float(entry), int(held), bar, float(dollars), point)


def _remember(strategy: dict, contracts: int, entry: float | None, stopped_bucket: str | None) -> None:
    if contracts:
        strategy["working"] = {"contracts": int(contracts), "entry": entry}
        strategy["stopped_bucket"] = None
    else:
        strategy["working"] = None
        if stopped_bucket is not None:
            strategy["stopped_bucket"] = stopped_bucket


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
    board.post(speaker, note, kind="report", channel=speaker)
    board.post("Floor Trader", f"Flatten {signal['account']} {signal['instrument']}: {result.get('reason')}", kind="report", channel="Floor Trader")
    return {"ok": True, "sent": bool(result.get("sent")), "action": "close", "reason": result.get("reason")}


def _tell_floor(board: Board, signal: dict, frame: str, agreed: int | None, plan: dict) -> None:
    price = signal.get("price")
    if agreed is None:
        text = f"The {frame} close is {price}. The armed rules disagree. No webhook."
    else:
        side = "long" if agreed == 1 else ("short" if agreed == -1 else "flat")
        if plan["action"] in {"place", "close"}:
            text = f"The {frame} close is {price}. The armed rule is {side}. Floor trader, send the CrossTrade webhook to {plan['action']}."
        else:
            text = f"The {frame} close is {price}. The armed rule is {side}. No webhook. The position stays."
    board.post("Trading Analyst", text, kind="report", channel="Trading Analyst")


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
                book[key] = today
                save(book)
                learning_pause.set()
                try:
                    strategy_meeting(board, slot)
                finally:
                    learning_pause.clear()
        time.sleep(20)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
