"""Profitable Backtrader studies, grouped by the contract that made the money."""

from __future__ import annotations

import json
import re
from pathlib import Path

_STAND_INS = {"donchian", "ema_cross", "rsi_revert", "vwap_side", "bollinger", "macd", "poc_pullback", "supertrend"}
_NOTES = Path(__file__).resolve().parent / "researchNotes"
_CACHE: dict = {"stamp": None, "rows": []}

# Checked before the ES default. A study that names no contract was run on the ES chart.
_NAMED = (
    ("GC1!", "Gold", ("gold", "mgc", "gc", "qo")),
    ("NQ1!", "Nasdaq", ("nq", "mnq", "nasdaq")),
    ("CL1!", "Crude", ("crude", "cl")),
    ("ES1!", "ES", ("es",)),
)
_DEFAULT = ("ES1!", "ES")


def _with_point_value(source: str, point: float) -> str:
    """Dollar stops use this contract's point value. Nasdaq is 20, not the 50 written for ES."""
    return re.sub(
        r"((?:pv|point_value|pointvalue)\s*=\s*input\.float\(\s*)[-0-9.]+",
        lambda match: f"{match.group(1)}{point:g}",
        source or "",
        flags=re.IGNORECASE,
    )


def contract_of(title: str, filename: str = "", explicit: str | None = None) -> tuple[str, str]:
    """Return the contract symbol and the shelf name."""
    if explicit:
        text = str(explicit).strip()
        folded = text.upper()
        if folded.startswith("QO") or folded.startswith("MGC") or folded.startswith("GC") or folded == "GOLD":
            return "GC1!", "Gold"
        for symbol, label, _words in _NAMED:
            if folded.startswith(symbol[:2]) or text.lower() == label.lower():
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


def library_script(strategy_id: str, contract: str | None = None) -> dict | None:
    matches = [item for item in _rows() if item["id"] == strategy_id]
    if contract:
        row = next((item for item in matches if item["contract"] == contract), None)
    else:
        row = matches[0] if matches else None
    if row is None:
        return None
    from futuresfund.contracts import POINT_VALUE, root_of
    from futuresfund.strategy import pine_text

    pine = pine_text(row.get("file") or "")
    try:
        root = root_of(str(row.get("contract") or "ES1!"))
    except ValueError:
        root = "ES"
    point = float(POINT_VALUE.get(root, 50))
    if point != 50 and pine:
        pine = _with_point_value(pine, point)
    return {"id": row["id"], "title": row["title"], "pine": pine}


def agent_catalog(limit: int = 12) -> str:
    """A short list the desks can read before they invent a result."""
    lines = ["Profitable strategy library. Each line is the most profitable timeframe of a real Pine script."]
    grouped: dict[str, list[dict]] = {}
    for row in _rows():
        grouped.setdefault(row["contract_label"], []).append(row)
    for label in ("ES", "Gold", "Nasdaq"):
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
        if label in {"ES", "Gold", "Nasdaq"}:
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
        "NQ1!": {"contract": "NQ1!", "label": "Nasdaq", "count": 0},
    }
    for row in _rows():
        slot = counts.setdefault(row["contract"], {"contract": row["contract"], "label": row["contract_label"], "count": 0})
        slot["count"] += 1
    ordered = [counts.pop("ES1!"), counts.pop("GC1!"), counts.pop("NQ1!")]
    ordered.extend(counts.values())
    return ordered


def _notes_stamp() -> tuple:
    if not _NOTES.is_dir():
        return tuple()
    return tuple(sorted((path.name, path.stat().st_mtime_ns) for path in _NOTES.glob("*.json")))


def _rows() -> list[dict]:
    stamp = _notes_stamp()
    cached = _CACHE.get("rows") or []
    if _CACHE.get("stamp") == stamp and cached:
        return [dict(row) for row in cached]
    ranked: dict[tuple[str, str], dict] = {}
    if _NOTES.is_dir():
        for path in _NOTES.glob("*.json"):
            for row in _entries(path):
                key = (row["contract"], _fold(row["title"]))
                current = ranked.get(key)
                if current is None or float(row["net_profit"]) > float(current["net_profit"]):
                    ranked[key] = row
    rows = sorted(ranked.values(), key=lambda item: float(item["net_profit"]), reverse=True)
    _CACHE["stamp"] = stamp
    _CACHE["rows"] = rows
    return [dict(row) for row in rows]


def _entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    instruments = data.get("instruments")
    if isinstance(instruments, dict) and instruments:
        rows = []
        for name, record in instruments.items():
            if not isinstance(record, dict):
                continue
            best = record.get("best") if isinstance(record.get("best"), dict) else {}
            marked = dict(best)
            marked.setdefault("contract", f"{str(name).upper()}1!")
            row = _measured_row(data, marked, path)
            if row is not None:
                rows.append(row)
        return rows
    row = _measured_row(data, data.get("best") if isinstance(data.get("best"), dict) else {}, path)
    return [row] if row is not None else []


def _row(path: Path) -> dict | None:
    found = _entries(path)
    return found[0] if found else None


def _measured_row(data: dict, best: dict, path: Path) -> dict | None:
    runner = str(best.get("runner") or "")
    engine = str(best.get("engine") or runner)
    if runner in _STAND_INS or engine in _STAND_INS:
        return None
    if engine != "backtrader" and runner != "backtrader":
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
