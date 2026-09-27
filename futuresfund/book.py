"""Prop-account book. Positions change when CrossTrade accepts an order, not when a desk is still thinking."""

from __future__ import annotations

import json
from datetime import datetime

from futuresfund.config import BOOK_PATH
from futuresfund.contracts import POINT_VALUE, root_of


def load() -> dict:
    if not BOOK_PATH.is_file():
        return _fresh()
    try:
        data = json.loads(BOOK_PATH.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return _fresh()
    if not isinstance(data, dict):
        return _fresh()
    data.setdefault("positions", {})
    data.setdefault("orders", [])
    data.setdefault("chat", [])
    data.setdefault("strategy", None)
    data.setdefault("intervals", [])
    return data


def save(book: dict) -> None:
    BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOOK_PATH.write_text(json.dumps(book, indent=2), encoding="utf-8")


def record_fill(book: dict, signal: dict, side: str, price: float | None) -> dict:
    key = _key(signal["account"], signal["instrument"])
    current = book["positions"].get(key) or {}
    held = signal.get("position")
    if held is None:
        held = float(current.get("contracts") or 0)
    qty = int(signal["qty"])
    if _flatten(signal, side, held):
        contracts = qty if side == "BUY" else -qty
    else:
        contracts = held + (qty if side == "BUY" else -qty)
    contracts = int(contracts) if float(contracts).is_integer() else contracts
    mark = price if price is not None else signal.get("price")
    position = {
        "account": signal["account"],
        "instrument": signal["instrument"],
        "yahoo": signal["yahoo"],
        "contracts": contracts,
        "average_price": mark if contracts else None,
        "last_price": mark,
        "updated": _now(),
    }
    if contracts == 0:
        book["positions"].pop(key, None)
    else:
        book["positions"][key] = position
    return position


def record_interval(book: dict, row: dict) -> dict:
    book.setdefault("intervals", []).append(row)
    book["intervals"] = book["intervals"][-500:]
    save(book)
    return row


def apply_target(book: dict, signal: dict, target: int, price: float | None) -> None:
    key = f"{signal['account']}|{signal['instrument']}"
    if target == 0:
        book["positions"].pop(key, None)
        return
    book["positions"][key] = {
        "account": signal["account"],
        "instrument": signal["instrument"],
        "yahoo": signal.get("yahoo") or "",
        "contracts": target,
        "average_price": price,
        "last_price": price,
        "updated": _now(),
    }


def record_order(book: dict, signal: dict, rating: str, result: dict) -> dict:
    order = {
        "time": _now(),
        "account": signal["account"],
        "instrument": signal["instrument"],
        "qty": signal["qty"],
        "rating": rating,
        "side": result.get("side"),
        "sent": bool(result.get("sent")),
        "dry_run": bool(result.get("dry_run")),
        "reason": result.get("reason") or "",
    }
    book["orders"].append(order)
    book["orders"] = book["orders"][-40:]
    if result.get("sent") and result.get("side") and not result.get("managed"):
        record_fill(book, signal, result["side"], signal.get("price"))
    save(book)
    return order


def snapshot() -> dict:
    book = load()
    positions = []
    for position in book["positions"].values():
        row = dict(position)
        row["pnl"] = _pnl(row)
        positions.append(row)
    marks = _marks(positions)
    for row in positions:
        live = marks.get(row["yahoo"])
        if live is not None:
            row["last_price"] = live
            row["pnl"] = _pnl(row)
    from futuresfund.strategy import lead_script

    return {
        "positions": positions,
        "orders": list(reversed(book["orders"][-20:])),
        "strategy": _public_strategy(book.get("strategy")),
        "lead": lead_script(),
        "intervals": list(reversed(book.get("intervals", [])[-15:])),
        "interval_count": len(book.get("intervals") or []),
        "next_meeting": "The vote is at 8:00am. Research continues until three rules pass risk.",
    }


def _public_strategy(strategy):
    if not isinstance(strategy, dict):
        return strategy
    shown = dict(strategy)
    shown["strategies"] = [
        {key: value for key, value in item.items() if key != "pine"}
        for item in strategy.get("strategies") or []
    ]
    return shown


def _marks(positions: list[dict]) -> dict:
    symbols = sorted({row["yahoo"] for row in positions if row.get("yahoo")})
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}
    marks = {}
    for symbol in symbols:
        try:
            history = yf.Ticker(symbol).history(period="1d")
            if history is not None and not history.empty:
                marks[symbol] = float(history["Close"].iloc[-1])
        except Exception:
            continue
    return marks


def _pnl(row: dict):
    avg = row.get("average_price")
    last = row.get("last_price")
    contracts = row.get("contracts") or 0
    if avg is None or last is None or not contracts:
        return None
    try:
        value = POINT_VALUE[root_of(row["instrument"])]
    except (ValueError, KeyError):
        return None
    return round((float(last) - float(avg)) * float(contracts) * value, 2)


def _flatten(signal: dict, side: str, held: float) -> bool:
    if signal.get("flatten_first") is True:
        return True
    if signal.get("flatten_first") is False:
        return False
    if not held:
        return False
    return (held > 0 and side == "SELL") or (held < 0 and side == "BUY")


def _key(account: str, instrument: str) -> str:
    return f"{account}|{instrument}"


def _fresh() -> dict:
    return {"positions": {}, "orders": [], "chat": [], "strategy": None, "intervals": [], "last_morning": "", "last_afternoon": ""}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
