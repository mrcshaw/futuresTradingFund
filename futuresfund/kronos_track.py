"""How often a Kronos six-candle forecast points the same way as the six candles that print.

The first percentage waits until one forecast has six closed candles after it.
Buy, sell, and flat use the same direction as the chart forecast. Nothing here
sends an order.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

PATH = Path(__file__).resolve().parent / "researchNotes" / "kronos_direction.json"
_LOCK = threading.Lock()


def observe(root: str, timeframe: str, candles: list[dict], prediction: dict | None, contract: str = "", path: Path | None = None) -> dict:
    """Remember this forecast once, score any forecast whose six candles have closed, and report the match rate."""
    root = str(root or "").upper()
    frame = str(timeframe or "")
    label = contract or f"{root}1!"
    closed = [candle for candle in candles or [] if not candle.get("forming")]
    with _LOCK:
        notes = _load(path)
        row = notes.setdefault(_key(root, frame), {"pending": {}, "correct": 0, "scored": 0})
        changed = _record(row, closed, prediction or {})
        changed = _score(row, closed) or changed
        if changed:
            _save(notes, path)
        return _summary(row, label, frame)


def entry_signal(root: str, timeframe: str, closed: list[dict], traded: set | None = None, path: Path | None = None) -> dict | None:
    """A buy or sell whose first candle is the latest closed candle. A later bar is not chased."""
    done = {int(value) for value in (traded or set())}
    with _LOCK:
        row = _load(path).get(_key(str(root).upper(), str(timeframe))) or {}
    epochs = [(epoch_of(candle.get("t")), candle) for candle in closed or []]
    epochs = [(stamp, candle) for stamp, candle in epochs if stamp is not None]
    if not epochs:
        return None
    latest = epochs[-1][0]
    chosen = None
    for stamp, item in (row.get("pending") or {}).items():
        if not isinstance(item, dict) or item.get("direction") not in {"Buy", "Sell"}:
            continue
        try:
            anchor = int(float(stamp))
        except (TypeError, ValueError):
            continue
        if anchor in done:
            continue
        after = [candle for when, candle in epochs if when > anchor]
        if len(after) != 1 or epoch_of(after[0].get("t")) != latest:
            continue
        if chosen is None or anchor > chosen["anchor"]:
            chosen = {"anchor": anchor, "direction": item["direction"], "bar": after[0], "bar_epoch": latest}
    return chosen


def epoch_of(value) -> int | None:
    """Candle time in seconds. Chart stamps are ISO strings or epoch numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return int(number)
    from futuresfund.chart_feed import _epoch_ms

    millis = _epoch_ms(value)
    if millis is None:
        return None
    return int(millis) // 1000


def _record(row: dict, closed: list[dict], prediction: dict) -> bool:
    if not closed or not prediction.get("candles"):
        return False
    direction = prediction.get("direction")
    if direction not in {"Buy", "Sell", "Flat"}:
        return False
    anchor = epoch_of(prediction.get("anchor"))
    if anchor is None or not any(epoch_of(candle.get("t")) == anchor for candle in closed):
        return False
    pending = row.setdefault("pending", {})
    key = str(anchor)
    if key in pending:
        return False
    pending[key] = {"direction": direction}
    return True


def _score(row: dict, closed: list[dict]) -> bool:
    from futuresfund.kronos_replay import direction_of

    epochs = []
    for candle in closed:
        stamp = epoch_of(candle.get("t"))
        if stamp is not None:
            epochs.append((stamp, candle))
    done = []
    for stamp, item in list((row.get("pending") or {}).items()):
        if not isinstance(item, dict):
            continue
        try:
            anchor = int(float(stamp))
        except (TypeError, ValueError):
            continue
        anchor_bar = next((candle for when, candle in epochs if when == anchor), None)
        after = [candle for when, candle in epochs if when > anchor]
        if anchor_bar is None or len(after) < 6:
            continue
        actual = direction_of(float(anchor_bar["c"]), float(anchor_bar.get("v") or 0), after[:6])
        row["scored"] = int(row.get("scored") or 0) + 1
        if actual == item.get("direction"):
            row["correct"] = int(row.get("correct") or 0) + 1
        done.append(stamp)
    for stamp in done:
        row["pending"].pop(stamp, None)
    return bool(done)


def _summary(row: dict, contract: str, timeframe: str) -> dict:
    scored = int(row.get("scored") or 0)
    correct = int(row.get("correct") or 0)
    pending = len(row.get("pending") or {})
    if scored < 1:
        text = f"Kronos direction match on {contract} {timeframe} waits until 6 candles have printed after a forecast."
        if pending == 1:
            text += " 1 forecast is still open."
        elif pending:
            text += f" {pending} forecasts are still open."
        percent = None
    else:
        percent = int(round(100.0 * correct / scored))
        word = "time" if scored == 1 else "times"
        text = f"Kronos matched the 6-candle direction on {contract} {timeframe} {correct} of {scored} {word}, {percent}%."
    return {
        "ready": scored > 0,
        "correct": correct,
        "scored": scored,
        "percent": percent,
        "pending": pending,
        "text": text,
    }


def _key(root: str, timeframe: str) -> str:
    return f"{root}|{timeframe}"


def _load(path: Path | None) -> dict:
    file = path or PATH
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(notes: dict, path: Path | None) -> None:
    file = path or PATH
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(notes), encoding="utf-8")
    except OSError:
        return
