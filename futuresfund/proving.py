"""Where each script stands on the 7, 30, 90 and 365 day backtests.

The ledger is researchNotes/proving/stages.json. One row is one script on one
contract and timeframe. Each stage a row passes keeps its numbers, and the
script with the passing inputs written in is saved under
researchNotes/proving/<days>_day/, so a passed version can be opened and copied.
Only the newest 100 rows are kept, with their saved scripts.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

STAGES = (7, 30, 90, 365)
LIMIT = 100
_LOCK = threading.Lock()
_METRICS = ("net_profit", "max_drawdown", "trades", "win_rate", "profit_factor", "biggest_loser", "held_loser", "stop_dollars", "spans")


def _home() -> Path:
    from futuresfund import learn

    return Path(learn.NOTES) / "proving"


def _ledger() -> Path:
    return _home() / "stages.json"


def _load() -> dict:
    try:
        data = json.loads(_ledger().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _ledger()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def _key(file: str, root: str, timeframe: str) -> str:
    return f"{file}|{root}|{timeframe}"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "script"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _pine(source: str, params: dict, root: str) -> str:
    from futuresfund.contracts import POINT_VALUE
    from futuresfund.library import _with_point_value
    from futuresfund.strategy import _apply_inputs

    text = source or ""
    if params:
        try:
            text = _apply_inputs(text, params)
        except (TypeError, ValueError, re.error):
            pass
    return _with_point_value(text, float(POINT_VALUE.get(root, 50)))


def _entry(data: dict, file: str, root: str, timeframe: str) -> dict:
    key = _key(file, root, timeframe)
    row = data.get(key)
    if not isinstance(row, dict):
        row = {"file": file, "root": root, "timeframe": timeframe, "passed": {}}
        data[key] = row
    row.setdefault("passed", {})
    return row


def _trim(data: dict) -> list[str]:
    """Drop the oldest rows past LIMIT, with their saved scripts, and return their keys.

    A script still running a stage stays, so its earlier passes are there when it finishes.
    """
    entries = [(key, entry) for key, entry in data.items() if isinstance(entry, dict)]
    extra = len(entries) - LIMIT
    if extra <= 0:
        return []
    settled = [item for item in entries if item[1].get("status") != "testing"]
    settled.sort(key=lambda item: str(item[1].get("updated") or ""))
    dropped = []
    for key, entry in settled[:extra]:
        del data[key]
        dropped.append(key)
        for passed in (entry.get("passed") or {}).values():
            pine = passed.get("pine") if isinstance(passed, dict) else None
            if pine:
                try:
                    (_home() / str(pine)).unlink(missing_ok=True)
                except OSError:
                    pass
    return dropped


def mark(file: str, root: str, timeframe: str, days: int, status: str, row: dict | None = None, source: str = "", reason: str = "") -> None:
    """Record that a script is testing, passed, failed, or stopped on the stage of `days`.

    A pass keeps the run's numbers and saves the script with its passing inputs.
    Starting the 7-day stage again clears the passes from an earlier study.
    A stopped stage ended without a result, and `reason` says why.
    """
    root = str(root or "ES").upper()
    with _LOCK:
        data = _load()
        entry = _entry(data, file, root, timeframe)
        if status == "testing" and int(days) == STAGES[0]:
            entry["passed"] = {}
        entry["stage"] = int(days)
        entry["status"] = status
        entry["updated"] = _now()
        if status == "stopped":
            entry["reason"] = reason or "The stage ended before it had a result."
        else:
            entry.pop("reason", None)
        if status == "passed":
            measured = row or {}
            params = dict(measured.get("params") or {})
            name = f"{_slug(Path(file).stem)}_{root}_{_slug(timeframe)}.pine"
            folder = _home() / f"{int(days)}_day"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(_pine(source, params, root), encoding="utf-8")
            entry["passed"][str(int(days))] = {
                **{key: measured.get(key) for key in _METRICS},
                "params": params,
                "pine": f"{int(days)}_day/{name}",
                "at": entry["updated"],
            }
        _trim(data)
        _write(data)


def stop_testing(reason: str, file: str | None = None, timeframe: str | None = None, since: str = "") -> list[str]:
    """Mark the stages still testing as stopped and return their keys.

    `file` and `timeframe` narrow it to one script on one chart, and `since` to
    stages marked at or after that time. With neither, every testing row stops:
    research is starting, so nothing is running them.
    """
    stopped = []
    with _LOCK:
        data = _load()
        for key, entry in data.items():
            if not isinstance(entry, dict) or entry.get("status") != "testing":
                continue
            if file is not None and entry.get("file") != file:
                continue
            if timeframe is not None and entry.get("timeframe") != timeframe:
                continue
            if since and str(entry.get("updated") or "") < since:
                continue
            entry["status"] = "stopped"
            entry["reason"] = reason
            entry["updated"] = _now()
            stopped.append(key)
        if stopped:
            _write(data)
    return stopped


def rows() -> list[dict]:
    """Every script in the ledger, newest change first, with a plain stage label."""
    with _LOCK:
        data = _load()
    out = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        stage = int(entry.get("stage") or 0)
        status = str(entry.get("status") or "")
        if status == "passed" and stage == STAGES[-1]:
            label = "Passed all four stages"
        elif status == "testing":
            label = f"Testing {stage} days"
        elif status == "failed":
            label = f"Failed {stage} days"
        elif status == "stopped":
            label = f"Stopped at {stage} days"
        else:
            label = f"Passed {stage} days"
        out.append({**entry, "id": key, "label": label})
    out.sort(key=lambda row: str(row.get("updated") or ""), reverse=True)
    return out


def passed_script(key: str, days: int) -> dict | None:
    """The saved script for one passed stage, ready to copy."""
    with _LOCK:
        entry = _load().get(key)
    if not isinstance(entry, dict):
        return None
    passed = (entry.get("passed") or {}).get(str(int(days)))
    if not isinstance(passed, dict) or not passed.get("pine"):
        return None
    path = _home() / str(passed["pine"])
    try:
        pine = path.read_text(encoding="utf-8")
    except OSError:
        return None
    title = f"{Path(str(entry.get('file') or 'script')).stem} on {entry.get('root')} {entry.get('timeframe')}, {int(days)}-day pass"
    return {"id": key, "title": title, "pine": pine}
