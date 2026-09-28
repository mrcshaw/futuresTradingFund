"""Candles for the desk chart. Prices come from the exported market file, not from a guess."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from futuresfund.contracts import root_of
from futuresfund.timeframe import bar_minutes, parse_timeframe

_EASTERN = ZoneInfo("America/New_York")
_FILED = {"ES"}


def chart_payload(contract: str, timeframe: str, limit: int = 160) -> dict:
    """Recent candles and the orders that landed on them."""
    frame = parse_timeframe(timeframe)
    root = _root(contract)
    candles = _candles(root, frame)
    shown = candles[-max(20, min(int(limit or 160), 400)):]
    orders = _orders(root, frame, shown)
    last = shown[-1] if shown else None
    return {
        "contract": contract,
        "root": root,
        "timeframe": frame,
        "candles": shown,
        "orders": orders,
        "last": last,
        "count": len(candles),
        "note": "" if shown else f"No market file is loaded for {contract}. The desk chart currently has the ES export.",
    }


def match_candle(signal: dict) -> str:
    """Say which candle a TradingView buy or sell belongs to. Price is not moved to fit."""
    frame = parse_timeframe(signal.get("timeframe") or "5m")
    root = _root(signal.get("instrument") or "ES1!")
    candles = _candles(root, frame)
    stamp = str(signal.get("bar_time") or "")
    candle = _candle_at(candles, stamp, frame) if stamp else None
    if candle is None and candles:
        candle = candles[-1]
        where = "the latest candle, because the alert had no bar time"
    elif candle is None:
        return f"No {frame} candle is loaded for {signal.get('instrument')}."
    else:
        where = f"the {frame} candle {candle['et']} ET"
    price = signal.get("price")
    inside = ""
    if price is not None:
        number = float(price)
        if candle["l"] <= number <= candle["h"]:
            inside = " The order price is inside that candle."
        else:
            inside = " The order price is outside that candle's high and low, so it was not rewritten."
    side = signal.get("hinted_action") or signal.get("side") or "order"
    return (
        f"Matched {side} to {where}: open {candle['o']}, high {candle['h']}, "
        f"low {candle['l']}, close {candle['c']}, volume {candle['v']}.{inside}"
    )


def _candles(root: str, frame: str) -> list[dict]:
    if root not in _FILED:
        return []
    from futuresfund.charts import load_chart

    rows = []
    for bar in load_chart(frame):
        rows.append({
            "t": bar["t"],
            "et": _eastern(bar["t"]),
            "o": bar["o"],
            "h": bar["h"],
            "l": bar["l"],
            "c": bar["c"],
            "v": bar.get("v") or 0,
        })
    return rows


def _orders(root: str, frame: str, candles: list[dict]) -> list[dict]:
    from futuresfund.book import load

    if not candles:
        return []
    opens = {candle["t"][:19]: candle for candle in candles}
    marks = []
    for order in load().get("orders") or []:
        if _root(order.get("instrument") or "") != root:
            continue
        side = str(order.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        stamp = str(order.get("bar_time") or order.get("time") or "")
        candle = _candle_at(candles, stamp, frame)
        if candle is None or candle["t"][:19] not in opens:
            continue
        marks.append({
            "t": candle["t"],
            "et": candle["et"],
            "side": side,
            "price": order.get("price"),
            "account": order.get("account"),
        })
    return marks


def _candle_at(candles: list[dict], stamp: str, frame: str) -> dict | None:
    if not candles or not stamp:
        return None
    text = stamp.replace(" ", "T")
    bucket = text[:19]
    try:
        minutes = bar_minutes(frame)
        parsed = datetime.strptime(bucket, "%Y-%m-%dT%H:%M:%S")
        total = parsed.hour * 60 + parsed.minute
        floored = (total // minutes) * minutes
        hour, minute = divmod(floored, 60)
        bucket = f"{parsed:%Y-%m-%d}T{hour:02d}:{minute:02d}:00"
    except ValueError:
        return None
    for candle in candles:
        if candle["t"][:19] == bucket:
            return candle
    return None


def _eastern(stamp: str) -> str:
    text = str(stamp)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if "+" in text[10:] or text[10:].find("-") > 0:
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return text[:16].replace("T", " ")
    return parsed.astimezone(_EASTERN).strftime("%Y-%m-%d %H:%M")


def _root(contract: str) -> str:
    try:
        return root_of(contract)
    except ValueError:
        return ""
