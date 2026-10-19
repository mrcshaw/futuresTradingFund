"""One Kronos replay for the chart engines, and the forecast drawn on the live chart.

The replay reads bars that have already closed. It does not open a trade.
A lookback window is stored as a signature. Under that signature, each distinct
six-candle set Kronos returns is counted. Prices are offsets from the window's
last close, and volume is kept with them. Profits, win rates, and fills are not stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOOKBACK = 400
PREDICT_LEN = 6
_EASTERN = ZoneInfo("America/New_York")
_PATH = Path(__file__).resolve().parent / "researchNotes" / "kronos_patterns.json"
_file_lock = threading.Lock()
_live = False
_cache: dict = {}
_failed: dict = {}
_retry_after: dict = {}
_running: set = set()
_cache_lock = threading.Lock()
_replay_thread = None
_replay_lock = threading.Lock()
_warned = False
_shown: dict = {}
_notes_memory: dict = {}
_view = {
    "contract": "",
    "timeframe": "",
    "candles": [],
    "predicted": [],
    "direction": "",
    "count": 0,
    "reached": "",
    "owner": "",
    "sampling": False,
    "loaded": True,
    "seen": False,
    "note": "The five-year map has no row for this window.",
}
_FIVE_START = datetime(2021, 10, 4, tzinfo=_EASTERN)
_FIVE_END = datetime(2026, 10, 4, tzinfo=_EASTERN)
_OWNERS = {
    "2m": "2min chart developer",
    "5m": "5 min chart developer",
    "15m": "15 min chart developer",
}


_warm_started = False


def _warm_model() -> None:
    try:
        from futuresfund.kronos_model import warm

        warm()
    except Exception:
        traceback.print_exc()


def enable_live() -> None:
    """The chart request may ask Kronos for the next candles. Tests leave this off.

    Loading the weights stays off this call. The chart returns the candles it already has.
    """
    global _live, _warm_started
    _live = True
    if _warm_started:
        return
    _warm_started = True
    threading.Thread(target=_warm_model, daemon=True, name="kronos-warm").start()


def pattern_counts(contract: str, timeframe: str, path: Path | None = None) -> dict:
    """Recorded matches for this contract and timeframe. An unread chart is an empty table."""
    notes = _load(path or _PATH)
    root = str(contract or "").upper()
    frame = str(timeframe or "")
    found = ((notes.get("patterns") or {}).get(root) or {}).get(frame) or {}
    if not isinstance(found, dict):
        return {}
    counts = {}
    for shape, count in found.items():
        try:
            counts[str(shape)] = int(count)
        except (TypeError, ValueError):
            continue
    return counts


def _cache_key(candles: list[dict], root: str, timeframe: str, path: Path | None) -> tuple:
    """Closed-bar time, plus the open of the forming bar. A tick of that bar does not change the key."""
    closed = [candle for candle in candles if not candle.get("forming")]
    stamp = str(closed[-1].get("t")) if closed else ""
    last = candles[-1] if candles else {}
    forming = str(last.get("t")) if last.get("forming") else ""
    return (str(root).upper(), str(timeframe), stamp, forming, str(path or _PATH))


def prediction_for(candles: list[dict], root: str, timeframe: str, predictor=None, path: Path | None = None) -> dict:
    """The next candles, kept apart from the printed ones. A missing model adds no prices.

    The live candles are matched to the sets saved in the replay. The set that was
    predicted most often for that live set is the one drawn.
    """
    shown = _match_candles(candles)
    closed = [candle for candle in shown if not candle.get("forming")]
    window = closed[-LOOKBACK:]
    sets = stored_sets(root, timeframe, window, path) if window else {}
    if not sets and window:
        sets = sets_for_shape(root, timeframe, window_shape(window), path)
    if sets:
        result = _forecast(shown, root, timeframe, None, sets, path)
        _remember_shown(root, timeframe, result)
        return result
    if predictor is not None:
        result = _forecast(candles, root, timeframe, predictor, sets, path)
        _remember_shown(root, timeframe, result)
        return result
    if _has_winner(sets) or not _live:
        if _has_winner(sets):
            result = _forecast(candles, root, timeframe, None, sets, path)
            _remember_shown(root, timeframe, result)
            return result
        return _quiet(False)
    key = _cache_key(candles, root, timeframe, path)
    with _cache_lock:
        ready = _cache.get(key)
        if ready is not None:
            return ready
        if key in _running:
            return _while_sampling(root, timeframe)
        if time.time() < _retry_after.get(key, 0):
            return _failed.get(key) or _while_sampling(root, timeframe)
        _running.add(key)
        threading.Thread(
            target=_fill,
            args=(key, list(candles), root, timeframe, path),
            daemon=True,
            name="kronos-chart",
        ).start()
    return _while_sampling(root, timeframe)


def replay(bars: list[dict], contract: str, timeframe: str, predictor, *, steps: int | None = None, path: Path | None = None) -> int:
    """After each closed candle, store the six-candle forecast that follows it.

    The walk moves one candle forward. It does not wait for a trade, and it does
    not skip ahead by the six candles being forecast. Returns how many sets were
    counted. The bars that follow the window are not compared, and no fill is stored.
    """
    series = list(bars or [])
    if len(series) < 2:
        return 0
    notes_path = path or _PATH
    index = _resume(series, contract, timeframe, notes_path)
    if index < 1:
        index = 1
    recorded = 0
    advanced = 0
    while index < len(series):
        history = series[max(0, index - LOOKBACK):index]
        printed = series[index:index + PREDICT_LEN]
        if history and len(printed) >= PREDICT_LEN:
            paths = [item for item in _ask(predictor, history, printed) if _usable(item)]
            recorded += _store_sets(history, paths, contract, timeframe, notes_path)
            _publish_window(contract, timeframe, history, paths, notes_path)
        following = index + 1
        cursor = series[following]["t"] if following < len(series) else _after(series[-1].get("t"))
        _place_cursor(contract, timeframe, cursor, notes_path)
        _view["reached"] = _reached_label(cursor)
        _view["owner"] = _OWNERS.get(str(timeframe), "")
        index = following
        advanced += 1
        if steps is not None and advanced >= steps:
            break
    return recorded


def shape_of(path: list[dict]) -> str:
    """Up or down, then small or large beside that candle's own high-low range."""
    return ",".join(body_mark(bar) for bar in path)


