"""PineTS studies grouped by the contract that made the money.

A script keeps one row per contract and timeframe: the highest profit among runs at the library pace.
130 to 600 trades a year is the rewarded count. More than 600 trades a year is too many and is not filed.
Three a week still files when that count stays inside the band and the profit factor is 3 or more,
or when the profit and drawdown are near $50,000 and $22,000.
Two trades is not filed.
The trailing-drawdown cap and a steadier run with less profit are not part of the shelf.
A run near $50,000 profit and $22,000 drawdown stays even when a later run made more
profit with a worse ratio. Among those near runs, the higher profit stays.
The most profitable shelf shows a run only after it is profitable on the full
five-year dataset: profit over $20,000, no single trade loses more than $1,500,
and one version of each strategy name on each contract and timeframe. A one-year
research run stays on the waiting list until that longer test. A two-year table
is a later shelf and is not this one.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

_STAND_INS = {"donchian", "ema_cross", "rsi_revert", "vwap_side", "bollinger", "macd", "poc_pullback", "supertrend"}
# Library rows measured on an older engine are measured again when this changes.
LIBRARY_ENGINE = "pinets-1"
OBJECTIVE_TAG = "shelf-most-profitable"   # how the shelf is folded and sorted; see objective.py (objective.stamp adds the headquarters figures)
_RESEARCH_ROOT = {"ES1!": "ES", "NQ1!": "NQ", "MNQ1!": "MNQ", "GC1!": "GC"}
_NOTES = Path(__file__).resolve().parent / "researchNotes"
_CACHE: dict = {"stamp": None, "rows": [], "index_mtime": None}
_INDEX_LOCK = threading.Lock()

# Checked before the ES default. A study that names no contract was run on the ES chart.
_NAMED = (
    ("MNQ1!", "Micro Nasdaq", ("mnq",)),
    ("GC1!", "Gold", ("gold", "mgc", "gc", "qo")),
    ("NQ1!", "Nasdaq", ("nq", "nasdaq")),
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


def versions_filed(script: str, contract: str | None, timeframe: str) -> list[dict]:
    """Rows of this name already on this contract and timeframe, on either library list."""
    family = strategy_family({"script": script})
    if not family:
        return []
    found = []
    for row in list(_rows()) + list(_untested_rows()):
        if strategy_family(row) != family:
            continue
        if contract and str(row.get("contract") or "") != str(contract):
            continue
        if str(row.get("timeframe") or "") != str(timeframe or ""):
            continue
        found.append(row)
    return found


def shelf_for(script: str, contract: str | None = None) -> list[dict]:
    """Rows of this script actually on the shelf. The library is the fact, not the test that just finished."""
    wanted = str(script or "").strip()
    if not wanted:
        return []
    found = []
    for row in _rows():
        name = str(row.get("script") or row.get("title") or "")
        if name != wanted and not name.startswith(wanted + " —"):
            continue
        if contract and row.get("contract") != contract:
            continue
        found.append(row)
    return found


def search_library(contract: str | None = None, query: str = "") -> dict:
    """Profitable studies for one contract shelf. Gold stays separate from ES."""
    chosen = str(contract or "ES1!").strip()
    needle = str(query or "").strip().lower()
    return {
        "contract": chosen,
        "contracts": _shelves(),
        "strategies": _matching(_rows(), chosen, needle),
        "untested": _matching(_untested_rows(), chosen, needle),
    }


def _matching(rows: list[dict], chosen: str, needle: str) -> list[dict]:
    if chosen.lower() not in {"", "all"}:
        rows = [row for row in rows if row.get("contract") == chosen or str(row.get("contract_label") or "").lower() == chosen.lower()]
    if needle:
        rows = [row for row in rows if needle in str(row.get("title") or "").lower() or needle in str(row.get("timeframe") or "").lower()]
    return rows


def measured_on_full_dataset(row: dict | None) -> bool:
    """True after a five-year measurement. A hand-added script has not been measured."""
    if not isinstance(row, dict) or row.get("kind") == "Added":
        return False
    return not measured_on_one_year(row)


def record_profitable(entry: dict) -> bool:
    """File this run on the most profitable shelf only after a profitable five-year test.

    A one-year result stays on the waiting list. One strategy name keeps its most
    profitable five-year version on that contract and timeframe.
    """
    if not measured_on_full_dataset(entry) or not _has_script(entry):
        return False
    if not str(entry.get("added_at") or "").strip():
        entry["added_at"] = _filed_at()
    with _INDEX_LOCK:
        rows = _load_index_rows()
        script = str(entry.get("script") or entry.get("title") or "")
        contract = entry.get("contract")
        timeframe = str(entry.get("timeframe") or "")
        kept = [
            row for row in rows
            if not (
                row.get("contract") == contract
                and str(row.get("script") or row.get("title") or "") == script
                and str(row.get("timeframe") or "") == timeframe
            )
        ]
        group = [
            row for row in rows
            if row.get("contract") == contract
            and str(row.get("script") or row.get("title") or "") == script
            and str(row.get("timeframe") or "") == timeframe
        ]
        folded = _library_rows([dict(row) for row in group] + [dict(entry)])
        shown = shelf_rows(kept + folded)
        _CACHE["rows"] = shown
        _write_index(shown)
        return any(row.get("id") == entry.get("id") and row.get("primary") for row in shown)


def measured_on_one_year(row: dict | None) -> bool:
    """True when this result has not been run on the five-year download.

    Research uses the last 260 sessions. A stored span of about five years, or an
    explicit full-dataset mark, has already left that window.
    """
    if not isinstance(row, dict):
        return False
    if row.get("full_dataset") is True:
        return False
    window = str(row.get("window") or "").strip().lower()
    if window in {"full", "five years", "five year", "full dataset"}:
        return False
    if row.get("kind") == "Added":
        return True
    from futuresfund.objective import RESEARCH_SESSIONS

    try:
        days = float(row["days"]) if row.get("days") is not None else None
    except (TypeError, ValueError, KeyError):
        days = None
    if days is not None and days >= RESEARCH_SESSIONS * 4:
        return False
    return True


def _stamp_untested(entry: dict) -> dict:
    """Mark a copy as waiting for the five-year test. The shelf row is left as it was."""
    from futuresfund.objective import profit_to_drawdown

    stored = dict(entry)
    stored["full_dataset"] = False
    stored["window"] = "one year"
    ratio = profit_to_drawdown(stored)
    if ratio is None or ratio == float("inf"):
        stored["profit_to_drawdown"] = None
    else:
        stored["profit_to_drawdown"] = round(float(ratio), 3)
    if stored.get("kind") != "Added":
        stored["kind"] = "Not tested on the full dataset"
    stored["primary"] = True
    if not str(stored.get("added_at") or "").strip():
        stored["added_at"] = _filed_at()
    return stored


def _untested_token(row: dict) -> tuple:
    return (
        row.get("id"),
        row.get("contract"),
        row.get("timeframe"),
        row.get("net_profit"),
        row.get("trades"),
        row.get("kind"),
        row.get("full_dataset"),
        row.get("window"),
        row.get("added_at"),
    )


def _accepted_untested(row: dict) -> bool:
    """A new one-year run clears the numbers saved on the strategy library page right now."""
    if row.get("kind") == "Added":
        return measured_on_one_year(row)
    return measured_on_one_year(row) and library_ready(row) and _trade_loss_ok(row)


def waiting_rows(rows: list[dict]) -> list[dict]:
    """Rows already on the Not tested list.

    A change to the profit, profit factor, win rate, or trade count on the strategy
    library page decides the next filing. It does not take off a run that was already
    accepted. One version of each name stays on each contract and timeframe. A
    hand-added script stays.
    """
    added = [dict(row) for row in rows if row.get("kind") == "Added"]
    ready = [row for row in rows if row.get("kind") != "Added" and measured_on_one_year(row)]
    shown = _pick_best(ready) + added
    return sorted(shown, key=lambda item: float(item.get("net_profit") or 0), reverse=True)


def _merge_untested(untested: list[dict], shelf: list[dict]) -> list[dict]:
    """One-year runs join the waiting list. The same name and chart keeps its highest profit.

    Rows already stored stay when the acceptance numbers change. A shelf row joins only
    when it clears the numbers saved now.
    """
    waiting = [_stamp_untested(row) for row in untested if row.get("kind") == "Added" or measured_on_one_year(row)]
    for row in shelf:
        if _accepted_untested(row):
            waiting.append(_stamp_untested(row))
    return waiting_rows(waiting)


def record_untested(entry: dict, *, opening_profit: bool = False) -> bool:
    """File a one-year run that has not been tested on the five-year download.

    The waiting list uses the one-year sample and the numbers on the strategy library
    page: profit, profit factor, win rate, and trade count. One trade down
    more than $1,500 is not filed. It does not wait for the five-year account gates.
    A repeat of the same result is not filed again. A later run with less profit
    does not replace it.

    An opening result is filed when it clears the profit number on the strategy library
    page, stays under the drawdown line, and no single trade loses more than $1,500.
    """
    if not measured_on_one_year(entry):
        return False
    stored = _stamp_untested(entry)
    if not _has_script(stored):
        return False
    if opening_profit:
        from futuresfund.book import strategy_acceptance

        limits = strategy_acceptance()
        try:
            profit = float(stored.get("net_profit"))
        except (TypeError, ValueError):
            return False
        if profit <= 0 or not limits.get("use_profit") or profit <= float(limits.get("min_profit") or 0) or not _trade_loss_ok(stored) or not _account_survives(stored):
            return False
    elif not _accepted_untested(stored):
        return False
    with _INDEX_LOCK:
        rows = _load_untested()
        if rows is None:
            return False
        shown = waiting_rows([row for row in rows if row.get("id") != stored.get("id")] + [stored])
        if not any(row.get("id") == stored.get("id") for row in shown):
            return False
        previous = next((row for row in rows if row.get("id") == stored.get("id")), None)
        if previous is not None and _untested_token(previous) == _untested_token(stored):
            return False
        _write_untested(shown)
        return True


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
    contract_name = str((row or {}).get("contract") or contract or "ES1!")
    root = _RESEARCH_ROOT.get(contract_name)
    if not root:
        try:
            root = root_of(contract_name)
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
    found = next((item for item in matches if item.get("contract") == contract), None) if contract else (matches[0] if matches else None)
    if found is not None:
        return found
    extras = [item for item in _load_untested() if item.get("id") == strategy_id]
    if contract:
        return next((item for item in extras if item.get("contract") == contract), None)
    return extras[0] if extras else None


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
    lines = [
        "Strategy library. Each script keeps its most profitable run. The lower drawdown and a steady pace are not required."
    ]
    grouped: dict[str, list[dict]] = {}
    for row in _rows():
        grouped.setdefault(row["contract_label"], []).append(row)
    for label in ("ES", "Gold", "Nasdaq", "Micro Nasdaq"):
        shelf = grouped.get(label) or []
        if not shelf:
            lines.append(f"{label}: none yet.")
            continue
        lines.append(f"{label}:")
        for row in shelf[:limit]:
            lines.append(
                f"- {row['title']}: {row['timeframe']}, profit {row['net_profit']}, "
                f"drawdown {row['max_drawdown']}, trades {row['trades']}" + (", target met" if row.get("target_met") else "")
            )
    for label, shelf in grouped.items():
        if label in {"ES", "Gold", "Nasdaq", "Micro Nasdaq"}:
            continue
        lines.append(f"{label}:")
        for row in shelf[:limit]:
            lines.append(
                f"- {row['title']}: {row['timeframe']}, profit {row['net_profit']}, "
                f"drawdown {row['max_drawdown']}, trades {row['trades']}"
            )
    return "\n".join(lines)


def _filed_at() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _shelves() -> list[dict]:
    counts: dict[str, dict] = {
        "ES1!": {"contract": "ES1!", "label": "ES", "count": 0, "untested": 0},
        "GC1!": {"contract": "GC1!", "label": "Gold", "count": 0, "untested": 0},
        "NQ1!": {"contract": "NQ1!", "label": "Nasdaq", "count": 0, "untested": 0},
        "MNQ1!": {"contract": "MNQ1!", "label": "Micro Nasdaq", "count": 0, "untested": 0},
    }
    for row in _rows():
        slot = counts.setdefault(
            row["contract"],
            {"contract": row["contract"], "label": row.get("contract_label") or row["contract"], "count": 0, "untested": 0},
        )
        slot["count"] += 1
    for row in _untested_rows():
        contract = str(row.get("contract") or "")
        if not contract:
            continue
        slot = counts.setdefault(
            contract,
            {"contract": contract, "label": row.get("contract_label") or contract, "count": 0, "untested": 0},
        )
        slot["untested"] = int(slot.get("untested") or 0) + 1
    ordered = [counts.pop("ES1!"), counts.pop("GC1!"), counts.pop("NQ1!"), counts.pop("MNQ1!")]
    ordered.extend(counts.values())
    return ordered


def _index_path() -> Path:
    return _NOTES / "library_index.json"


def _untested_path() -> Path:
    return _NOTES / "untested_library.json"


def _load_untested() -> list[dict] | None:
    """The stored one-year list. None means the file is there but could not be read, so callers must not replace it."""
    path = _untested_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = data.get("rows") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _write_untested(rows: list[dict]) -> None:
    _NOTES.mkdir(parents=True, exist_ok=True)
    _untested_path().write_text(
        json.dumps({"full_dataset": False, "window": "one year", "rows": rows}, indent=2, default=str),
        encoding="utf-8",
    )


def _has_script(row: dict) -> bool:
    """A table row stays only when its Pine file can be read."""
    from futuresfund.strategy import pine_text

    filename = str(row.get("file") or "").strip()
    if filename and pine_text(filename):
        return True
    strategy_id = str(row.get("id") or "").strip()
    return bool(strategy_id and pine_text(_pine_name(strategy_id)))


def _only_backed(rows: list[dict]) -> list[dict]:
    return [row for row in rows if _has_script(row)]


def _untested_rows() -> list[dict]:
    """One-year shelf runs, plus anything already waiting, until the five-year test."""
    with _INDEX_LOCK:
        current = _load_untested()
        if current is None:
            return []
        merged = _only_backed(_merge_untested(current, _load_index_rows()))
        if [_untested_token(row) for row in current] != [_untested_token(row) for row in merged]:
            _write_untested(merged)
        return [dict(row) for row in merged]


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
            loaded = _load_index_rows()
            rows = _only_backed(loaded)
            if len(rows) != len(loaded):
                _write_index(rows)
            _CACHE["rows"] = rows
            _CACHE["index_mtime"] = mtime
            _CACHE["index_path"] = str(index)
            return [dict(row) for row in rows]
        rows = _only_backed(_scan_notes())
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


def index_engine() -> str:
    """The engine the saved library rows were measured on."""
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("engine") or "") if isinstance(data, dict) else ""


def _write_index(rows: list[dict], engine: str | None = None) -> None:
    _NOTES.mkdir(parents=True, exist_ok=True)
    path = _index_path()
    from futuresfund.objective import stamp as objective_stamp

    stamp = engine if engine is not None else index_engine()
    path.write_text(
        json.dumps({"engine": stamp, "objective": objective_stamp(), "rows": rows}, indent=2, default=str),
        encoding="utf-8",
    )
    _CACHE["index_mtime"] = path.stat().st_mtime_ns
    _CACHE["index_path"] = str(path)
    _CACHE["rows"] = rows


def index_objective() -> str:
    """The objective the saved library rows were folded under."""
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("objective") or "") if isinstance(data, dict) else ""


def refold_if_needed() -> bool:
    """Fold the shelf again from the notes when it was built under an older objective.

    The run the objective picks can be a different attempt from the one the old shelf kept, so the notes
    are read again. The engine stamp is cleared so the rows are measured again on engine 4 before they
    are trusted.
    """
    from futuresfund.objective import stamp

    with _INDEX_LOCK:
        if not _index_path().is_file() or index_objective() == stamp():
            return False
    rows = _scan_notes()
    with _INDEX_LOCK:
        existing = _load_index_rows()
        _write_index(shelf_rows(list(rows) + existing), engine="")
    return True


def recheck_library(slot: int = 3) -> dict:
    """Measure every library row again on this engine, over the Strategy Tester range.

    A row that no longer makes money leaves the shelf. Runs on engine 4 when the desk is up.
    """
    from futuresfund.backtrader_engine import run_script
    from futuresfund.learn import _with_contract_point
    from futuresfund.objective import library_pace
    from futuresfund.research_bars import research_series
    from futuresfund.strategy import pine_text

    with _INDEX_LOCK:
        rows = _load_index_rows()
    frames: dict[tuple, list] = {}
    kept = []
    dropped = []
    tested_from = ""
    for row in rows:
        if row.get("kind") == "Added":
            kept.append(row)
            continue
        root = _RESEARCH_ROOT.get(str(row.get("contract") or ""), "ES")
        timeframe = str(row.get("timeframe") or "")
        pine = pine_text(str(row.get("file") or _pine_name(str(row.get("id") or ""))))
        if not pine or timeframe not in {"2m", "5m", "15m"}:
            dropped.append(row)
            continue
        key = (root, timeframe)
        if key not in frames:
            frames[key] = research_series(root, timeframe)
        params = _with_contract_point(dict(row.get("params") or {}), root)
        label = str(row.get("script") or row.get("title") or "script")
        measured = run_script(pine, frames[key], timeframe, params, None, root, label, slot) or {}
        if measured.get("error") or measured.get("fatal") or measured.get("blocked") or measured.get("net_profit") is None:
            dropped.append(row)
            continue
        tested_from = measured.get("tested_from") or tested_from
        updated = dict(row)
        for field in (
            "net_profit", "max_drawdown", "trades", "win_rate", "profit_factor", "profit_ratio",
            "sharpe", "biggest_winner", "biggest_loser", "tested_from", "days", "trades_per_day",
        ):
            updated[field] = measured.get(field)
        try:
            profit = float(updated.get("net_profit"))
        except (TypeError, ValueError):
            profit = 0.0
        if profit > 0 and library_pace(updated):
            kept.append(updated)
        else:
            dropped.append(updated)
    shown = shelf_rows(_library_rows(kept))
    with _INDEX_LOCK:
        _write_index(shown, engine=LIBRARY_ENGINE)
    return {"kept": len(shown), "dropped": len(dropped), "tested_from": tested_from}


def _scan_notes() -> list[dict]:
    collected: dict[str, dict] = {}
    seen = 0
    if _NOTES.is_dir():
        for path in _NOTES.glob("*.json"):
            if path.name in {"library_index.json", "strategy_cards.json", "creation_sources.json", "risk_suggestions.json"}:
                continue
            for row in _entries(path):
                collected[row["id"]] = row
                seen += 1
                if seen % 400 == 0:
                    time.sleep(0)
    return shelf_rows(_library_rows(list(collected.values())))


def _library_rows(rows: list[dict]) -> list[dict]:
    """One row per script on a contract and timeframe.

    A loss stays off the shelf. The profit, profit factor, win rate, and trade count
    are the numbers on the strategy library page.
    More than 600 trades a year stays off. The trailing-drawdown cap and a steadier
    run with less profit do not replace that run. A run near $50,000 profit and
    $22,000 drawdown is kept ahead of a larger profit whose ratio is worse.
    """
    from futuresfund.objective import firm_ratio, pace

    groups: dict[tuple[str, str, str], list[dict]] = {}
    added = []
    for row in rows:
        if row.get("kind") == "Added":
            added.append(dict(row))
            continue
        script = str(row.get("script") or row.get("title") or "")
        groups.setdefault((str(row.get("contract") or ""), str(row.get("timeframe") or ""), script), []).append(row)
    shown = []
    for items in groups.values():
        profitable = []
        for item in items:
            try:
                profit = float(item.get("net_profit"))
            except (TypeError, ValueError):
                continue
            if library_ready(item) and _clears_ratio(item) and _trade_loss_ok(item) and _account_survives(item):
                profitable.append(item)
        if not profitable:
            continue
        near = [item for item in profitable if firm_ratio(item)]
        pool = near or profitable
        richest = max(pool, key=lambda item: (float(item["net_profit"]), -float(item.get("max_drawdown") or 0)))
        row = dict(richest)
        row["primary"] = True
        row["target_met"] = True
        row["kind"] = "Near $50,000 profit" if near else "Most profitable"
        if row.get("trades_per_day") is None:
            row["trades_per_day"] = pace(richest)
        shown.append(row)
    shown.extend(added)
    return sorted(shown, key=lambda item: float(item.get("net_profit") or 0), reverse=True)


SHELF_PROFIT = 20_000.0
LIBRARY_MIN_TRADES = 100


def library_ready(row: dict | None) -> bool:
    """A result is filed when it clears the acceptance boxes that are turned on.

    Drawdown under the saved percent of the account and the trade count are on
    unless those boxes are unchecked. Once profit is above $3,000, that drawdown
    cap lifts and the profit has to be at least 1.5 times the drawdown. Profit,
    the profit factor, and the win rate count only when their boxes are checked.
    More than 600 trades a year stays off while the trade box is on.
    """
    from futuresfund.book import strategy_acceptance
    from futuresfund.objective import TRADES_YEAR_MAX, account_survives, profit_factor_of, trades_per_year, win_rate_percent

    if not isinstance(row, dict) or row.get("error") or row.get("fatal"):
        return False
    limits = strategy_acceptance()
    if limits.get("use_drawdown") and not account_survives(row):
        return False
    if limits.get("use_trades"):
        try:
            trades = int(float(row.get("trades") or 0))
        except (TypeError, ValueError):
            return False
        if trades < int(limits["min_trades"]):
            return False
        yearly = trades_per_year(row)
        if yearly is not None and yearly > TRADES_YEAR_MAX:
            return False
    if limits.get("use_profit"):
        try:
            if float(row.get("net_profit")) <= float(limits["min_profit"]):
                return False
        except (TypeError, ValueError):
            return False
    if limits.get("use_factor"):
        factor = profit_factor_of(row)
        if factor is None or factor <= float(limits["profit_factor"]):
            return False
    if limits.get("use_win_rate"):
        rate = win_rate_percent(row)
        if rate is None or rate <= float(limits["win_rate"]):
            return False
    return True


def strategy_family(row: dict) -> str:
    """The strategy name without the version, so v4 and v11 count as one strategy."""
    name = str(row.get("script") or row.get("title") or "")
    name = re.split(r"\s+[—–]\s+", name, maxsplit=1)[0]
    name = name.lower()
    name = re.sub(r"^engine4[_\s-]*", "", name)
    name = re.sub(r"(?:[_\s-]*v\d+)\b", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def _clears_ratio(row: dict) -> bool:
    """Average winner more than 1.12 times the average loser."""
    from futuresfund.objective import clears_profit_ratio

    return clears_profit_ratio(row)


def _trade_loss_ok(row: dict) -> bool:
    """A strategy with one trade down more than $1,500 is not included."""
    from futuresfund.objective import trade_loss_ok

    return trade_loss_ok(row)


def _account_survives(row: dict) -> bool:
    """Under $3,000 profit, drawdown stays under 5 percent. Above that, profit must be at least 1.5 times the drawdown."""
    from futuresfund.objective import account_survives

    return account_survives(row)


def _in_year_band(row: dict) -> bool:
    """True when the run has 130 to 600 trades a year. A row with no span is not rejected here."""
    from futuresfund.objective import in_trade_band, trades_per_year

    yearly = trades_per_year(row)
    if yearly is None:
        return True
    return in_trade_band(yearly)


def _shelf_key(row: dict) -> tuple[str, str, str]:
    """One slot per strategy name on a contract and timeframe."""
    return (strategy_family(row), str(row.get("contract") or ""), str(row.get("timeframe") or ""))


def shelf_rows(rows: list[dict]) -> list[dict]:
    """Rows worth showing: the profit, profit factor, win rate, and trade count on the strategy library page, a profit ratio above 1.12, and the best version of each name.

    The same name on another contract or timeframe stays. A strategy added by hand stays
    on the shelf even before it has a measured profit.
    """
    added = [dict(row) for row in rows if row.get("kind") == "Added"]
    rich = []
    for row in rows:
        if row.get("kind") == "Added":
            continue
        try:
            profit = float(row.get("net_profit"))
        except (TypeError, ValueError):
            continue
        if library_ready(row) and _clears_ratio(row) and _trade_loss_ok(row) and _account_survives(row):
            rich.append(row)
    shown = _pick_best(rich) + added
    return sorted(shown, key=lambda item: float(item.get("net_profit") or 0), reverse=True)


def _pick_best(rows: list[dict]) -> list[dict]:
    """One row per strategy name, contract, and timeframe. Higher profit wins a tie on drawdown."""
    best: dict[tuple, dict] = {}
    for row in rows:
        key = _shelf_key(row)
        current = best.get(key)
        if current is None:
            best[key] = row
            continue
        profit = float(row["net_profit"])
        current_profit = float(current["net_profit"])
        if profit > current_profit or (
            profit == current_profit and float(row.get("max_drawdown") or 0) < float(current.get("max_drawdown") or 0)
        ):
            best[key] = row
    return list(best.values())


_ADDED_DIR = Path(__file__).resolve().parent / "learnedStrategies"


def _added_slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")
    return (slug or "strategy")[:48]


def add_strategy(title: str, contract: str, pine: str, timeframe: str = "") -> dict:
    """File a script on the shelf. It stays until someone deletes it."""
    name = str(title or "").strip()
    source = str(pine or "").strip()
    if not name:
        raise ValueError("The strategy needs a name.")
    if "strategy(" not in source:
        raise ValueError("The script needs a strategy() line.")
    symbol, label = contract_of(name, "", contract or "ES1!")
    slug = _added_slug(name)
    digest = hashlib.sha1(f"{name}|{symbol}|{source}".encode()).hexdigest()[:12]
    filename = f"added_{slug}_{digest[:8]}.pine"
    _ADDED_DIR.mkdir(parents=True, exist_ok=True)
    path = _ADDED_DIR / filename
    path.write_text(source if source.endswith("\n") else source + "\n", encoding="utf-8")
    row = {
        "id": f"added_{slug}-{digest}",
        "title": name,
        "script": name,
        "file": filename,
        "contract": symbol,
        "contract_label": label,
        "timeframe": str(timeframe or "").strip(),
        "net_profit": None,
        "max_drawdown": None,
        "trades": None,
        "kind": "Added",
        "primary": True,
        "added_at": _filed_at(),
    }
    with _INDEX_LOCK:
        others = [item for item in _load_index_rows() if item.get("id") != row["id"]]
        _write_index(shelf_rows(others + [row]))
    record_untested(row)
    return row


def remove_strategy(strategy_id: str, contract: str | None = None) -> bool:
    """Take one strategy off the shelf and off the one-year list."""
    wanted = str(strategy_id or "").strip()
    if not wanted:
        return False

    def matches(row: dict) -> bool:
        if row.get("id") != wanted:
            return False
        return not contract or row.get("contract") == contract

    with _INDEX_LOCK:
        rows = _load_index_rows()
        removed = [row for row in rows if matches(row)]
        kept = [row for row in rows if not matches(row)]
        untested = _load_untested() or []
        removed_untested = [row for row in untested if matches(row)]
        kept_untested = [row for row in untested if not matches(row)]
        if not removed and not removed_untested:
            return False
        if removed:
            _write_index(kept)
        if removed_untested:
            _write_untested(kept_untested)
    for row in removed:
        if row.get("kind") != "Added":
            continue
        filename = str(row.get("file") or "")
        if not filename.startswith("added_") or not filename.endswith(".pine"):
            continue
        try:
            (_ADDED_DIR / filename).unlink(missing_ok=True)
        except OSError:
            pass
    return True


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
        "days": measured.get("days"),
        "trades_per_day": measured.get("trades_per_day"),
        "sharpe": measured.get("sharpe"),
        "profit_factor": best.get("profit_factor"),
        "profit_ratio": measured.get("profit_ratio") if measured.get("profit_ratio") is not None else best.get("profit_ratio"),
        "biggest_winner": measured.get("biggest_winner"),
        "biggest_loser": measured.get("biggest_loser"),
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
        "days": source.get("days", best.get("days")),
        "trades_per_day": source.get("trades_per_day", best.get("trades_per_day")),
        "sharpe": source.get("sharpe", best.get("sharpe")),
        "profit_ratio": source.get("profit_ratio", best.get("profit_ratio")),
        "biggest_winner": source.get("biggest_winner", best.get("biggest_winner")),
        "biggest_loser": source.get("biggest_loser", best.get("biggest_loser")),
    }


def _fold(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


_NOTE_SKIP = {
    "library_index.json",
    "strategy_cards.json",
    "creation_sources.json",
    "risk_suggestions.json",
    "kronos_patterns.json",
    "untested_library.json",
}


def _note_trials(data: dict) -> list[dict]:
    instruments = data.get("instruments")
    found: list[dict] = []
    if isinstance(instruments, dict) and instruments:
        for record in instruments.values():
            if not isinstance(record, dict):
                continue
            charts = record.get("charts")
            if isinstance(charts, dict) and charts:
                for chart in charts.values():
                    trials = chart.get("trials") if isinstance(chart, dict) else None
                    if isinstance(trials, list):
                        found.extend(item for item in trials if isinstance(item, dict))
                continue
            trials = record.get("trials")
            if isinstance(trials, list):
                found.extend(item for item in trials if isinstance(item, dict))
        if found:
            return found
    trials = data.get("trials")
    return [item for item in trials if isinstance(item, dict)] if isinstance(trials, list) else []


def _passed_entry(data: dict, path: Path, row: dict) -> dict | None:
    """A PineTS run that clears the acceptance boxes and can be filed."""
    if str(row.get("engine") or row.get("runner") or "") != "pinets":
        return None
    if row.get("error") or row.get("fatal"):
        return None
    from futuresfund.objective import consistent_spans, too_many_to_save

    if too_many_to_save(row) or not library_ready(row) or not _trade_loss_ok(row):
        return None
    filename = str(data.get("file") or f"{path.stem}.pine")
    created = Path(__file__).resolve().parent / "createdStrategies" / filename
    if created.is_file() and not consistent_spans(row.get("spans")):
        return None
    try:
        profit = float(row.get("net_profit"))
    except (TypeError, ValueError):
        return None
    title = str(data.get("title") or path.stem)
    change = str(row.get("change") or "").strip()
    shown = title
    if change and change not in {"Original", "Starting settings"} and not change.startswith("Starting measurement"):
        shown = f"{title} — {change}"
    instrument = str(row.get("instrument") or "ES").upper()
    explicit = str(row.get("contract") or ("GC1!" if instrument in {"QO", "GC", "MGC"} else f"{instrument}1!"))
    symbol, label = contract_of(title, path.name, explicit)
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    fingerprint = json.dumps(
        [symbol, row.get("timeframe"), params, profit, int(row.get("trades") or 0)],
        sort_keys=True,
        default=str,
    )
    entry = {
        "id": f"{path.stem}-{hashlib.sha1(fingerprint.encode()).hexdigest()[:12]}",
        "title": shown,
        "script": title,
        "file": filename,
        "contract": symbol,
        "contract_label": label,
        "timeframe": row.get("timeframe") or "",
        "net_profit": profit,
        "max_drawdown": row.get("max_drawdown"),
        "trades": int(row.get("trades") or 0),
        "win_rate": row.get("win_rate"),
        "sharpe": row.get("sharpe"),
        "profit_factor": row.get("profit_factor"),
        "profit_ratio": row.get("profit_ratio"),
        "biggest_winner": row.get("biggest_winner"),
        "biggest_loser": row.get("biggest_loser"),
        "days": row.get("days"),
        "trades_per_day": row.get("trades_per_day"),
        "params": params,
        "engine": "pinets",
        "runner": "pinets",
    }
    if not _has_script(entry) or not measured_on_one_year(entry):
        return None
    return entry


def recover_passed() -> dict:
    """Add PineTS runs that clear the acceptance boxes and were left off the waiting list.

    The most profitable list stays for a five-year test. One name keeps its highest
    passing profit on each contract and timeframe.
    """
    existing = _load_untested()
    if existing is None:
        return {"filed": [], "kept": 0}
    found: dict[tuple, dict] = {}
    if _NOTES.is_dir():
        for path in _NOTES.glob("*.json"):
            if path.name in _NOTE_SKIP or path.name.startswith("kronos"):
                continue
            try:
                if path.stat().st_size > 8_000_000:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            for row in _note_trials(data):
                entry = _passed_entry(data, path, row)
                if entry is None:
                    continue
                key = _shelf_key(entry)
                current = found.get(key)
                if current is None or float(entry["net_profit"]) > float(current["net_profit"]):
                    found[key] = entry
    shown = waiting_rows(list(existing) + list(found.values()))
    with _INDEX_LOCK:
        _write_untested(shown)
    kept_ids = {row.get("id") for row in shown}
    added = [row for row in found.values() if row.get("id") in kept_ids]
    return {
        "filed": [
            {
                "title": row.get("title"),
                "contract": row.get("contract"),
                "timeframe": row.get("timeframe"),
                "net_profit": row.get("net_profit"),
            }
            for row in sorted(added, key=lambda item: float(item.get("net_profit") or 0), reverse=True)
        ],
        "kept": len(shown),
    }
