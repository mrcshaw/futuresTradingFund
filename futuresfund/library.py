"""Profitable PineForge studies, grouped by the contract that made the money."""

from __future__ import annotations

import json
import re
from pathlib import Path

_STAND_INS = {"donchian", "ema_cross", "rsi_revert", "vwap_side", "bollinger", "macd", "poc_pullback", "supertrend"}
_NOTES = Path(__file__).resolve().parent / "researchNotes"
_CACHE: dict = {"stamp": None, "rows": []}

# Checked before the ES default. A study that names no contract was run on the ES chart.
_NAMED = (
    ("GC1!", "Gold", ("gold", "mgc", "gc")),
    ("NQ1!", "Nasdaq", ("nq", "mnq", "nasdaq")),
    ("CL1!", "Crude", ("crude", "cl")),
    ("ES1!", "ES", ("es",)),
)
_DEFAULT = ("ES1!", "ES")


def contract_of(title: str, filename: str = "", explicit: str | None = None) -> tuple[str, str]:
    """Return the contract symbol and the shelf name."""
    if explicit:
        text = str(explicit).strip()
        for symbol, label, _words in _NAMED:
            if text.upper().startswith(symbol[:2]) or text.lower() == label.lower():
                return symbol, label
        return text, text
    blob = f"{title} {filename}".lower()
    for symbol, label, words in _NAMED:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", blob) for word in words):
            return symbol, label
    return _DEFAULT


def search_library(contract: str | None = None, query: str = "") -> dict:
    """Profitable studies for one contract shelf. Gold stays separate from ES."""
    rows = _rows()
    chosen = str(contract or "ES1!").strip()
    if chosen.lower() not in {"", "all"}:
        rows = [row for row in rows if row["contract"] == chosen or row["contract_label"].lower() == chosen.lower()]
    needle = str(query or "").strip().lower()
    if needle:
        rows = [row for row in rows if needle in row["title"].lower() or needle in str(row["timeframe"]).lower()]
    return {"contract": chosen, "contracts": _shelves(), "strategies": rows}


def library_script(strategy_id: str) -> dict | None:
    row = next((item for item in _rows() if item["id"] == strategy_id), None)
    if row is None:
        return None
    from futuresfund.strategy import pine_text

    return {"id": row["id"], "title": row["title"], "pine": pine_text(row.get("file") or "")}


def agent_catalog(limit: int = 12) -> str:
    """A short list the desks can read before they invent a result."""
    lines = ["Profitable strategy library. Each line is the most profitable timeframe of a real Pine script."]
    grouped: dict[str, list[dict]] = {}
    for row in _rows():
        grouped.setdefault(row["contract_label"], []).append(row)
    for label in ("ES", "Gold"):
        shelf = grouped.get(label) or []
        if not shelf:
            lines.append(f"{label}: none yet.")
            continue
        lines.append(f"{label}:")
        for row in shelf[:limit]:
            lines.append(
                f"- {row['title']}: {row['timeframe']}, profit {row['net_profit']}, "
                f"drawdown {row['max_drawdown']}, trades {row['trades']}"
            )
    for label, shelf in grouped.items():
        if label in {"ES", "Gold"}:
            continue
        lines.append(f"{label}:")
        for row in shelf[:limit]:
            lines.append(
                f"- {row['title']}: {row['timeframe']}, profit {row['net_profit']}, "
                f"drawdown {row['max_drawdown']}, trades {row['trades']}"
            )
    return "\n".join(lines)


def _shelves() -> list[dict]:
    counts: dict[str, dict] = {
        "ES1!": {"contract": "ES1!", "label": "ES", "count": 0},
        "GC1!": {"contract": "GC1!", "label": "Gold", "count": 0},
    }
    for row in _rows():
        slot = counts.setdefault(row["contract"], {"contract": row["contract"], "label": row["contract_label"], "count": 0})
        slot["count"] += 1
    ordered = [counts.pop("ES1!"), counts.pop("GC1!")]
    ordered.extend(counts.values())
    return ordered


def _rows() -> list[dict]:
    stamp = _NOTES.stat().st_mtime if _NOTES.is_dir() else 0
    cached = _CACHE.get("rows") or []
    if _CACHE.get("stamp") == stamp and cached:
        return [dict(row) for row in cached]
    ranked: dict[tuple[str, str], dict] = {}
    if _NOTES.is_dir():
        for path in _NOTES.glob("*.json"):
            row = _row(path)
            if row is None:
                continue
            key = (row["contract"], _fold(row["title"]))
            current = ranked.get(key)
            if current is None or float(row["net_profit"]) > float(current["net_profit"]):
                ranked[key] = row
    rows = sorted(ranked.values(), key=lambda item: float(item["net_profit"]), reverse=True)
    _CACHE["stamp"] = stamp
    _CACHE["rows"] = rows
    return [dict(row) for row in rows]


def _row(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    best = data.get("best") if isinstance(data.get("best"), dict) else {}
    runner = str(best.get("runner") or "")
    engine = str(best.get("engine") or runner)
    if runner in _STAND_INS or engine in _STAND_INS:
        return None
    if engine != "pineforge" and runner != "pineforge":
        return None
    note = str(best.get("note") or "")
    if "price rule" in note or "not substituted" in note or "cannot enter" in note:
        return None
    measured = _best_run(best)
    if measured is None or measured["net_profit"] <= 0 or measured["trades"] < 1:
        return None
    title = str(data.get("title") or "").strip()
    if not title or title.lower().startswith("untitled"):
        title = path.stem.replace("_", " ")
    symbol, label = contract_of(title, path.name, data.get("contract") or best.get("contract"))
    win = measured.get("win_rate")
    return {
        "id": path.stem,
        "title": title,
        "file": str(data.get("file") or ""),
        "contract": symbol,
        "contract_label": label,
        "timeframe": measured["timeframe"],
        "net_profit": measured["net_profit"],
        "max_drawdown": measured["max_drawdown"],
        "trades": measured["trades"],
        "win_rate": None if win is None else round(float(win), 2),
    }


def _best_run(best: dict) -> dict | None:
    frames = [frame for frame in best.get("frames") or [] if isinstance(frame, dict) and frame.get("net_profit") is not None]
    source = best
    if frames:
        source = max(frames, key=lambda frame: float(frame.get("net_profit") or -10**12))
    try:
        profit = float(source.get("net_profit"))
        drawdown = float(source.get("max_drawdown") or 0)
        trades = int(source.get("trades") or 0)
    except (TypeError, ValueError):
        return None
    win = best.get("win_rate") if str(source.get("timeframe") or "") == str(best.get("timeframe") or "") else source.get("win_rate")
    return {
        "timeframe": str(source.get("timeframe") or best.get("timeframe") or ""),
        "net_profit": profit,
        "max_drawdown": drawdown,
        "trades": trades,
        "win_rate": win,
    }


def _fold(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