def body_mark(bar: dict) -> str:
    open_ = float(bar["o"])
    high = float(bar["h"])
    low = float(bar["l"])
    close = float(bar["c"])
    direction = "up" if close >= open_ else "down"
    span = high - low
    body = abs(close - open_)
    size = "small" if span <= 0 or body * 2 <= span else "large"
    return f"{direction}-{size}"


def stored_sets(contract: str, timeframe: str, window: list[dict], path: Path | None = None) -> dict:
    """The predicted sets stored for this lookback window. An unseen window is an empty map."""
    if not window:
        return {}
    root = str(contract or "").upper()
    frame = str(timeframe or "")
    ident = window_id(window, _tick(root))
    notes = _load(path or _PATH)
    found = ((notes.get("windows") or {}).get(root) or {}).get(frame) or {}
    bucket = found.get(ident) if isinstance(found, dict) else None
    if not isinstance(bucket, dict):
        return {}
    sets = {}
    for signature, row in bucket.items():
        if not isinstance(row, dict):
            continue
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            continue
        candles = row.get("candles")
        if count < 1 or not isinstance(candles, list):
            continue
        sets[str(signature)] = {"count": count, "candles": candles}
    return sets


def window_shape(bars: list[dict]) -> str:
    """The live set: the last six candles, up or down and small or large."""
    tail = list(bars or [])[-PREDICT_LEN:]
    if len(tail) < PREDICT_LEN:
        return ""
    return shape_of(tail)


def sets_for_shape(contract: str, timeframe: str, shape: str, path: Path | None = None) -> dict:
    """Saved forecasts for this live candle set. The same set keeps its highest count.

    An exact six-candle shape wins. A shorter tail is used only when the full set was never saved.
    """
    if not shape:
        return {}
    root = str(contract or "").upper()
    frame = str(timeframe or "")
    notes = _load(path or _PATH)
    table = ((notes.get("shapes") or {}).get(root) or {}).get(frame) or {}
    if not isinstance(table, dict) or not table:
        return {}
    marks = [mark for mark in str(shape).split(",") if mark]
    for length in range(len(marks), 3, -1):
        suffix = ",".join(marks[-length:])
        idents = []
        for key, listing in table.items():
            if key == suffix or (length < len(marks) and str(key).endswith(suffix)):
                idents.extend(listing if isinstance(listing, list) else [])
        merged = _merge_sets(root, frame, idents, notes)
        if merged:
            return merged
    return {}


