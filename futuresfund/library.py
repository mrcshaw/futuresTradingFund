"""Profitable Backtrader studies, grouped by the contract that made the money."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

_STAND_INS = {"donchian", "ema_cross", "rsi_revert", "vwap_side", "bollinger", "macd", "poc_pullback", "supertrend"}
_NOTES = Path(__file__).resolve().parent / "researchNotes"
_CACHE: dict = {"stamp": None, "rows": [], "index_mtime": None}
_INDEX_LOCK = threading.Lock()

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


def record_profitable(entry: dict) -> bool:
    """Keep the most profitable row, and the steadiest different row, for this script."""
    with _INDEX_LOCK:
        rows = _load_index_rows()
        script = str(entry.get("script") or entry.get("title") or "")
        contract = entry.get("contract")
        kept = [
            row for row in rows
            if not (row.get("contract") == contract and str(row.get("script") or row.get("title") or "") == script)
        ]
        group = [
            row for row in rows
            if row.get("contract") == contract and str(row.get("script") or row.get("title") or "") == script
        ]
        folded = _library_rows([dict(row) for row in group] + [dict(entry)])
        _CACHE["rows"] = kept + folded
        _write_index(_CACHE["rows"])
        return any(row.get("id") == entry.get("id") and str(row.get("kind") or "").startswith("Most profitable") for row in folded)


def library_script(strategy_id: str, contract: str | None = None) -> dict | None:
    """Open the Pine file for a library row without reading the research notes."""
    row = _cached_row(strategy_id, contract)
    filename = str((row or {}).get("file") or _pine_name(strategy_id))
    from futuresfund.contracts import POINT_VALUE, root_of
    from futuresfund.strategy import _apply_inputs, pine_text

    pine = pine_text(filename)
    params = (row or {}).get("params") if isinstance((row or {}).get("params"), dict) else {}
    if pine and params:
        try:
            pine = _apply_inputs(pine, params)
        except (TypeError, ValueError, re.error):
            pass
    if not pine:
        return None
    try:
        root = root_of(str((row or {}).get("contract") or contract or "ES1!"))
    except ValueError:
        root = "ES"
    point = float(POINT_VALUE.get(root, 50))
    if point != 50:
        pine = _with_point_value(pine, point)
    title = str((row or {}).get("title") or _strategy_title(pine) or filename)
    return {"id": strategy_id, "title": title, "pine": pine}


def _cached_row(strategy_id: str, contract: str | None) -> dict | None:
    with _INDEX_LOCK:
        index = _index_path()
        if index.is_file() and (_CACHE.get("index_mtime") != index.stat().st_mtime_ns or _CACHE.get("index_path") != str(index)):
            _CACHE["rows"] = _load_index_rows()
            _CACHE["index_mtime"] = index.stat().st_mtime_ns
            _CACHE["index_path"] = str(index)
        matches = [item for item in (_CACHE.get("rows") or []) if item.get("id") == strategy_id]
    if contract:
        return next((item for item in matches if item.get("contract") == contract), None)
    return matches[0] if matches else None


def _pine_name(strategy_id: str) -> str:
    if "-" in strategy_id:
        head, tail = strategy_id.rsplit("-", 1)
        if len(tail) == 12 and all(char in "0123456789abcdef" for char in tail):
            return f"{head}.pine"
    return strategy_id if str(strategy_id).endswith(".pine") else f"{strategy_id}.pine"


def _strategy_title(source: str) -> str:
    match = re.search(r'strategy\s*\(\s*"([^"]+)"', source or "")
    return match.group(1).strip() if match else ""


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


def _index_path() -> Path:
    return _NOTES / "library_index.json"


def _notes_stamp() -> tuple:
    if not _NOTES.is_dir():
        return tuple()
    return tuple(sorted((path.name, path.stat().st_mtime_ns) for path in _NOTES.glob("*.json")))


def _rows() -> list[dict]:
    """Library rows. The saved index is small. A missing index is built from the notes once."""
    with _INDEX_LOCK:
        index = _index_path()
        if index.is_file():
            mtime = index.stat().st_mtime_ns
            if _CACHE.get("index_mtime") == mtime and _CACHE.get("rows") and _CACHE.get("index_path") == str(index):
                return [dict(row) for row in _CACHE["rows"]]
            rows = _load_index_rows()
            _CACHE["rows"] = rows
            _CACHE["index_mtime"] = mtime
            _CACHE["index_path"] = str(index)
            return [dict(row) for row in rows]
        rows = _scan_notes()
        _CACHE["rows"] = rows
        _write_index(rows)
        return [dict(row) for row in rows]


def _load_index_rows() -> list[dict]:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _write_index(rows: list[dict]) -> None:
    _NOTES.mkdir(parents=True, exist_ok=True)
    path = _index_path()
    path.write_text(json.dumps({"rows": rows}, indent=2, default=str), encoding="utf-8")
    _CACHE["index_mtime"] = path.stat().st_mtime_ns
    _CACHE["index_path"] = str(path)
    _CACHE["rows"] = rows


def _scan_notes() -> list[dict]:
    collected: dict[str, dict] = {}
    seen = 0
    if _NOTES.is_dir():
        for path in _NOTES.glob("*.json"):
            if path.name in {"library_index.json", "strategy_cards.json", "creation_sources.json"}:
                continue
            for row in _entries(path):
                collected[row["id"]] = row
                seen += 1
                if seen % 400 == 0:
                    time.sleep(0)
    return _library_rows(list(collected.values()))


def _library_rows(rows: list[dict]) -> list[dict]:
    """One row for the highest profit, and another when a different result keeps more after the drawdown."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        script = str(row.get("script") or row.get("title") or "")
        groups.setdefault((row["contract"], script), []).append(row)
    shown = []
    for items in groups.values():
        richest = max(items, key=lambda item: float(item["net_profit"]))
        steadiest = max(
            items,
            key=lambda item: (
                float(item["net_profit"]) - float(item.get("max_drawdown") or 0),
                -float(item.get("max_drawdown") or 0),
            ),
        )
        keeps_more = float(steadiest["net_profit"]) > float(steadiest.get("max_drawdown") or 0)
        profit_row = dict(richest)
        if steadiest["id"] == richest["id"] and keeps_more:
            profit_row["kind"] = "Most profitable and most consistent"
        else:
            profit_row["kind"] = "Most profitable"
        shown.append(profit_row)
        if steadiest["id"] == richest["id"] or not keeps_more:
            continue
        steady_row = dict(steadiest)
        steady_row["kind"] = "Most consistent"
        shown.append(steady_row)
    return sorted(shown, key=lambda item: -float(item["net_profit"]))


