"""Live candles from TradingView. Ingestion writes them. The chart folds them into any timeframe."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from futuresfund.config import LIVE_BARS_PATH
from futuresfund.contracts import root_of
from futuresfund.timeframe import bar_minutes, parse_timeframe

_EASTERN = ZoneInfo("America/New_York")


def _epoch_ms(value) -> int | None:
    """Chart times are US Eastern when they have no timezone."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        number = int(text)
        return number if abs(number) > 10_000_000_000 else number * 1000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_EASTERN)
    return int(parsed.timestamp() * 1000)


def record_live_bar(signal: dict, bar: dict) -> dict:
    """Keep the alert's own candle. A repeat of the same minute replaces that minute."""
    frame = parse_timeframe(signal.get("timeframe") or "1m")
    instrument = str(signal.get("instrument") or "ES1!")
    stored = {
        "instrument": instrument,
        "root": _root(instrument),
        "timeframe": frame,
        "t": bar["t"],
        "epoch": _epoch_ms(bar["t"]),
        "o": bar["o"],
        "h": bar["h"],
        "l": bar["l"],
        "c": bar["c"],
        "v": bar.get("v") or 0,
        "macd": _first(signal, bar, "macd"),
        "macd_signal": _first(signal, bar, "macd_signal"),
        "macd_histogram": _first(signal, bar, "macd_histogram"),
        "delta": _first(signal, bar, "delta"),
        "delta_pct": _first(signal, bar, "delta_pct"),
        "delta_high": _first(signal, bar, "delta_high"),
        "delta_low": _first(signal, bar, "delta_low"),
        "poc": _first(signal, bar, "poc"),
        "poc_volume": _first(signal, bar, "poc_volume"),
    }
    rows = _load()
    root = place_root(stored["root"], stored["c"], _anchors(rows))
    if root and root != stored["root"]:
        stored["root"] = root
        stored["instrument"] = f"{root}1!"
    key = (stored["root"], stored["timeframe"], stored["epoch"])
    kept = [row for row in rows if (row.get("root"), row.get("timeframe"), row.get("epoch")) != key]
    kept.append(stored)
    kept.sort(key=lambda row: row.get("epoch") or 0)
    _save(kept[-20000:])
    return stored


def live_bars(root: str) -> list[dict]:
    return [row for row in _load() if row.get("root") == root and row.get("epoch")]


def place_root(named: str, price: float | None, anchors: dict[str, float]) -> str:
    """Keep a print on the contract it matches. A mislabeled alert must not freeze the other chart."""
    if not named or price is None:
        return named
    try:
        number = float(price)
    except (TypeError, ValueError):
        return named
    named_close = anchors.get(named)
    if named_close in (None, 0):
        return named
    named_distance = abs(number - named_close) / abs(named_close)
    if named_distance <= 0.08:
        return named
    best = named
    best_distance = named_distance
    for root, close in anchors.items():
        if root == named or close in (None, 0):
            continue
        distance = abs(number - close) / abs(close)
        if distance <= 0.02 and distance < best_distance:
            best = root
            best_distance = distance
    return best


def fold_bars(bars: list[dict], timeframe: str) -> list[dict]:
    """Build display candles from finer bars. The last candle stays open until the next one starts."""
    minutes = bar_minutes(timeframe)
    width = minutes * 60 * 1000
    groups: dict[int, list[dict]] = {}
    for bar in bars:
        epoch = bar.get("epoch")
        if epoch is None:
            epoch = _epoch_ms(bar.get("t"))
        if epoch is None:
            continue
        bucket = epoch - (epoch % width)
        groups.setdefault(bucket, []).append(bar)
    candles = []
    for bucket in sorted(groups):
        chunk = sorted(groups[bucket], key=lambda row: row.get("epoch") or 0)
        first, last = chunk[0], chunk[-1]
        candle = {
            "t": datetime.fromtimestamp(bucket / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "et": datetime.fromtimestamp(bucket / 1000, timezone.utc).astimezone(_EASTERN).strftime("%Y-%m-%d %H:%M"),
            "o": first["o"],
            "h": max(row["h"] for row in chunk),
            "l": min(row["l"] for row in chunk),
            "c": last["c"],
            "v": sum(float(row.get("v") or 0) for row in chunk),
            "forming": False,
        }
        _last_indicator(candle, chunk, "macd")
        _last_indicator(candle, chunk, "macd_signal")
        _last_indicator(candle, chunk, "macd_histogram")
        _last_indicator(candle, chunk, "poc")
        _last_indicator(candle, chunk, "poc_volume")
        _last_indicator(candle, chunk, "delta_pct")
        deltas = [row.get("delta") for row in chunk if row.get("delta") is not None]
        if deltas:
            candle["delta"] = sum(float(value) for value in deltas)
        candles.append(candle)
    if candles:
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        last_bucket = _epoch_ms(candles[-1]["t"]) or 0
        candles[-1]["forming"] = last_bucket + width > now
    return candles


def _first(signal: dict, bar: dict, key: str):
    if signal.get(key) is not None:
        return signal.get(key)
    return bar.get(key)


def _last_indicator(candle: dict, chunk: list[dict], key: str) -> None:
    for row in reversed(chunk):
        if row.get(key) is not None:
            candle[key] = row[key]
            return


def _load() -> list[dict]:
    if not LIVE_BARS_PATH.is_file():
        return []
    try:
        data = json.loads(LIVE_BARS_PATH.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    if _refile(data):
        _save(data)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    for row in data:
        epoch = row.get("epoch")
        if not isinstance(epoch, (int, float)) or epoch - now <= 15 * 60 * 1000:
            continue
        text = str(row.get("t") or "")[:19]
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        row["epoch"] = int(parsed.timestamp() * 1000)
        row["t"] = parsed.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return data


def _save(rows: list[dict]) -> None:
    LIVE_BARS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIVE_BARS_PATH.write_text(json.dumps(rows), encoding="utf-8")


def _anchors(rows: list[dict]) -> dict[str, float]:
    found: dict[str, float] = {}
    for root in {row.get("root") for row in rows if row.get("root")}:
        close = _anchor_close(rows, str(root))
        if close is not None:
            found[str(root)] = close
    return found


def _anchor_close(rows: list[dict], root: str) -> float | None:
    closes = []
    for row in rows:
        if row.get("root") != root or row.get("c") in (None, ""):
            continue
        try:
            closes.append(float(row["c"]))
        except (TypeError, ValueError):
            continue
    if not closes:
        return None
    sample = closes[-40:]
    median = sorted(sample)[len(sample) // 2]
    if median == 0:
        return median
    for price in reversed(sample):
        if abs(price - median) / abs(median) <= 0.08:
            return price
    return median


def _refile(rows: list[dict]) -> bool:
    anchors = _anchors(rows)
    changed = False
    for row in rows:
        named = str(row.get("root") or "")
        target = place_root(named, row.get("c"), anchors)
        if target and target != named:
            row["root"] = target
            row["instrument"] = f"{target}1!"
            changed = True
    return changed


def _root(contract: str) -> str:
    try:
        return root_of(contract)
    except ValueError:
        return ""