def window_signature(bars: list[dict], tick: float) -> str:
    """Up or down, small or large, and the tick offset of open, high, low, close, plus volume."""
    anchor = float(bars[-1]["c"])
    return "|".join(_token(bar, anchor, tick) for bar in bars)


def window_id(bars: list[dict], tick: float) -> str:
    return hashlib.sha256(window_signature(bars, tick).encode("utf-8")).hexdigest()


def direction_of(anchor_close: float, anchor_volume: float, predicted: list[dict]) -> str:
    """Above on price and volume is Buy. Below is Sell. A split or an unchanged close is Flat."""
    if not predicted:
        return "Flat"
    end = float(predicted[-1]["c"])
    price = 0 if abs(end - float(anchor_close)) < 1e-6 else (1 if end > float(anchor_close) else -1)
    average = sum(float(bar.get("v") or 0) for bar in predicted) / len(predicted)
    base = float(anchor_volume or 0)
    volume = 0 if abs(average - base) < 1e-6 else (1 if average > base else -1)
    if price == 0 or (volume != 0 and price != volume):
        return "Flat"
    return "Buy" if price > 0 else "Sell"


def kronos_report() -> dict:
    """What the R&D tab shows. Counts come from the stored map. No profit is added."""
    with _cache_lock:
        return {
            "contract": _view.get("contract") or "",
            "timeframe": _view.get("timeframe") or "",
            "candles": [dict(row) for row in _view.get("candles") or []],
            "predicted": [dict(row) for row in _view.get("predicted") or []],
            "direction": _view.get("direction") or "",
            "count": int(_view.get("count") or 0),
            "reached": _view.get("reached") or "",
            "owner": _view.get("owner") or "",
            "sampling": bool(_view.get("sampling")),
            "loaded": bool(_view.get("loaded", True)),
            "seen": bool(_view.get("seen")),
            "note": _view.get("note") or "",
        }


def _five_year(bars: list[dict]) -> list[dict]:
    """The download from 2021-10-04 through 2026-10-03. Strategy search does not use this cut."""
    start = _FIVE_START.timestamp()
    end = _FIVE_END.timestamp()
    kept = []
    for bar in bars or []:
        try:
            epoch = float(bar.get("t"))
        except (TypeError, ValueError):
            continue
        if start <= epoch < end:
            kept.append(bar)
    return kept


def choose_path(paths: list[list[dict]], counts: dict) -> tuple[list[dict], int, bool, str]:
    """The stored shape with the most matches. An empty table uses the shape the samples share."""
    shapes = [shape_of(path) for path in paths]
    scored = bool(counts)

    def rank(index: int) -> tuple:
        shape = shapes[index]
        weight = int(counts.get(shape) or 0) if scored else 0
        return (weight, shapes.count(shape))

    chosen = max(range(len(paths)), key=rank)
    shape = shapes[chosen]
    return paths[chosen], shapes.count(shape), scored, shape


