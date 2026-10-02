"""Candles for the desk chart. Prices come from the exported market file, not from a guess."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from futuresfund.contracts import root_of
from futuresfund.timeframe import bar_minutes, parse_timeframe

_EASTERN = ZoneInfo("America/New_York")
_FILED = {"ES", "QO", "NQ"}


def chart_payload(contract: str, timeframe: str, limit: int = 160) -> dict:
    """Recent candles and the orders that landed on them."""
    frame = parse_timeframe(timeframe)
    root = _root(contract)
    candles = _candles(root, frame)
    shown = candles[-max(20, min(int(limit or 160), 2000)):]
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
        "indicators": _indicators(shown),
        "note": "" if shown else f"No market file is loaded for {contract}. ES is read from chartData/es and gold from chartData/qo.",
    }


def _indicators(candles: list[dict]) -> list[str]:
    found = []
    if any(candle.get("v") not in (None, 0, 0.0) for candle in candles):
        found.append("volume")
    if any(candle.get("macd") is not None for candle in candles):
        found.append("macd")
    if any(candle.get("delta") is not None for candle in candles):
        found.append("delta")
    return found


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
    from futuresfund.chart_feed import _epoch_ms, fold_bars, live_bars

    history = _history(root, frame)
    raw = [bar for bar in live_bars(root) if bar.get("epoch")]
    live = fold_bars(raw, frame)
    if not live:
        return history
    first_raw = min(bar["epoch"] for bar in raw)
    merged = {}
    for candle in history:
        epoch = _epoch_ms(candle["t"])
        if epoch is not None:
            merged[epoch] = candle
    for candle in live:
        epoch = _epoch_ms(candle["t"])
        if epoch is None:
            continue
        previous = merged.get(epoch)
        if previous is None:
            merged[epoch] = candle
            continue
        previous["h"] = max(previous["h"], candle["h"])
        previous["l"] = min(previous["l"], candle["l"])
        previous["c"] = candle["c"]
        if first_raw > epoch:
            previous["v"] = float(previous.get("v") or 0) + float(candle.get("v") or 0)
        else:
            previous["v"] = max(float(previous.get("v") or 0), float(candle.get("v") or 0))
        previous["forming"] = candle.get("forming")
        for key in ("macd", "macd_signal", "macd_histogram", "delta", "delta_pct", "delta_high", "delta_low", "poc", "poc_volume"):
            if candle.get(key) is not None:
                previous[key] = candle[key]
    return [merged[key] for key in sorted(k for k in merged if k is not None)]


_HISTORY: dict = {}


def _history(root: str, frame: str) -> list[dict]:
    if root not in _FILED:
        return []
    from futuresfund.charts import chart_paths, load_chart
    from futuresfund.chart_feed import fold_bars
    from futuresfund.timeframe import bar_minutes

    stamp = tuple(path.stat().st_mtime_ns for path in chart_paths(root).values())
    key = (root, frame, stamp)
    cached = _HISTORY.get(key)
    if cached is None:
        one_minute = load_chart("1m", root)
        if one_minute:
            cached = [_candle_from_bar(bar) for bar in one_minute]
            if frame != "1m":
                cached = fold_bars(cached, frame)
                for candle in cached:
                    candle["forming"] = False
        else:
            native = load_chart(frame, root)
            if native:
                cached = [_candle_from_bar(bar) for bar in native]
            else:
                minutes = bar_minutes(frame)
                cached = []
                for source in ("2m", "5m", "15m"):
                    if bar_minutes(source) >= minutes:
                        continue
                    finer = load_chart(source, root)
                    if finer:
                        cached = fold_bars([_candle_from_bar(bar) for bar in finer], frame)
                        for candle in cached:
                            candle["forming"] = False
                        break
        _HISTORY.clear()
        _HISTORY[key] = cached
    return [dict(candle) for candle in cached]


def _candle_from_bar(bar: dict) -> dict:
    candle = {
        "t": bar["t"],
        "et": _eastern(bar["t"]),
        "o": bar["o"],
        "h": bar["h"],
        "l": bar["l"],
        "c": bar["c"],
        "v": bar.get("v") or 0,
        "forming": False,
    }
    for key in ("macd", "macd_signal", "macd_histogram", "delta", "delta_pct", "delta_high", "delta_low", "poc", "poc_volume"):
        if bar.get(key) is not None:
            candle[key] = bar[key]
    return candle


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
