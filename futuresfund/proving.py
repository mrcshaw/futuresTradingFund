"""Where each script stands on the 7, 30, 90 and 365 day backtests.

The ledger is researchNotes/proving/stages.json. One row is one script on one
contract and timeframe. Each stage a row passes keeps its numbers, and the
script with the passing inputs written in is saved under
researchNotes/proving/<days>_day/, so a passed version can be opened and copied.
Only the newest 100 rows are kept, with their saved scripts.

A stage that stopped without a result because the desk or research restarted, the engine's Restart
was pressed, or the engine or search stopped on an error is resumable: its row has resume: true, the
chart counts as pending again, and the next study starts at that stage from the pass before it. Skip
pressed by the owner stays a skip. A stop that was the script's or the engine's (Restart, an engine
fault, an error) counts in entry["resumes"], and the stage resumes after at most MAX_RESUMES of them.
A desk restart or a research Stop is not the script's fault: it counts in entry["interruptions"]
instead, with the larger cap MAX_INTERRUPTIONS, so a script that brings the whole desk down cannot
come back forever.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

STAGES = (7, 30, 90, 365)
LIMIT = 100
# A stage that stopped through the script's or the engine's fault (Restart, an engine fault, an error)
# resumes at most this many times. After that its row stays stopped, so a script that always errors
# does not come back forever.
MAX_RESUMES = 3
# A stage cut off by a desk restart or a research Stop resumes at most this many times. Those are not
# the script's fault, so the cap is far larger; it only ends a script that brings the whole desk down.
MAX_INTERRUPTIONS = 10
# A resumable row whose file never came back (moved by hand, parked from another chart) stops holding
# its place in the ledger after this many days, so the ledger stays at LIMIT rows.
RESUME_KEEP_DAYS = 14
# Seconds between the cheap checks of the ledger's file time behind resumable_roots. The desk is the
# only writer, and its own writes refresh the cache at once.
CACHE_CHECK = 30.0
# Rows written before a stopped row recorded resume: these reasons were cut-offs, not results. The old
# Restart button ended a study exactly like Skip, so its rows say "It was skipped on this chart."
_LEGACY_RESUME_REASONS = (
    "Research stopped or the desk restarted before this stage finished.",
    "Research was stopped before this stage finished.",
    "The search stopped on an error.",
    "The search ended before this stage had a result.",
    "It was skipped on this chart.",
)
_LOCK = threading.Lock()
_METRICS = (
    "net_profit", "max_drawdown", "trades", "win_rate", "profit_factor", "biggest_loser", "held_loser", "stop_dollars", "spans",
    # The attempts the stage used, the attempts it had, and which attempt was its best pass.
    "attempts_used", "attempt_budget", "best_attempt",
)


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
    _remember(path, data)


# The resumable rows by (file, timeframe) -> {root: stage}, so a folder scan never reads the ledger.
_CACHE_LOCK = threading.Lock()
_cache: dict = {"path": None, "stamp": None, "checked": 0.0, "index": {}}


def _stamp(path: Path):
    try:
        found = path.stat()
    except OSError:
        return None
    return (found.st_mtime_ns, found.st_size)


def _index_of(data: dict) -> dict:
    index: dict = {}
    for entry in data.values():
        if not isinstance(entry, dict) or not resumable(entry):
            continue
        key = (str(entry.get("file") or ""), str(entry.get("timeframe") or ""))
        index.setdefault(key, {})[str(entry.get("root") or "").upper()] = int(entry.get("stage") or 0)
    return index


def _remember(path: Path, data: dict) -> None:
    """Refresh the cache from a ledger this process just wrote."""
    index = _index_of(data)
    with _CACHE_LOCK:
        _cache.update(path=str(path), stamp=_stamp(path), checked=time.monotonic(), index=index)


def _resume_index() -> dict:
    """The cached resumable rows. The file time is checked at most every CACHE_CHECK seconds."""
    path = _ledger()
    now = time.monotonic()
    with _CACHE_LOCK:
        if _cache["path"] == str(path) and now - float(_cache["checked"]) < CACHE_CHECK:
            return _cache["index"]
    with _LOCK:
        # Another thread may have checked the file while this one waited for the lock: one stat is enough.
        with _CACHE_LOCK:
            if _cache["path"] == str(path) and time.monotonic() - float(_cache["checked"]) < CACHE_CHECK:
                return _cache["index"]
        # Under the ledger lock, so a write cannot land between the file time and the read.
        stamp = _stamp(path)
        with _CACHE_LOCK:
            if _cache["path"] == str(path) and stamp == _cache["stamp"]:
                _cache["checked"] = now
                return _cache["index"]
        index = _index_of(_load())
        with _CACHE_LOCK:
            _cache.update(path=str(path), stamp=stamp, checked=now, index=index)
    return index


def resumable(entry: dict | None) -> bool:
    """True when this row's stage stopped without a result and resumes there on its next study."""
    if not isinstance(entry, dict) or entry.get("status") != "stopped":
        return False
    if "resume" in entry:
        return entry.get("resume") is True
    reason = str(entry.get("reason") or "")
    return reason in _LEGACY_RESUME_REASONS or reason.startswith("The engine stopped.")