def _entries(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    found: dict[str, dict] = {}

    def consider(record, contract: str | None) -> None:
        if not isinstance(record, dict):
            return
        marked = dict(record)
        if contract and not marked.get("contract"):
            marked["contract"] = contract
        row = _measured_row(data, marked, path)
        if row is None:
            return
        row["script"] = row["title"]
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        row["params"] = params
        change = str(record.get("change") or "").strip()
        if change and change not in {"Original", "Starting settings"} and not change.startswith("Starting measurement"):
            row["title"] = f"{row['title']} — {change}"
        fingerprint = json.dumps(
            [row["contract"], row["timeframe"], params, row["net_profit"], row["trades"]],
            sort_keys=True,
            default=str,
        )
        row["id"] = f"{path.stem}-{hashlib.sha1(fingerprint.encode()).hexdigest()[:12]}"
        found[row["id"]] = row

    def trials_of(record, contract: str | None) -> None:
        if not isinstance(record, dict):
            return
        consider(record.get("best"), contract)
        for name in ("trials", "profitable"):
            rows = record.get(name)
            if isinstance(rows, list):
                for item in rows:
                    consider(item, contract)
        charts = record.get("charts")
        if isinstance(charts, dict):
            for chart in charts.values():
                trials_of(chart, contract)

    instruments = data.get("instruments")
    if isinstance(instruments, dict):
        for name, record in instruments.items():
            hint = f"{str(name).upper()}1!"
            if isinstance(record, dict) and isinstance(record.get("best"), dict) and record["best"].get("contract"):
                hint = str(record["best"]["contract"])
            trials_of(record, hint)
    consider(data.get("best"), data.get("contract"))
    if isinstance(data.get("trials"), list):
        for item in data["trials"]:
            consider(item, data.get("contract"))
    return list(found.values())


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