def _merge_sets(root: str, timeframe: str, idents: list, notes: dict) -> dict:
    windows = ((notes.get("windows") or {}).get(root) or {}).get(timeframe) or {}
    merged = {}
    seen = set()
    for ident in idents:
        if ident in seen or not isinstance(windows.get(ident), dict):
            continue
        seen.add(ident)
        for signature, row in windows[ident].items():
            if not isinstance(row, dict) or not isinstance(row.get("candles"), list):
                continue
            try:
                count = int(row.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if count < 1:
                continue
            slot = merged.get(str(signature))
            if slot is None:
                merged[str(signature)] = {"count": count, "candles": row["candles"]}
            else:
                slot["count"] = int(slot["count"]) + count
    return merged


def _match_candles(candles: list[dict]) -> list[dict]:
    """Through the Friday 16:44 bar when the chart is in the halt. Later session bars stay."""
    closed = [candle for candle in candles or [] if not candle.get("forming")]
    if not closed or not _in_halt(closed[-1]):
        return list(candles or [])
    anchor = _session_anchor(closed)
    if anchor is None:
        return list(candles or [])
    kept = []
    for candle in closed:
        kept.append(candle)
        if str(candle.get("t")) == str(anchor.get("t")):
            break
    return kept


def _session_anchor(closed: list[dict]) -> dict | None:
    """The last bar before 16:45 New York. On a 5-minute chart that bar contains 16:44."""
    for candle in reversed(closed):
        if not _in_halt(candle):
            return candle
    return closed[-1] if closed else None


def _in_halt(candle: dict) -> bool:
    from futuresfund.live_trading import halt_window

    epoch = _epoch_seconds(candle.get("t")) if candle else 0
    return bool(epoch) and halt_window(epoch)


def _place_on_chart(times: list, candles: list[dict], timeframe: str) -> list:
    """Draw the forecast on the bars after Friday 16:44 instead of the Sunday reopen."""
    closed = [candle for candle in candles or [] if not candle.get("forming")]
    if not closed or len(times) < PREDICT_LEN:
        return times
    from futuresfund.timeframe import bar_minutes

    width = bar_minutes(timeframe) * 60
    anchor = _session_anchor(closed) or closed[-1]
    start = _stamp_seconds(anchor.get("t")) + width
    first = _stamp_seconds(times[0])
    if start <= 0 or first - start <= width:
        return list(times)[:PREDICT_LEN]
    return [start + index * width for index in range(PREDICT_LEN)]


def index_saved_shapes(path: Path | None = None) -> int:
    """Attach each saved window to the six-candle set that produced it.

    The replay can then match a live set without walking the price path again.
    """
    from futuresfund.research_bars import research_series

    notes_path = path or _PATH
    notes = _load(notes_path)
    windows = notes.get("windows") or {}
    linked = 0
    for root, frames in windows.items():
        if not isinstance(frames, dict):
            continue
        tick = _tick(str(root))
        for frame, buckets in frames.items():
            if not isinstance(buckets, dict) or not buckets:
                continue
            wanted = set(buckets)
            found: dict[str, str] = {}
            try:
                bars = _five_year(research_series(str(root), str(frame), include_live=False))
            except Exception:
                continue
            cursor = _cursor(root, frame, notes_path)
            for index in range(1, len(bars)):
                try:
                    if cursor is not None and float(bars[index - 1].get("t")) > float(cursor) + 1:
                        break
                except (TypeError, ValueError):
                    continue
                history = bars[max(0, index - LOOKBACK):index]
                if len(history) < PREDICT_LEN:
                    continue
                ident = window_id(history, tick)
                if ident not in wanted or ident in found:
                    continue
                shape = window_shape(history)
                if shape:
                    found[ident] = shape
                if len(found) == len(wanted):
                    break
            listing = notes.setdefault("shapes", {}).setdefault(str(root), {}).setdefault(str(frame), {})
            for ident, shape in found.items():
                row = listing.setdefault(shape, [])
                if ident not in row:
                    row.append(ident)
                    linked += 1
    if linked:
        _save(notes_path, notes)
    return linked


def next_open_times(after, timeframe: str, count: int) -> list[float]:
    """Bar opens after this one, skipping the 16:45-18:00 halt and the weekend halt."""
    from futuresfund.chart_feed import _epoch_ms
    from futuresfund.live_trading import halt_clears, halt_window
    from futuresfund.timeframe import bar_minutes

    anchor = _epoch_ms(after)
    if anchor is None or count < 1:
        return []
    width = bar_minutes(timeframe) * 60 * 1000
    cursor = anchor + width
    found = []
    guard = 0
    while len(found) < count and guard < 5000:
        guard += 1
        if halt_window(cursor / 1000):
            reopen = int(halt_clears(cursor / 1000) * 1000)
            cursor = reopen if reopen > cursor else cursor + width
            continue
        found.append(cursor / 1000)
        cursor += width
    return found


def start_research_replay(board) -> None:
    """One replay for the 2-minute, 5-minute, and 15-minute engines. It is not a strategy trial."""
    global _replay_thread
    with _replay_lock:
        if _replay_thread is not None and _replay_thread.is_alive():
            return
        _replay_thread = threading.Thread(target=_research_loop, args=(board,), daemon=True, name="kronos-replay")
        _replay_thread.start()


def _forecast(candles, root, timeframe, predictor, sets, path: Path | None = None) -> dict:
    if not candles:
        return _quiet(False)
    forming = bool(candles[-1].get("forming"))
    closed = [candle for candle in candles if not candle.get("forming")]
    if not closed:
        return _quiet(False)
    window = closed[-LOOKBACK:]
    horizon = PREDICT_LEN + (1 if forming else 0)
    if forming:
        times = [candles[-1].get("t")] + next_open_times(candles[-1].get("t"), timeframe, PREDICT_LEN)
    else:
        times = next_open_times(candles[-1].get("t"), timeframe, PREDICT_LEN)
    samples = []
    if predictor is not None and (not _has_winner(sets)):
        printed = [{"t": stamp} for stamp in times]
        paths = _ask(predictor, window, printed, horizon)
        if forming:
            paths = [item[1:1 + PREDICT_LEN] for item in paths if len(item) > PREDICT_LEN]
            times = times[1:1 + PREDICT_LEN]
        else:
            paths = [item[:PREDICT_LEN] for item in paths if len(item) >= PREDICT_LEN]
            times = times[:PREDICT_LEN]
        samples = [item for item in paths if _usable(item)]
    elif forming:
        times = times[1:1 + PREDICT_LEN]
    else:
        times = times[:PREDICT_LEN]
    if len(times) < PREDICT_LEN:
        return _quiet(bool(sets))
    chosen, agreed, seen, count = _choose_set(window, sets, samples, _tick(root))
    if not chosen:
        result = _quiet(bool(sets))
        if not sets:
            result["note"] = _note(root, timeframe, "", 0, 0, False, 0)
        _publish_view(root, timeframe, window, [], "", 0, bool(sets), result["note"], sampling=False)
        return result
    anchor = float(window[-1]["c"])
    direction = direction_of(anchor, float(window[-1].get("v") or 0), chosen)
    times = _place_on_chart(times[:PREDICT_LEN], candles, timeframe)
    drawn = _candles(chosen[:PREDICT_LEN], times[:PREDICT_LEN], direction)
    note = _note(root, timeframe, direction, agreed, len(samples), seen, count)
    result = {
        "candles": drawn,
        "agreed": agreed,
        "paths": len(samples),
        "scored": seen,
        "direction": direction,
        "count": count,
        "anchor": window[-1].get("t"),
        "note": note,
    }
    _publish_view(root, timeframe, window, drawn, direction, count, seen, note, sampling=False)
    return result


def _quiet(seen: bool, waiting: bool = False) -> dict:
    if waiting:
        note = "Kronos is sampling."
    elif seen:
        note = ""
    else:
        note = "The pattern table is empty."
    return {"candles": [], "agreed": 0, "paths": 0, "scored": bool(seen), "direction": "", "count": 0, "note": note}


def _note(root: str, timeframe: str, direction: str, agreed: int, paths: int, seen: bool, count: int) -> str:
    text = f"{direction + '. ' if direction else ''}{root} {timeframe}."
    if not seen:
        text += " This window has not been seen in the five-year map."
    elif count:
        text += f" That predicted set was stored {count} times for this live candle set."
    else:
        text += " The five-year map has no row for this window."
    if paths:
        text += f" {agreed} of {paths} samples agreed."
    text += " The dashed candles are a prediction and not an order."
    return text


def _candles(path: list[dict], times: list, direction: str = "") -> list[dict]:
    candles = []
    for index, (bar, stamp) in enumerate(zip(path, times)):
        epoch = float(stamp) if isinstance(stamp, (int, float)) else _epoch_seconds(stamp)
        candle = {
            "t": datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "et": datetime.fromtimestamp(epoch, timezone.utc).astimezone(_EASTERN).strftime("%Y-%m-%d %H:%M"),
            "o": float(bar["o"]),
            "h": float(bar["h"]),
            "l": float(bar["l"]),
            "c": float(bar["c"]),
            "v": float(bar.get("v") or 0),
            "forming": False,
            "predicted": True,
        }
        if direction and index == 0:
            candle["direction"] = direction
        candles.append(candle)
    return candles


def _stamp_seconds(stamp) -> float:
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        number = float(stamp)
        if number > 10_000_000_000:
            number /= 1000
        return number
    return _epoch_seconds(stamp)


def _epoch_seconds(stamp) -> float:
    from futuresfund.chart_feed import _epoch_ms

    found = _epoch_ms(stamp)
    return (found or 0) / 1000


def _ask(predictor, history, printed, pred_len: int | None = None):
    length = pred_len or len(printed)
    future = [bar.get("t") for bar in printed]
    try:
        paths = predictor(history, pred_len=length, future_times=future)
    except TypeError:
        paths = predictor(history)
    return list(paths or [])


def _usable(path) -> bool:
    if not isinstance(path, list) or len(path) < PREDICT_LEN:
        return False
    for bar in path[:PREDICT_LEN]:
        if not isinstance(bar, dict):
            return False
        for key in ("o", "h", "l", "c"):
            try:
                float(bar[key])
            except (KeyError, TypeError, ValueError):
                return False
    return True


def _distance(path, printed) -> float:
    total = 0.0
    for pred, real in zip(path, printed):
        for key in ("o", "h", "l", "c"):
            total += abs(float(pred[key]) - float(real[key]))
    return total


def _tick(root: str) -> float:
    from futuresfund.contracts import tick_size

    try:
        return float(tick_size(str(root or "")))
    except ValueError:
        return 0.25


def _offset(price, anchor: float, tick: float) -> float:
    steps = round((float(price) - float(anchor)) / float(tick))
    return round(steps * float(tick), 6)


def _relative(bar: dict, anchor: float, tick: float) -> dict:
    opened = _offset(bar["o"], anchor, tick)
    high = _offset(bar["h"], anchor, tick)
    low = _offset(bar["l"], anchor, tick)
    close = _offset(bar["c"], anchor, tick)
    return {
        "o": opened,
        "h": max(opened, high, low, close),
        "l": min(opened, high, low, close),
        "c": close,
        "v": int(round(float(bar.get("v") or 0))),
    }


def _token(bar: dict, anchor: float, tick: float) -> str:
    relative = _relative(bar, anchor, tick)
    return f"{body_mark(bar)}:{relative['o']}:{relative['h']}:{relative['l']}:{relative['c']}:{relative['v']}"


def _set_parts(path: list[dict], anchor: float, tick: float) -> tuple[str, list[dict]]:
    candles = [_relative(bar, anchor, tick) for bar in path[:PREDICT_LEN]]
    tokens = [
        f"{body_mark(bar)}:{relative['o']}:{relative['h']}:{relative['l']}:{relative['c']}:{relative['v']}"
        for bar, relative in zip(path[:PREDICT_LEN], candles)
    ]
    return "|".join(tokens), candles


def _place(offsets: list[dict], anchor: float) -> list[dict]:
    placed = []
    for row in offsets:
        placed.append({
            "o": float(anchor) + float(row["o"]),
            "h": float(anchor) + float(row["h"]),
            "l": float(anchor) + float(row["l"]),
            "c": float(anchor) + float(row["c"]),
            "v": float(row.get("v") or 0),
        })
    return placed


def _has_winner(sets: dict) -> bool:
    if not sets:
        return False
    counts = [int(row["count"]) for row in sets.values()]
    top = max(counts)
    return counts.count(top) == 1


def _choose_set(window: list[dict], sets: dict, samples: list, tick: float):
    anchor = float(window[-1]["c"])
    grouped: dict[str, int] = {}
    sample_rows: dict[str, list[dict]] = {}
    for path in samples:
        signature, candles = _set_parts(path, anchor, tick)
        grouped[signature] = grouped.get(signature, 0) + 1
        sample_rows[signature] = candles
    if not sets:
        if not grouped:
            return None, 0, False, 0
        signature = max(grouped, key=lambda item: (grouped[item], item))
        return _place(sample_rows[signature], anchor), grouped[signature], False, 0
    top = max(int(row["count"]) for row in sets.values())
    tied = [signature for signature, row in sets.items() if int(row["count"]) == top]
    signature = tied[0] if len(tied) == 1 else max(tied, key=lambda item: (grouped.get(item, 0), item))
    return _place(sets[signature]["candles"], anchor), grouped.get(signature, 0), True, int(sets[signature]["count"])


def _store_sets(window: list[dict], paths: list, contract: str, timeframe: str, path: Path) -> int:
    if not window or not paths:
        return 0
    root = str(contract).upper()
    tick = _tick(root)
    anchor = float(window[-1]["c"])
    ident = window_id(window, tick)
    added = 0
    with _file_lock:
        notes = _load(path)
        bucket = notes.setdefault("windows", {}).setdefault(root, {}).setdefault(str(timeframe), {}).setdefault(ident, {})
        for item in paths:
            if not _usable(item):
                continue
            signature, candles = _set_parts(item[:PREDICT_LEN], anchor, tick)
            row = bucket.setdefault(signature, {"count": 0, "candles": candles})
            row["count"] = int(row.get("count") or 0) + 1
            row["candles"] = candles
            added += 1
        shape = window_shape(window)
        if shape:
            listing = notes.setdefault("shapes", {}).setdefault(root, {}).setdefault(str(timeframe), {}).setdefault(shape, [])
            if ident not in listing:
                listing.append(ident)
        _save(path, notes)
    return added


def _publish_window(contract: str, timeframe: str, window: list[dict], paths: list, path: Path) -> None:
    sets = stored_sets(contract, timeframe, window, path)
    chosen, _agreed, seen, count = _choose_set(window, sets, [item for item in paths if _usable(item)], _tick(contract))
    if not chosen:
        return
    anchor = float(window[-1]["c"])
    direction = direction_of(anchor, float(window[-1].get("v") or 0), chosen)
    note = _note(contract, timeframe, direction, 0, 0, seen, count)
    _publish_view(contract, timeframe, window, chosen, direction, count, seen, note, sampling=True)


def _publish_view(root: str, timeframe: str, window: list[dict], predicted: list, direction: str, count: int, seen: bool, note: str, sampling: bool) -> None:
    tail = []
    for bar in window[-6:]:
        tail.append({"t": bar.get("t"), "o": bar.get("o"), "h": bar.get("h"), "l": bar.get("l"), "c": bar.get("c"), "v": bar.get("v")})
    shown = []
    for bar in predicted or []:
        shown.append({
            "t": bar.get("t"),
            "o": bar.get("o"),
            "h": bar.get("h"),
            "l": bar.get("l"),
            "c": bar.get("c"),
            "v": bar.get("v"),
            "predicted": True,
        })
    with _cache_lock:
        reached = _view.get("reached") or ""
        owner = _OWNERS.get(str(timeframe), _view.get("owner") or "")
        _view.update({
            "contract": str(root).upper(),
            "timeframe": str(timeframe),
            "candles": tail,
            "predicted": shown,
            "direction": direction,
            "count": int(count or 0),
            "reached": reached,
            "owner": owner,
            "sampling": sampling,
            "loaded": True,
            "seen": bool(seen),
            "note": note,
        })


def _remember_shown(root: str, timeframe: str, result: dict) -> None:
    if result.get("candles"):
        _shown[(str(root).upper(), str(timeframe))] = result


def _while_sampling(root: str, timeframe: str) -> dict:
    previous = _shown.get((str(root).upper(), str(timeframe)))
    if previous:
        kept = dict(previous)
        kept["sampling"] = True
        note = str(previous.get("note") or "")
        if "sampling" not in note.lower():
            kept["note"] = f"{note} Kronos is sampling.".strip()
        _view["sampling"] = True
        _view["note"] = kept["note"]
        return kept
    quiet = _quiet(False, waiting=True)
    _view["sampling"] = True
    _view["contract"] = str(root).upper()
    _view["timeframe"] = str(timeframe)
    _view["note"] = quiet["note"]
    return quiet


def _reached_label(stamp) -> str:
    try:
        epoch = float(stamp)
    except (TypeError, ValueError):
        return str(stamp or "")
    if epoch > 10_000_000_000:
        epoch /= 1000
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(_EASTERN).strftime("%Y-%m-%d %H:%M")


def _fill(key, candles, root, timeframe, path):
    closed = [candle for candle in candles if not candle.get("forming")]
    window = closed[-LOOKBACK:]
    sets = stored_sets(root, timeframe, window, path) if window else {}
    _view["sampling"] = True
    _view["contract"] = str(root).upper()
    _view["timeframe"] = str(timeframe)
    _view["note"] = "Kronos is sampling."
    try:
        from futuresfund.kronos_model import predict_paths

        result = _forecast(candles, root, timeframe, predict_paths, sets, path)
    except Exception as exc:
        traceback.print_exc()
        result = _quiet(bool(sets))
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            result["note"] = "Kronos is not loaded, so there is no prediction."
            _view["loaded"] = False
        else:
            result["note"] = "Kronos did not return a prediction."
            if not sets:
                result["note"] += " The five-year map has no row for this window."
        _view["sampling"] = False
        _view["note"] = result["note"]
        with _cache_lock:
            _failed[key] = result
            _retry_after[key] = time.time() + 20
            _running.discard(key)
        return
    _remember_shown(root, timeframe, result)
    with _cache_lock:
        _cache[key] = result
        _failed.pop(key, None)
        _running.discard(key)


def _research_loop(board) -> None:
    global _warned
    from futuresfund.learn import RESEARCH_CHARTS, research_instruments
    from futuresfund.research_bars import research_series
    from futuresfund.roster import CHART_RESEARCHER

    while not board.cancel.is_set():
        moved = False
        for timeframe in RESEARCH_CHARTS:
            for root in research_instruments():
                if board.cancel.is_set():
                    return
                try:
                    bars = _five_year(research_series(root, timeframe, include_live=False))
                except Exception:
                    continue
                if len(bars) <= PREDICT_LEN:
                    continue
                _view["sampling"] = True
                _view["contract"] = root
                _view["timeframe"] = timeframe
                _view["owner"] = _OWNERS.get(timeframe, "")
                _view["note"] = "Kronos is sampling."
                try:
                    from futuresfund.kronos_model import predict_paths

                    done = replay(bars, root, timeframe, predict_paths, steps=1)
                except Exception as exc:
                    traceback.print_exc()
                    _view["sampling"] = False
                    if not _warned:
                        _warned = True
                        message = "Kronos is not loaded, so there is no prediction."
                        _view["loaded"] = False
                        _view["note"] = message
                        if not isinstance(exc, (ImportError, ModuleNotFoundError)):
                            message = "Kronos did not return a prediction. No set was added."
                            _view["loaded"] = True
                            _view["note"] = message
                        try:
                            board.post(CHART_RESEARCHER, message, kind="log", channel=CHART_RESEARCHER)
                        except Exception:
                            pass
                    if not _pause(board, 30):
                        return
                    continue
                _view["sampling"] = False
                if done:
                    moved = True
                    if not _warned:
                        _warned = True
                        try:
                            board.post(
                                CHART_RESEARCHER,
                                "The 2min chart developer, the 5 min chart developer, and the 15 min chart developer are walking the five-year bars from 2021-10-04 through 2026-10-03 for ES, gold, and Nasdaq. Strategy search stays on the last 260 sessions. Kronos does not open a trade.",
                                kind="log",
                                channel=CHART_RESEARCHER,
                            )
                        except Exception:
                            pass
        if not moved and not _pause(board, 20):
            return


def _pause(board, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if board.cancel.is_set():
            return False
        time.sleep(0.2)
    return True


def _resume(bars, contract, timeframe, path: Path) -> int:
    cursor = _cursor(contract, timeframe, path)
    if cursor is None:
        return 1
    for index, bar in enumerate(bars):
        try:
            if float(bar.get("t")) >= float(cursor):
                return max(1, index)
        except (TypeError, ValueError):
            continue
    return len(bars)


def _cursor_key(contract, timeframe) -> str:
    """Separate from the earlier one-year cursor, so the five-year walk starts at 2021-10-04."""
    return f"{str(contract).upper()}|{timeframe}|five"


def _cursor(contract, timeframe, path: Path):
    notes = _load(path)
    return (notes.get("cursor") or {}).get(_cursor_key(contract, timeframe))


def _place_cursor(contract, timeframe, cursor, path: Path) -> None:
    with _file_lock:
        notes = _load(path)
        notes.setdefault("cursor", {})[_cursor_key(contract, timeframe)] = cursor
        _save(path, notes)


def _after(stamp):
    try:
        return float(stamp) + 1
    except (TypeError, ValueError):
        return stamp


def _record(contract: str, timeframe: str, shape: str, path: Path) -> None:
    if not shape:
        return
    root = str(contract).upper()
    with _file_lock:
        notes = _load(path)
        patterns = notes.setdefault("patterns", {})
        frames = patterns.setdefault(root, {})
        counts = frames.setdefault(str(timeframe), {})
        counts[shape] = int(counts.get(shape) or 0) + 1
        _save(path, notes)


def _load(path: Path) -> dict:
    cached = _notes_memory.get(str(path))
    if isinstance(cached, dict):
        return cached
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {"patterns": {}, "windows": {}, "cursor": {}}
    if not isinstance(data, dict):
        data = {"patterns": {}, "windows": {}, "cursor": {}}
    data.setdefault("patterns", {})
    data.setdefault("windows", {})
    data.setdefault("cursor", {})
    _notes_memory[str(path)] = data
    return data


def _save(path: Path, data: dict) -> None:
    _notes_memory[str(path)] = data
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)