def resumable_roots(file: str, timeframe: str) -> dict:
    """{root: stage} for this file's resumable rows on this chart, from the in-process cache."""
    found = _resume_index().get((str(file), str(timeframe)))
    return dict(found) if found else {}


def resume_point(file: str, root: str, timeframe: str) -> dict | None:
    """Where a resumable row stopped: {"stage", "passed", "attempts", "resumes"}, or None. Read fresh, once per study."""
    root = str(root or "ES").upper()
    with _LOCK:
        entry = _load().get(_key(file, root, timeframe))
    if not resumable(entry):
        return None
    return {
        "stage": int(entry.get("stage") or 0),
        "passed": dict(entry.get("passed") or {}),
        "attempts": dict(entry.get("attempts") or {}) if isinstance(entry.get("attempts"), dict) else {},
        "resumes": int(entry.get("resumes") or 0),
    }


def drop_resume(file: str) -> list[str]:
    """This file's stopped stages no longer resume, as when an answer sends the script back to every chart."""
    dropped = []
    with _LOCK:
        data = _load()
        for key, entry in data.items():
            if isinstance(entry, dict) and entry.get("file") == file and resumable(entry):
                entry["resume"] = False
                dropped.append(key)
        if dropped:
            _write(data)
    return dropped


def _settle_stop(entry: dict, reason: str, resume: bool, counted: bool = True) -> None:
    """Mark this row stopped, and count a resumable stop when it is made, not when the stage starts again.

    A counted stop (Restart, an engine fault, an error: the script's or the engine's fault) adds one to
    entry["resumes"]; past MAX_RESUMES the row stays stopped, and the reason says so. An uncounted stop
    (a desk restart or a research Stop) adds one to entry["interruptions"] instead, capped at
    MAX_INTERRUPTIONS.
    """
    entry["status"] = "stopped"
    entry["updated"] = _now()
    stage = int(entry.get("stage") or 0)
    if resume:
        name, cap = ("resumes", MAX_RESUMES) if counted else ("interruptions", MAX_INTERRUPTIONS)
        used = int(entry.get(name) or 0)
        if used >= cap:
            entry["resume"] = False
            entry["reason"] = (
                f"{reason} It already resumed {cap} times at the {stage}-day stage, so it stays stopped."
                if counted else
                f"{reason} It was already cut off {cap} times at the {stage}-day stage, so it stays stopped."
            )
            return
        entry[name] = used + 1
    entry["resume"] = bool(resume)
    entry["reason"] = reason


def _key(file: str, root: str, timeframe: str) -> str:
    return f"{file}|{root}|{timeframe}"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "script"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _days_before(days: int) -> str:
    """The ledger time `days` days before _now(), in the same format, so the two compare as text."""
    try:
        now = time.mktime(time.strptime(_now(), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - int(days) * 86400))


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

    A script still running a stage stays, so its earlier passes are there when it finishes. So does a
    stopped stage that resumes, so its passes are there when it starts again, unless it waited more than
    RESUME_KEEP_DAYS: its file left the queue (moved by hand, parked from another chart), so it never will.
    """
    entries = [(key, entry) for key, entry in data.items() if isinstance(entry, dict)]
    extra = len(entries) - LIMIT
    if extra <= 0:
        return []
    stale = _days_before(RESUME_KEEP_DAYS)

    def waiting(entry: dict) -> bool:
        return resumable(entry) and str(entry.get("updated") or "") >= stale

    settled = [item for item in entries if item[1].get("status") != "testing" and not waiting(item[1])]
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


def _attempts(row: dict | None) -> dict:
    """{"used": n, "budget": m} from a row's attempts_used and attempt_budget. Used is None while the stage is testing."""
    if not isinstance(row, dict):
        return {}
    budget, used = row.get("attempt_budget"), row.get("attempts_used")
    if budget is None and used is None:
        return {}
    try:
        return {
            "used": None if used is None else int(used),
            "budget": None if budget is None else int(budget),
        }
    except (TypeError, ValueError):
        return {}


def mark(file: str, root: str, timeframe: str, days: int, status: str, row: dict | None = None, source: str = "", reason: str = "", fresh: bool = False, resumed: bool = False, resume: bool = False, counted: bool = True) -> None:
    """Record that a script is testing, passed, failed, or stopped on the stage of `days`.

    A pass keeps the run's numbers and saves the script with its passing inputs.
    Starting the 7-day stage again clears the passes from an earlier study. So does `fresh`, which
    research passes for the first stage that runs when the owner turned the 7-day stage off.
    `resumed` marks a stopped stage starting again where it stopped: the passes before it stay, and
    so do its counts of resumes and interruptions (which the stop that made it resumable already
    counted). A passed or failed stage resets those counts.
    A stopped stage ended without a result, and `reason` says why. With `resume` it resumes there;
    `counted` says whether the stop was the script's or the engine's (see _settle_stop).
    A row with attempt_budget (and attempts_used) records the attempts that stage had (and used) under
    entry["attempts"][days], for any status, so a testing or failed stage shows them too.
    """
    root = str(root or "ES").upper()
    with _LOCK:
        data = _load()
        entry = _entry(data, file, root, timeframe)
        if status == "testing" and (int(days) == STAGES[0] or fresh):
            entry["passed"] = {}
            entry["attempts"] = {}
            if not resumed:
                entry.pop("resumes", None)
                entry.pop("interruptions", None)
        spent = _attempts(row)
        if spent:
            attempts = entry.get("attempts") if isinstance(entry.get("attempts"), dict) else {}
            attempts[str(int(days))] = spent
            entry["attempts"] = attempts
        entry["stage"] = int(days)
        if status in ("passed", "failed"):
            entry.pop("resumes", None)
            entry.pop("interruptions", None)
        if status == "stopped":
            _settle_stop(entry, reason or "The stage ended before it had a result.", resume, counted)
        else:
            entry["status"] = status
            entry["updated"] = _now()
            entry.pop("reason", None)
            entry.pop("resume", None)
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


def stop_testing(reason: str, file: str | None = None, timeframe: str | None = None, since: str = "", root: str | None = None, resume: bool = False, counted: bool = True) -> list[str]:
    """Mark the stages still testing as stopped and return their keys.

    `file`, `timeframe` and `root` narrow it to one script on one instrument's chart, and
    `since` to stages marked at or after that time. Each instrument has its own engines, so one
    instrument's study ending does not stop another instrument's stage on the same file and
    chart. With none of them, every testing row stops: research is starting, so nothing is
    running them. With `resume` the stopped stages resume where they stopped: up to MAX_RESUMES
    times for a `counted` stop (the script's or the engine's fault), and up to MAX_INTERRUPTIONS
    times for one that is not (a desk restart or a research Stop).
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
            if root is not None and str(entry.get("root") or "").upper() != str(root).upper():
                continue
            if since and str(entry.get("updated") or "") < since:
                continue
            _settle_stop(entry, reason, resume, counted)
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
        elif status == "stopped" and resumable(entry):
            label = f"Stopped at {stage} days, resumes there"
        elif status == "stopped":
            label = f"Stopped at {stage} days"
        else:
            label = f"Passed {stage} days"
        out.append({**entry, "id": key, "label": label, "resume": resumable(entry)})
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
