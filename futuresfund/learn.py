"""Study the Pine files on disk. Adjust parameters on the engine, keep the notes, then file the script as learned."""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from queue import Empty, PriorityQueue

from futuresfund import input_effects
from futuresfund.pine_report import pine_facts
ROOT = Path(__file__).resolve().parent
LEARNING = ROOT / "learningStrategies"
LEARNED = ROOT / "learnedStrategies"
CREATED = ROOT / "createdStrategies"
# A new script waits here while engine 4 develops it. It enters createdStrategies only after one run clears the acceptance.
DRAFTS = ROOT / "draftStrategies"
# A created script that never had a run clear the acceptance. Kept, never studied again.
UNPROVEN = ROOT / "unprovenStrategies"
CANNOT_RUN = ROOT.parent / "cannotRunStrategies"
# A script whose researcher asked the owner a blocking question waits here (questions.py), next to cannotRunStrategies.
AWAITING = ROOT.parent / "awaitingAnswerStrategies"
NOTES = ROOT / "researchNotes"
ENGINE_ROOT = ROOT.parent.parent / "tradingEngine"
ENGINE_CHART = "5m"
RESEARCH_CHARTS = ("5m", "2m", "15m")
ATTEMPT_LIMIT = 300       # different results the one-year search gets on one chart (learn.stage_attempts(None))
LOSS_LOOK = 40           # chart engines leave a script whose best result is still a loss after this many different results
CREATED_PROFIT_ATTEMPTS = 20  # the researcher who wrote a script scraps it after this many results with no profit
SUCCESS_COUNT = 10        # stop after this many counted runs: a profit with the drawdown inside the pro account rule
FLAT_RUNS = 20            # this many no-profit, no-trade results in a row, and the researcher asks the owner (awaitingAnswerStrategies)
IMPACT_PERCENT = 0.05     # a profit drop of this share, or more, ends a climb on that input
ACCOUNT_SIZE = 50000
PROFIT_TARGET = 3000.0
HOLD_PATH = Path.home() / ".futuresfund" / "hold_new.json"
FROZEN = {
    "initial_capital",
    "point_value",
    "pv",
    "pointvalue",
    "tick_size",
    "commission_value",
    "slippage",
    "default_qty_value",
    "pyramiding",
    "trailing_drawdown_limit",
    "profit_target",
}
_FILTERS: dict = {}
_notes_lock = threading.Lock()


def candidates(base: dict, limit: int = ATTEMPT_LIMIT) -> list[dict]:
    """Original settings first, then one change at a time, never past the attempt cap."""
    origin = {key: value for key, value in base.items() if _tunable_value(value)}
    plans = [dict(origin)]
    seen = {_key(origin)}
    for name, value in origin.items():
        if name in FROZEN:
            continue
        for nxt in neighbors(name, value):
            if len(plans) >= limit:
                return plans
            plan = dict(origin)
            plan[name] = nxt
            mark = _key(plan)
            if mark in seen:
                continue
            seen.add(mark)
            plans.append(plan)
    return plans


def neighbors(name: str, value):
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int) and not isinstance(value, bool):
        raw = [int(value * scale) for scale in (0.5, 0.75, 1.25, 1.5, 2)]
        raw += [value - 2, value - 1, value + 1, value + 2]
        out = []
        for item in raw:
            item = _clamp(name, item)
            if item != value and item not in out and item > 0:
                out.append(item)
        return out
    if isinstance(value, float):
        out = []
        for scale in (0.5, 0.75, 1.25, 1.5, 2):
            item = round(value * scale, 4)
            if item != value and item not in out and item > 0:
                out.append(item)
        return out
    return []


def research_slate(limit: int = 3) -> list[dict]:
    """The strategy being studied and the next ones waiting. No attempt log."""
    from futuresfund.charts import charts_have_volume
    from futuresfund.pine_report import pine_facts

    jobs = queue(skip_volume=not charts_have_volume())
    slate = []
    for path in jobs[:limit]:
        title = path.stem
        try:
            found = pine_facts(path.read_text(encoding="utf-8", errors="replace")).get("title") or ""
            if found and found != "Untitled strategy":
                title = found
        except OSError:
            pass
        slate.append({"title": title, "file": path.name})
    return slate


def tested_instruments(notes: dict) -> set[str]:
    """Instruments this strategy has already been run on. An older note was an ES test."""
    recorded = notes.get("instruments") if isinstance(notes, dict) else None
    if isinstance(recorded, dict) and recorded:
        return {str(name).upper() for name, record in recorded.items() if _instrument_finished(record)}
    if isinstance(notes, dict) and (notes.get("trials") or isinstance(notes.get("best"), dict) and notes.get("best")):
        return {"ES"}
    return set()


def pending_for_chart(notes: dict, timeframe: str, instruments: list[str] | None = None) -> list[str]:
    """Instruments that still need this one chart. A finished chart is not run again."""
    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    return [root for root in roots if not _chart_finished(notes or {}, root, timeframe)]


def pending_instruments(notes: dict, instruments: list[str] | None = None) -> list[str]:
    """Instruments that still need a run. One that is already listed is not tested again."""
    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    done = tested_instruments(notes or {})
    return [root for root in roots if root not in done]


def ready_to_file(notes: dict, instruments: list[str] | None = None) -> bool:
    """A script moves to learned only after every chart instrument has a result."""
    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    return bool(roots) and not pending_instruments(notes or {}, roots)


def research_instruments() -> list[str]:
    """ES, gold, Nasdaq, Micro Nasdaq, and Micro S&P, then every other chart folder, such as M2K and YM.

    Gold is GC at $100 a point when that download is on disk, and QO at $50
    when it is not. MNQ is $2 a point and joins once its minute file is on disk.
    MES, M2K, and YM are $5 a point. NQ stays in the list even before its download arrives.
    """
    from futuresfund.charts import chart_roots

    present = chart_roots()
    gold = "GC" if "GC" in present else "QO"
    ordered = [name for name in ("ES", gold, "NQ", "MNQ", "MES") if name in present or name == "NQ"]
    skip = {"QO", "MGC"} if gold == "GC" else set()
    ordered.extend(name for name in present if name not in ordered and name not in skip)
    return ordered


def _instrument_finished(record) -> bool:
    """An instrument is finished when the 2-minute, 5-minute, and 15-minute charts have each been run."""
    if not isinstance(record, dict):
        return False
    charts = record.get("charts")
    if isinstance(charts, dict):
        return all(_attempted(charts.get(name)) for name in RESEARCH_CHARTS)
    return _attempted(record)


def _chart_finished(notes: dict, root: str, timeframe: str) -> bool:
    recorded = notes.get("instruments") if isinstance(notes, dict) else None
    if isinstance(recorded, dict) and recorded:
        record = recorded.get(root)
        if not isinstance(record, dict):
            return False
        charts = record.get("charts")
        if isinstance(charts, dict):
            return _attempted(charts.get(timeframe))
        return _attempted(record)
    if root == "ES" and isinstance(notes, dict) and (notes.get("trials") or isinstance(notes.get("best"), dict) and notes.get("best")):
        return _attempted({
            "trials": notes.get("trials"),
            "best": notes.get("best"),
            "error": notes.get("error"),
            "blocked": notes.get("blocked"),
        })
    return False


def _attempted(record) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("blocked") or record.get("error"):
        return False
    best = record.get("best")
    if isinstance(best, dict) and (best.get("error") or best.get("fatal")):
        return False
    rows = [row for row in (record.get("trials") or []) if isinstance(row, dict)]
    best = record.get("best") if isinstance(record.get("best"), dict) else {}
    if record.get("skipped") or best.get("skipped") or any(row.get("skipped") for row in rows):
        return bool(rows or best or record.get("attempts"))
    # A chart that never entered was not really studied. The developer runs it again and then skips it if it still cannot.
    if rows and all("trades" in row and int(row.get("trades") or 0) == 0 and not row.get("error") for row in rows):
        return False
    if record.get("trials") or record.get("attempts"):
        return True
    return bool(best)


_copy_cache: dict[str, tuple[float, dict]] = {}


def _copy_index(folder: Path) -> dict[str, list[tuple[Path, dict]]]:
    """Other copies of a script in this folder, grouped by name and kept for a moment."""
    now = time.monotonic()
    key = f"{folder}|{NOTES}"
    with _cache_lock:
        hit = _copy_cache.get(key)
        if hit is not None and now - hit[0] < 2:
            return hit[1]
    index: dict[str, list[tuple[Path, dict]]] = {}
    for other in _strategy_files(folder):
        note = NOTES / f"{other.stem.replace(' ', '_')}.json"
        if not note.is_file():
            continue
        stem = " ".join(other.stem.split()).casefold()
        try:
            resolved = other.resolve()
        except OSError:
            resolved = other
        index.setdefault(stem, []).append((resolved, _note_view(other)))
    with _cache_lock:
        _copy_cache[key] = (now, index)
    return index


def _copy_finished(path: Path, timeframe: str, root: str) -> bool:
    """True when another file of the same script already finished this chart."""
    key = " ".join(path.stem.split()).casefold()
    try:
        mine = path.resolve()
    except OSError:
        mine = path
    folder = path.parent
    if not folder.is_dir():
        return False
    for resolved, notes in _copy_index(folder).get(key, []):
        if resolved == mine:
            continue
        if _chart_finished(notes, root, timeframe):
            return True
    return False


_view_lock = threading.Lock()
_parse_gate = threading.Lock()
_view_cache: dict[str, tuple[int, dict]] = {}
_progress_lock = threading.Lock()
_progress_state: dict = {"rows": None, "at": 0.0, "running": False}


def _slim_trial(row: dict) -> dict:
    """The fields that decide whether a chart was really studied."""
    item = {}
    if "trades" in row:
        item["trades"] = row.get("trades")
    if row.get("error"):
        item["error"] = row.get("error")
    if row.get("skipped"):
        item["skipped"] = True
    return item


def _slim_best(best):
    if not isinstance(best, dict) or not best:
        return best if isinstance(best, dict) else None
    return {
        "error": best.get("error"),
        "fatal": best.get("fatal"),
        "skipped": best.get("skipped"),
        "net_profit": best.get("net_profit"),
        "trades": best.get("trades"),
    }


def _slim_record(record: dict) -> dict:
    slim = {}
    for key in ("blocked", "error", "skipped", "attempts"):
        if key in record:
            slim[key] = record.get(key)
    if isinstance(record.get("trials"), list):
        slim["trials"] = [_slim_trial(row) for row in record["trials"] if isinstance(row, dict)]
    if "best" in record:
        slim["best"] = _slim_best(record.get("best"))
    charts = record.get("charts")
    if isinstance(charts, dict):
        slim["charts"] = {name: _slim_record(chart) if isinstance(chart, dict) else chart for name, chart in charts.items()}
    return slim


def _slim_notes(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    slim = _slim_record(data)
    instruments = data.get("instruments")
    if isinstance(instruments, dict):
        slim["instruments"] = {
            str(root): _slim_record(record) if isinstance(record, dict) else record
            for root, record in instruments.items()
        }
    return slim


def _note_view(path: Path) -> dict:
    """A small copy of the note, cached by file time, so a finished chart is not parsed again."""
    slug = path.stem.replace(" ", "_")
    file = NOTES / f"{slug}.json"
    try:
        stamp = file.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(file)
    with _view_lock:
        hit = _view_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    with _parse_gate:
        with _view_lock:
            hit = _view_cache.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
        data = _read_notes(path)
        try:
            stamp = file.stat().st_mtime_ns
        except OSError:
            stamp = 0
        view = _slim_notes(data)
    with _view_lock:
        _view_cache[key] = (stamp, view)
    return view


def _stem_key(path: Path) -> str:
    return " ".join(path.stem.split()).casefold()


def _folder_views(folder: Path) -> dict[str, list[tuple[Path, dict]]]:
    """One read of the folder. The key is the script name, so a second copy can see a finished chart."""
    grouped: dict[str, list[tuple[Path, dict]]] = {}
    if not folder.is_dir():
        return grouped
    try:
        names = list(folder.iterdir())
    except OSError:
        return grouped
    for other in names:
        if not _is_strategy_file(other):
            continue
        try:
            resolved = other.resolve()
        except OSError:
            resolved = other
        grouped.setdefault(_stem_key(other), []).append((resolved, _note_view(other)))
    return grouped


def _resumable_roots(path: Path, timeframe: str) -> dict:
    """{root: stage} for this file's stopped stages on this chart that resume there (proving ledger).

    The ledger is cached in this process (proving.resumable_roots), so a folder scan reads no file for it.
    """
    try:
        from futuresfund.proving import resumable_roots

        return resumable_roots(path.name, timeframe)
    except Exception:
        return {}


def _pending_roots(path: Path, timeframe: str, notes: dict) -> list[str]:
    """pending_for_chart, plus an instrument whose stage on this chart stopped and resumes there.

    A study cut off by a restart or an error already wrote its attempts to the notes, so the notes
    count that chart as finished. The ledger row that resumes keeps it pending until it finishes.
    """
    again = _resumable_roots(path, timeframe)
    if not again:
        return pending_for_chart(notes, timeframe)
    roots = research_instruments()
    pending = set(pending_for_chart(notes, timeframe, roots))
    return [root for root in roots if root in pending or root in again]


# The (file name, root, timeframe) of every study running a resumed stage. Its ledger row reads "testing"
# from the stage's first mark, so only this keeps the file from being filed until that study ends.
_resuming_lock = threading.Lock()
_resuming: set[tuple[str, str, str]] = set()


def _begin_resuming(name: str, root, timeframe: str) -> None:
    key = (str(name), str(root or "").upper(), str(timeframe or ""))
    with _resuming_lock:
        _resuming.add(key)
    _stage_end.resuming = key


def _end_resuming(name: str, root, timeframe: str) -> None:
    """The study of this chart ended (the key _prove_stages added on this thread goes too)."""
    mine = getattr(_stage_end, "resuming", None)
    _stage_end.resuming = None
    with _resuming_lock:
        _resuming.discard((str(name), str(root or "").upper(), str(timeframe or "")))
        if mine is not None:
            _resuming.discard(mine)


def _resume_pending(path: Path) -> bool:
    """True while one of this file's charts has a stopped stage that resumes, or a study is running one,
    so the file is not filed yet. Reads only memory: the set above and proving's cache.

    The resumed study on this thread does not hold itself back: when it reaches its own filing check,
    its chart is done (its year is marked), so only the other charts' resumed studies count.
    """
    mine = getattr(_stage_end, "resuming", None)
    with _resuming_lock:
        if any(item[0] == path.name and item != mine for item in _resuming):
            return True
    found = set()
    for timeframe in RESEARCH_CHARTS:
        found |= set(_resumable_roots(path, timeframe))
    return bool(found and found & set(research_instruments()))


def _open_roots(path: Path, timeframe: str, views: dict | None = None) -> list[str]:
    """Instruments this file still needs on this chart, ignoring a copy that already finished them.

    A stage of this file that stopped and resumes stays open even when a copy finished that chart.
    """
    pending = _pending_roots(path, timeframe, _note_view(path))
    again = _resumable_roots(path, timeframe) if pending else {}
    if views is None:
        return [root for root in pending if root in again or not _copy_finished(path, timeframe, root)]
    try:
        mine = path.resolve()
    except OSError:
        mine = path
    copies = views.get(_stem_key(path), [])

    def covered(root: str) -> bool:
        for other, view in copies:
            if other == mine:
                continue
            if _chart_finished(view, root, timeframe):
                return True
        return False

    return [root for root in pending if root in again or not covered(root)]


_held_lock = threading.Lock()
# Keys are (file, timeframe, root). A blank timeframe and root hold the file on every chart of every
# instrument: a compile failure is the file's, not one chart's.
_held_back: set[tuple[str, str, str]] = set()
_engine_parked: set[tuple[str, str, str]] = set()


def clear_holds() -> None:
    with _held_lock:
        _held_back.clear()
        _engine_parked.clear()


def _hold_key(path: Path | str, timeframe: str = "", root: str = "") -> tuple[str, str, str]:
    return (str(path), str(timeframe or ""), str(root or "").upper())


def is_held(held, path: Path | str, timeframe: str, root: str | None = None) -> bool:
    """True when this file is held on this instrument's chart: by its own key, its chart's, or the whole file's.

    held is a set of (file, timeframe) or (file, timeframe, root) keys. Without a root any hold on the chart counts.
    """
    if not held:
        return False
    key = str(path)
    if (key, timeframe) in held or (key, timeframe, "") in held or (key, "", "") in held:
        return True
    if root is None:
        return any(isinstance(item, tuple) and len(item) == 3 and item[0] == key and item[1] == timeframe for item in held)
    return (key, timeframe, str(root).upper()) in held


def hold_script(path: Path | str, timeframe: str = "", root: str = "") -> bool:
    """Leave this script out of the queue until it is released or the run ends. True when the hold is new.

    A timeframe and root hold it on one instrument's chart; leave them blank to hold it on every chart.
    """
    key = _hold_key(path, timeframe, root)
    with _held_lock:
        new = key not in _held_back
        _held_back.add(key)
        return new


def park_engine_script(path: Path | str, timeframe: str = "", root: str = "") -> None:
    """A script PineTS could not finish stays out of that instrument's chart queue for this research run."""
    key = _hold_key(path, timeframe, root)
    with _held_lock:
        _held_back.add(key)
        _engine_parked.add(key)


def release_script(path: Path | str, timeframe: str = "", root: str = "") -> None:
    """The chart (or, with a blank timeframe and root, every chart) may study this script again."""
    with _held_lock:
        _held_back.discard(_hold_key(path, timeframe, root))


_repair_gate = threading.Lock()
# One repair per file, whichever engine found the compile error. Keyed by str(path).
_repairs: dict[str, dict] = {}


# Developer repairs of a script that does not compile, before its researcher asks the owner (awaitingAnswerStrategies).
# REPAIR_TRIES further down is a different count: the researcher's repair asks on a script that runs.
COMPILE_REPAIR_TRIES = 3
_repair_tries: dict[str, int] = {}


def compile_problem(error) -> bool:
    """True when the script failed to compile. That is a code fix, not a stopped engine."""
    text = str(error or "").lower()
    return any(phrase in text for phrase in (
        "did not compile",
        "cannot compile",
        "failed to transpile",
        "defval",
        "minval",
        "maxval",
        "undeclared",
        "unknown name",
        "not supported",
        "syntax error",
    ))


def send_to_repair(board, path: Path, developer: str, timeframe: str, source: str, error: str, slot: int, researcher: str = "") -> str:
    """Repair a script that does not compile. One that still fails after COMPILE_REPAIR_TRIES waits for the owner's answer."""
    if _repair_tries.get(str(path), 0) >= COMPILE_REPAIR_TRIES:
        _repair_tries.pop(str(path), None)
        tried = f"{COMPILE_REPAIR_TRIES} repairs did not make it compile." if COMPILE_REPAIR_TRIES else "Compile repairs are turned off, so it was not sent for a repair."
        return _ask_owner(board, path, researcher or developer, f"{tried} {error}", "compile", developer)
    start_compile_repair(board, path, developer, timeframe, source, error, slot, researcher)
    return "repairing"


def start_compile_repair(board, path: Path, developer: str, timeframe: str, source: str, error: str, slot: int, researcher: str = "") -> None:
    """The developer fixes the script and the creation tester validates it. The engine does not wait."""
    _stage_end.reason = f"It went to the developer for a repair. {error}"
    from futuresfund.backtrader_engine import set_engine_notice

    set_engine_notice(slot, "")
    if board is not None:
        board.post(
            developer,
            f"{path.name} did not compile. {error} "
            f"The {developer} is fixing it, and the creation tester will validate the change. "
            "This engine is taking the next script.",
            kind="log",
            channel=developer,
        )
    # The researcher emails the error while the repair runs. A fixed script or a note from the owner is used.
    _ask_owner(
        board, path, researcher or developer,
        f"{path.name} did not compile: {error} The {developer} is repairing it now. A fixed script or a hint helps the next try.",
        "repair", developer,
    )
    with _repair_gate:
        current = _repairs.get(str(path))
        if current and current.get("thread") is not None and current["thread"].is_alive():
            return

    def work() -> None:
        from futuresfund.attempt_reason import keep_original
        from futuresfund.questions import repair_hint
        from futuresfund.strategy import _legal_defaults
        from futuresfund.strategy_team import CREATION_TESTER, compile_error, engine_problem, repair_for_pine, repair_with_developer

        _repair_tries[str(path)] = _repair_tries.get(str(path), 0) + 1
        # The file as the repair found it. A script the owner emails meanwhile replaces the file, and then
        # this repair of the old text is dropped instead of written over the owner's fix.
        try:
            started = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            started = None
        text = source or ""
        fixed, _notes = repair_for_pine(text)
        if fixed:
            text = fixed
        problem = compile_error(text)
        if problem:
            # The owner's emailed note for this repair, when one came in.
            hint = repair_hint(path)
            rewritten = repair_with_developer(board, developer, text, f"{problem} {hint}".strip())
            if rewritten:
                text = rewritten
        text = _legal_defaults(text)
        # Validated means TradingView's rules hold and the engine runs it, not only that the desk's own parser took it.
        problem = compile_error(text) or engine_problem(text)
        ready = not problem
        replaced = False
        if ready:
            try:
                now = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
                if started is not None and now != started:
                    replaced = True
                    ready = False
                else:
                    keep_original(path)
                    path.write_text(text, encoding="utf-8")
            except OSError:
                ready = False
                problem = "The repaired script could not be saved."
        if replaced:
            if board is not None:
                board.post(
                    CREATION_TESTER,
                    f"{path.name} changed while the {developer} was repairing it, as when the owner sends a fixed script. "
                    "The repair of the old script is dropped, and the new one stays as it is.",
                    kind="log",
                    channel=CREATION_TESTER,
                )
        elif board is not None:
            if ready:
                board.post(
                    CREATION_TESTER,
                    f"{path.name} compiles. The {developer}'s change is validated.",
                    kind="log",
                    channel=CREATION_TESTER,
                )
            else:
                board.post(
                    CREATION_TESTER,
                    f"{path.name} was sent back. {problem or error}",
                    kind="report",
                    channel=CREATION_TESTER,
                )
        with _repair_gate:
            job = _repairs.get(str(path))
            if job is not None and job.get("path") == path:
                job["ready"] = ready
                job["done"] = True

    thread = threading.Thread(target=work, daemon=True, name=f"repair-{path.stem[:40]}")
    with _repair_gate:
        _repairs[str(path)] = {
            "path": path,
            "timeframe": timeframe,
            "root": str((_chart_context() or {}).get("root") or ""),
            "developer": developer,
            "slot": slot,
            "researcher": researcher or developer,
            "thread": thread,
            "ready": False,
            "done": False,
        }
    thread.start()


def _repair_jobs(developer: str, slot: int | None = None) -> list[tuple[str, dict]]:
    """This developer's repairs. With a slot, only the ones that engine started."""
    with _repair_gate:
        jobs = [(key, job) for key, job in _repairs.items() if job.get("developer") == developer]
    if slot is not None:
        jobs = [(key, job) for key, job in jobs if job.get("slot") == slot]
    return jobs


def collect_validated(developer: str, slot: int | None = None):
    """The validated script after the next backtest. Pending while the tester is still checking.

    A repair is the file's, not one engine's. Each chart developer owns seven engines, so slot
    narrows it to the repairs this engine started.
    """
    jobs = _repair_jobs(developer, slot)
    if not jobs:
        return "none", None
    finished = [
        (key, job) for key, job in jobs
        if job.get("done") and not (job.get("thread") is not None and job["thread"].is_alive())
    ]
    if not finished:
        return "pending", None
    key, job = finished[0]
    with _repair_gate:
        if _repairs.get(key) is not job:
            return "none", None
        _repairs.pop(key, None)
    if job.get("ready"):
        return "ready", job
    return "rejected", None


def pickup_validated(board, developer: str, frames: dict, pause, engine_slot: int | None = None, root: str | None = None) -> str | None:
    """Backtest the script the creation tester validated, on this engine's instrument."""
    status, job = collect_validated(developer, engine_slot)
    if status == "pending":
        return "pending"
    if status != "ready" or not job:
        return None
    path = job["path"]
    timeframe = job["timeframe"]
    root = root or job.get("root") or None
    # The repair was the file's, so every chart may take the fixed script again.
    release_script(path)
    release_script(path, timeframe)
    if not path.is_file():
        return None
    if board is not None:
        board.post(
            developer,
            f"{path.name} is validated. This chart is backtesting it.",
            kind="log",
            channel=developer,
        )
    studied = _study(
        board,
        path,
        frames,
        pause,
        developer,
        timeframe,
        job.get("researcher") or developer,
        engine_slot,
        root=root,
    )
    if studied == "engine":
        park_engine_script(path, timeframe, root or "")
    elif studied == "repairing":
        hold_script(path)
    elif studied == "restarted":
        # Like the chart loop: the engine takes its other scripts first, then this one resumes its stage.
        hold_script(path, timeframe, root or "")
    return studied


def _tell_administrator(board, slot: int, path: Path, reason: str) -> None:
    """An engine fault belongs to the systems administrator. The engine takes the next script."""
    _stage_end.reason = f"The engine stopped. {reason}"
    from futuresfund.backtrader_engine import set_engine_notice

    set_engine_notice(slot, "")
    if board is None:
        return
    board.post(
        "Systems Administrator",
        f"Engine {slot + 1} problem while running {path.name}. {reason} The engine is taking the next script.",
        kind="report",
        channel="Systems Administrator",
    )


def release_retries(timeframe: str, failed: dict, root: str | None = None) -> bool:
    """Give a script that failed once another turn after the other scripts have been tried.

    A script PineTS could not finish stays parked. Another engine must not put it back while
    slower copies are still waiting. failed counts compile failures per file (str(path)); a hold
    on the whole file (a compile failure) is released by any chart's engine.
    """
    wanted = str(root or "").upper()
    with _held_lock:
        keys = [
            key for key in _held_back
            if key[1] in (timeframe, "")
            and (root is None or key[2] in ("", wanted))
            and key not in _engine_parked
            and failed.get(key[0], 0) < 2
        ]
        for key in keys:
            _held_back.discard(key)
        return bool(keys)


def chart_needs_a_script(timeframe: str, learning: Path | None = None, root: str | None = None) -> bool:
    """True when this chart (one instrument's, with a root) has no Pine file it can study. A parked script does not count."""
    from futuresfund.charts import charts_have_volume

    skip_volume = not charts_have_volume()
    with _held_lock:
        held = set(_held_back) | set(_engine_parked)
    paths = queue(learning, skip_volume=skip_volume) if learning is not None else jobs_for_chart(timeframe, skip_volume=skip_volume, root=root)
    for path in paths:
        if is_held(held, path, timeframe, root):
            continue
        if learning is not None:
            roots = _open_roots(path, timeframe)
            if not roots or (root is not None and str(root).upper() not in roots):
                continue
        return False
    return True


def _in_learning(path: Path | None) -> bool:
    """True when this Pine file is still in the folder the charts study."""
    if path is None:
        return True
    try:
        return path.is_file() and path.resolve().parent == LEARNING.resolve()
    except OSError:
        return False


def _study_home(path: Path) -> str:
    """The folder a chart says the script is waiting in."""
    if _in_created(path):
        return path.parent.name
    return "the learning folder"


def _in_created(path: Path | None) -> bool:
    """True when this file is one the creation team wrote: a draft on engine 4, or a script in createdStrategies."""
    if path is None:
        return False
    try:
        return path.is_file() and path.parent.name in {"createdStrategies", "draftStrategies"}
    except OSError:
        return False


def _in_drafts(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_file() and path.parent.name == "draftStrategies"
    except OSError:
        return False


def _created_runs(notes: dict) -> list[dict]:
    """Every measured run in a created script's notes: engine 4's development, each chart, and the archived charts."""
    rows = [row for row in (notes.get("trials") or []) if isinstance(row, dict)]
    rows.extend(row for row in [notes.get("best")] if isinstance(row, dict) and row)
    creation = notes.get("creation") if isinstance(notes.get("creation"), dict) else {}
    rows.extend(row for row in (creation.get("trials") or []) if isinstance(row, dict))
    instruments = notes.get("instruments") if isinstance(notes.get("instruments"), dict) else {}
    for record in instruments.values():
        if not isinstance(record, dict):
            continue
        rows.extend(row for row in (record.get("trials") or []) if isinstance(row, dict))
        charts = record.get("charts") if isinstance(record.get("charts"), dict) else {}
        for chart in charts.values():
            if isinstance(chart, dict):
                rows.extend(row for row in (chart.get("trials") or []) if isinstance(row, dict))
                rows.extend(row for row in [chart.get("best")] if isinstance(row, dict) and row)
        archived = record.get("archived_charts") if isinstance(record.get("archived_charts"), dict) else {}
        for items in archived.values():
            for item in items if isinstance(items, list) else []:
                kept = item.get("record") if isinstance(item, dict) else None
                if isinstance(kept, dict):
                    rows.extend(row for row in (kept.get("trials") or []) if isinstance(row, dict))
    return rows


def _created_passed(path: Path) -> bool:
    """A created script moves on to more testing only after one run cleared the acceptance."""
    from futuresfund.book import strategy_acceptance
    from futuresfund.objective import acceptance_misses

    limits = strategy_acceptance()
    return any(not acceptance_misses(row, limits=limits) for row in _created_runs(_peek_notes(path)))


def set_unproven(path: Path, reason: str) -> Path:
    """Move a created script that never cleared the acceptance into unprovenStrategies. It is kept and not studied again."""
    UNPROVEN.mkdir(parents=True, exist_ok=True)
    target = UNPROVEN / path.name
    if target.exists() and target.resolve() != path.resolve():
        target = UNPROVEN / f"{path.stem}_{int(time.time())}{path.suffix}"
    twin = companion(path) if path.suffix.lower() == ".pine" else None
    if path.resolve() != target.resolve():
        shutil.move(str(path), str(target))
    if twin is not None and twin.is_file() and not (UNPROVEN / twin.name).exists():
        shutil.move(str(twin), str(UNPROVEN / twin.name))
    (UNPROVEN / f"{target.stem}.reason.txt").write_text(reason.strip() + "\n", encoding="utf-8")
    return target


def leave_unrunnable(board, path: Path, developer: str, reason: str) -> str:
    """Park the script in cannotRunStrategies. Only the owner's "park" answer, questions by email turned off,
    or an engine 4 draft comes here. Everything that used to park a script on its own now asks the owner (_ask_owner).
    """
    _stage_end.reason = f"It cannot be run. {reason}"
    if _in_learning(path) or _in_created(path):
        set_aside(path, reason)
        # Parked on every chart, so no stopped stage of this file waits to resume ("resumes there" would stay forever).
        _forget_resume(path)
    if board is not None:
        board.post(
            developer,
            f"{path.name} cannot be run. It was moved to cannotRunStrategies. {reason}",
            kind="log",
            channel=developer,
        )
    return "cannot_run"


def set_aside(path: Path, reason: str) -> Path:
    """Move a script the engine cannot run out of the learning folder.

    The folder is cannotRunStrategies at the project root. The charts never study it.
    A reason file sits next to the script.
    """
    CANNOT_RUN.mkdir(parents=True, exist_ok=True)
    twin = companion(path) if path.suffix.lower() == ".pine" else None
    target = _place_aside(path)
    if twin is not None and twin.is_file():
        _place_aside(twin)
    (CANNOT_RUN / f"{target.stem}.reason.txt").write_text(reason.strip() + "\n", encoding="utf-8")
    return target


def _place_aside(path: Path, folder: Path | None = None) -> Path:
    """Move one file into a folder (cannotRunStrategies unless another is named). A name already there gets a time suffix."""
    folder = Path(folder) if folder is not None else CANNOT_RUN
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    if target.exists() and target.resolve() != path.resolve():
        target = folder / f"{path.stem}_{int(time.time())}{path.suffix}"
    if path.resolve() != target.resolve():
        shutil.move(str(path), str(target))
    return target


def _ask_owner(board, path: Path, author: str, reason: str, kind: str, developer: str = "") -> str:
    """The chart researcher asks the owner (questions.ask_owner). A blocking kind returns "awaiting".

    With questions by email off, or when the questions module fails, a blocking kind parks the script the old way.
    """
    try:
        from futuresfund import questions

        chart = dict(_chart_context() or {})
        if developer:
            chart["developer"] = developer
        return questions.ask_owner(board, path, author or developer, reason, kind, chart=chart)
    except Exception:
        traceback.print_exc()
    if kind != "repair":
        # Every kind but a starting repair blocks the script, so it must leave the queue.
        return leave_unrunnable(board, path, developer or author, reason)
    return ""


def _chart_context() -> dict:
    """The chart this thread is studying: root, timeframe, engine, developer and researcher (set in _study_chart)."""
    return dict(getattr(_stage_end, "chart", None) or {})


def _left_text(path: Path, label: str = "") -> str:
    """What a chart posts when the file it was studying left the folder: parked, waiting for the owner, or filed."""
    where = f"The {label} chart" if label else "This chart"
    if (CANNOT_RUN / path.name).is_file():
        return f"{path.name} is in cannotRunStrategies. {where} left it."
    try:
        from futuresfund.questions import awaiting_dir

        if (awaiting_dir() / path.name).is_file():
            return f"{path.name} waits in awaitingAnswerStrategies for the owner's answer. {where} left it."
    except Exception:
        pass
    if (LEARNED / path.name).is_file():
        return f"{path.name} was filed in learnedStrategies. {where} left it."
    return f"{path.name} is no longer in its folder. {where} left it."


def release_file(path: Path | str) -> None:
    """Every chart may take this script again: holds, parked engines, repair counts and zero-trade counts are cleared."""
    key = str(path)
    with _held_lock:
        for held in [item for item in _held_back if item[0] == key]:
            _held_back.discard(held)
        for held in [item for item in _engine_parked if item[0] == key]:
            _engine_parked.discard(held)
    _repair_tries.pop(key, None)
    try:
        base = str(Path(path).resolve())
    except OSError:
        base = key
    with _flat_lock:
        for name in [name for name in _flat_runs if name == base or name.startswith(base + "|")]:
            _flat_runs.pop(name, None)


def researcher_job(key: str, work, urgent: bool = False) -> bool:
    """Run work on the chart researcher's decision worker, one job per key at a time. Never on a chart thread.

    A test run does not start the worker for these jobs (tests that drain the decision queue would see it
    take their questions): the job runs where it was asked.
    """
    from futuresfund.mailer import test_run

    if test_run():
        try:
            work()
        except Exception:
            traceback.print_exc()
        return True
    with _decisions_lock:
        if key in _decisions_open:
            return False
        _decisions_open.add(key)
    _decisions.put((0 if urgent else 1, next(_decisions_turn), {"key": key, "call": work}))
    _ensure_worker()
    return True


def _reopen_for_owner(board, path: Path, researcher: str) -> bool:
    """The owner's answer runs the script's charts again. The old notes go to researchNotes/before_repair.

    Unlike _reopen_for_answer it does not count against REOPEN_LIMIT and works with REOPEN_FOR_ANSWERS
    or READ_BEFORE_TESTING off: an owner's answer is not a researcher's answer.
    """
    with _notes_lock:
        notes = _read_notes(path)
        if not notes.get("instruments") and not notes.get("trials"):
            return False
        if not path.is_file() and (LEARNED / path.name).is_file():
            shutil.move(str(LEARNED / path.name), str(path))
        slug = path.stem.replace(" ", "_")
        keep = NOTES / "before_repair"
        keep.mkdir(parents=True, exist_ok=True)
        number = len(list(keep.glob(f"{slug}_owner_*.json"))) + 1
        (keep / f"{slug}_owner_{number}.json").write_text(_notes_json(notes), encoding="utf-8")
        dropped = {"instruments", "trials", "best", "over_trade_charts", "over_trade_cap"}
        fresh = {key: value for key, value in notes.items() if key not in dropped}
        (NOTES / f"{slug}.json").write_text(_notes_json(fresh), encoding="utf-8")
    _forget_resume(path)
    if board is not None:
        board.post(researcher, f"{path.name} has the owner's answer, so its charts run again.", kind="report", channel=researcher)
    return True


def queue(learning: Path | None = None, *, skip_volume: bool = False) -> list[Path]:
    """Pine scripts waiting to be studied. A python twin is studied with its pine, not as a second job.

    The learning folder comes first. When it has no script, the charts study the
    strategies the creation team placed in createdStrategies. A single chart that
    has already finished the learning folder uses jobs_for_chart and does not wait
    for the other charts.
    """
    folder = learning or LEARNING
    pines = _pine_files(folder, skip_volume)
    if pines or learning is not None:
        return pines
    return [
        path
        for path in _pine_files(CREATED, skip_volume)
        if path.name not in _developing_now() and _created_passed(path)
    ]


def _needs(path: Path, timeframe: str, views: dict, root: str | None) -> bool:
    """True when this file still needs this chart: on this instrument with a root, or on any instrument without one."""
    roots = _open_roots(path, timeframe, views)
    if root is None:
        return bool(roots)
    return str(root).upper() in roots


def jobs_for_chart(timeframe: str, *, skip_volume: bool = False, held: set | None = None, root: str | None = None) -> list[Path]:
    """Scripts this chart can open now. With a root, only scripts that still need that instrument's chart.

    Every instrument has its own engine for each chart, so an engine keeps a script only while its
    own root is still pending on its timeframe. Learning scripts that still need this chart come
    first. When every learning script is finished on this chart, the chart studies
    createdStrategies. A learning script held out of this run, such as one that still does not
    compile, does not keep the chart from createdStrategies.
    """
    learning_files = _pine_files(LEARNING, skip_volume)
    learning_views = _folder_views(LEARNING)
    learning = [
        path for path in learning_files
        if _needs(path, timeframe, learning_views, root) and not is_held(held, path, timeframe, root)
    ]
    if learning:
        return learning
    created_views = _folder_views(CREATED)
    return [
        path
        for path in _pine_files(CREATED, skip_volume)
        if path.name not in _developing_now() and _created_passed(path) and _needs(path, timeframe, created_views, root)
    ]


def _record_ran(record) -> bool:
    """True when this chart has a measured run, including a run that took no trades."""
    if not isinstance(record, dict):
        return False
    if record.get("attempts"):
        return True
    trials = record.get("trials")
    if isinstance(trials, list) and any(isinstance(row, dict) for row in trials):
        return True
    best = record.get("best")
    return isinstance(best, dict) and bool(best)


def _ran_on_chart(path: Path, timeframe: str) -> bool:
    """True when this file, still in its folder, already has a run on this chart."""
    notes = _note_view(path)
    instruments = notes.get("instruments") if isinstance(notes, dict) else None
    if not isinstance(instruments, dict):
        return False
    for record in instruments.values():
        if not isinstance(record, dict):
            continue
        charts = record.get("charts")
        if isinstance(charts, dict) and _record_ran(charts.get(timeframe)):
            return True
    return False


def _peek_notes(path: Path) -> dict:
    """The note on disk. A missing file has not been run, and this read does not take the engine locks."""
    file = NOTES / f"{path.stem.replace(' ', '_')}.json"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ran_notes(notes: dict, timeframe: str) -> bool:
    instruments = notes.get("instruments") if isinstance(notes, dict) else None
    if not isinstance(instruments, dict):
        return False
    for record in instruments.values():
        if not isinstance(record, dict):
            continue
        charts = record.get("charts")
        if isinstance(charts, dict) and _record_ran(charts.get(timeframe)):
            return True
    return False


def _direct_views(folder: Path) -> dict[str, list[tuple[Path, dict]]]:
    """One unlocked pass over a folder, so a second copy of a script can see a finished chart."""
    grouped: dict[str, list[tuple[Path, dict]]] = {}
    if not folder.is_dir():
        return grouped
    try:
        names = list(folder.iterdir())
    except OSError:
        return grouped
    for other in names:
        if not _strategy_text(other):
            continue
        try:
            resolved = other.resolve()
        except OSError:
            resolved = other
        grouped.setdefault(_stem_key(other), []).append((resolved, _peek_notes(other)))
    return grouped


def _open_from_notes(path: Path, timeframe: str, notes: dict, views: dict) -> list[str]:
    pending = _pending_roots(path, timeframe, notes)
    again = _resumable_roots(path, timeframe) if pending else {}
    try:
        mine = path.resolve()
    except OSError:
        mine = path
    copies = views.get(_stem_key(path), [])

    def covered(root: str) -> bool:
        for other, view in copies:
            if other == mine:
                continue
            if _chart_finished(view, root, timeframe):
                return True
        return False

    return [root for root in pending if root in again or not covered(root)]


def _counted_pairs(views: dict, skip_volume: bool) -> list[tuple[Path, dict]]:
    pairs = [item for group in views.values() for item in group]
    if skip_volume:
        pairs = [(path, notes) for path, notes in pairs if not _script_uses_volume(path)]
    return pairs


def _ran_root(notes: dict, root: str, timeframe: str) -> bool:
    """True when this instrument's chart has a measured run in these notes."""
    instruments = notes.get("instruments") if isinstance(notes, dict) else None
    record = instruments.get(root) if isinstance(instruments, dict) else None
    charts = record.get("charts") if isinstance(record, dict) else None
    return isinstance(charts, dict) and _record_ran(charts.get(timeframe))


def _folder_counts(skip_volume: bool, by_root: bool) -> tuple[list[dict], list[dict]]:
    """The chart rows and, with by_root, one row per instrument's chart, from one read of the notes."""
    learning_views = _direct_views(LEARNING)
    created_views = _direct_views(CREATED)
    learning = _counted_pairs(learning_views, skip_volume)
    created = _counted_pairs(created_views, skip_volume)
    rows = []
    open_learning = {}
    open_created = {}
    for timeframe in ("2m", "5m", "15m"):
        open_learning[timeframe] = [_open_from_notes(path, timeframe, notes, learning_views) for path, notes in learning]
        open_created[timeframe] = [_open_from_notes(path, timeframe, notes, created_views) for path, notes in created]
        rows.append({
            "timeframe": timeframe,
            "learning_done": sum(_ran_notes(notes, timeframe) for _path, notes in learning),
            "learning_total": len(learning),
            "learning_open": sum(bool(roots) for roots in open_learning[timeframe]),
            "created_done": sum(_ran_notes(notes, timeframe) for _path, notes in created),
            "created_total": len(created),
            "created_open": sum(bool(roots) for roots in open_created[timeframe]),
        })
    charts = []
    if by_root:
        from futuresfund.roster import ENGINE_DESKS

        for desk in ENGINE_DESKS:
            root, timeframe = desk["root"], desk["timeframe"]
            charts.append({
                "root": root,
                "timeframe": timeframe,
                "engine": desk["engine"],
                "learning_done": sum(_ran_root(notes, root, timeframe) for _path, notes in learning),
                "learning_total": len(learning),
                "learning_open": sum(root in roots for roots in open_learning[timeframe]),
                "created_done": sum(_ran_root(notes, root, timeframe) for _path, notes in created),
                "created_total": len(created),
                "created_open": sum(root in roots for roots in open_created[timeframe]),
            })
    return rows, charts


def chart_root_counts(*, skip_volume: bool = False) -> list[dict]:
    """The same counts for each instrument's chart, one row per chart engine: {root, timeframe, engine, ...}."""
    return _folder_counts(skip_volume, True)[1]


def chart_folder_counts(*, skip_volume: bool = False) -> list[dict]:
    """How many scripts still in each folder this chart has already run.

    The total is the files in the folder now. A file counts once it has a run
    on this chart, even when another instrument on that chart is still open.
    Notes are read directly so a stuck engine scan cannot keep the old totals on the cards.
    chart_root_counts gives the same counts for each instrument's chart.
    """
    return _folder_counts(skip_volume, False)[0]


def chart_progress() -> dict:
    """Counts for the engine cards. One pass runs at a time and is kept for a few seconds."""
    from futuresfund.charts import charts_have_volume

    now = time.monotonic()
    with _progress_lock:
        rows = _progress_state["rows"]
        charts = _progress_state.get("charts") or []
        if rows is not None and now - float(_progress_state["at"]) <= 15:
            return {"ready": True, "rows": list(rows), "charts": list(charts)}
        if _progress_state["running"]:
            return {"ready": rows is not None, "rows": list(rows or []), "charts": list(charts)}
        _progress_state["running"] = True
    try:
        # One row per instrument's chart too, so each engine card shows its own instrument's counts.
        rows, charts = _folder_counts(not charts_have_volume(), True)
    except Exception:
        traceback.print_exc()
        with _progress_lock:
            rows = list(_progress_state["rows"] or [])
            charts = list(_progress_state.get("charts") or [])
    with _progress_lock:
        _progress_state["rows"] = rows
        _progress_state["charts"] = charts
        _progress_state["at"] = time.monotonic()
        _progress_state["running"] = False
    return {"ready": True, "rows": list(rows), "charts": list(charts)}


def _refresh_chart_progress(skip_volume: bool) -> None:
    try:
        rows = chart_folder_counts(skip_volume=skip_volume)
    except Exception:
        traceback.print_exc()
        with _progress_lock:
            rows = list(_progress_state["rows"] or [])
    with _progress_lock:
        _progress_state["rows"] = rows
        _progress_state["at"] = time.monotonic()
        _progress_state["running"] = False


_kind_cache: dict[str, tuple[int, bool]] = {}
_folder_cache: dict[str, tuple[int, list]] = {}
_cache_lock = threading.Lock()


def _is_strategy_file(path: Path) -> bool:
    """A Pine script, including one saved with a .txt name."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return False
    key = str(path)
    with _cache_lock:
        hit = _kind_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    found = _strategy_text(path)
    with _cache_lock:
        _kind_cache[key] = (stamp, found)
    return found


def _strategy_text(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() == ".pine":
        return True
    if path.suffix.lower() != ".txt":
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:40]
    except OSError:
        return False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("//@version") or stripped.startswith("strategy("):
            return True
    return False


def _strategy_files(folder: Path) -> list[Path]:
    """Strategy files in one folder, listed once until that folder changes."""
    try:
        stamp = folder.stat().st_mtime_ns
    except OSError:
        return []
    key = str(folder)
    with _cache_lock:
        hit = _folder_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    names = [path for path in folder.iterdir() if _is_strategy_file(path)]
    with _cache_lock:
        _folder_cache[key] = (stamp, names)
    return names


def _pine_files(folder: Path, skip_volume: bool) -> list[Path]:
    if not folder.is_dir():
        return []
    pines = sorted(path for path in folder.iterdir() if _is_strategy_file(path))
    if skip_volume:
        pines = [path for path in pines if not _script_uses_volume(path)]
    from futuresfund.redesign import is_redesign

    from futuresfund.delta_core import is_core_file

    # Scripts the owner named in researchNotes/queue_first.json go before everything else, in that order.
    # Delta volume breakout scripts go next: the desk is looking for one that clears the acceptance on every instrument.
    # Inside each group a researcher's redesign goes first, so the answer to a failed script is measured soon.
    # Scripts the creation team wrote wait behind the other learning scripts.
    first = _queue_first()
    return sorted(pines, key=lambda path: (
        first.index(path.name) if path.name in first else len(first),
        not is_core_file(path), not is_redesign(path), path.name.startswith("created_"), path.name.lower(),
    ))


_QUEUE_FIRST: dict = {}


def _queue_first() -> list[str]:
    """File names to study before the rest, from researchNotes/queue_first.json. Read again only when it changes."""
    path = NOTES / "queue_first.json"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    if _QUEUE_FIRST.get("stamp") != stamp:
        try:
            names = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            names = []
        _QUEUE_FIRST["names"] = [str(name) for name in names] if isinstance(names, list) else []
        _QUEUE_FIRST["stamp"] = stamp
    # The design page can clear this cache from another thread, so a missing entry reads as empty.
    return list(_QUEUE_FIRST.get("names") or [])


_developing_lock = threading.Lock()
_developing: set[str] = set()


def mark_developing(name: str) -> None:
    """Engine 4 is rewriting this created script, so the charts leave it alone."""
    with _developing_lock:
        _developing.add(name)


def clear_developing(name: str) -> None:
    with _developing_lock:
        _developing.discard(name)


def _developing_now() -> set[str]:
    with _developing_lock:
        return set(_developing)


def _script_uses_volume(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "volume" in text.lower()


def companion(pine: Path) -> Path | None:
    direct = pine.with_suffix(".py")
    if direct.is_file() and not direct.name.startswith("test_"):
        return direct
    alt = pine.with_name(pine.stem + "_strategy.py")
    if alt.is_file():
        return alt
    return None


def restock_learning(skip: set[str] | None = None) -> list[str]:
    """Move created scripts into the learning folder when that folder has no script.

    A file an engine is developing, or a file named in skip, stays where it is.
    """
    if _strategy_files(LEARNING):
        return []
    left = set(skip or ())
    left.update(_developing_now())
    LEARNING.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in list(_strategy_files(CREATED)):
        if path.name in left or not path.is_file() or not _created_passed(path):
            continue
        dest = LEARNING / path.name
        if dest.exists():
            continue
        twin = companion(path)
        try:
            shutil.move(str(path), str(dest))
        except OSError:
            continue
        if twin is not None and twin.is_file():
            twin_dest = LEARNING / twin.name
            if not twin_dest.exists():
                try:
                    shutil.move(str(twin), str(twin_dest))
                except OSError:
                    pass
        moved.append(dest.name)
    return moved


_learning_thread: threading.Thread | None = None
_job_take = None
_job_release = None
_engine_threads: dict[int, threading.Thread] = {}
_engine_starters: dict[int, object] = {}
_engine4_learning = False
_learning_lock = threading.Lock()
_flat_lock = threading.Lock()
_flat_runs: dict[str, int] = {}
_leaving = False


def begin_research(board) -> dict:
    """Start the researchers. A stop that has not finished yet is turned back into a run."""
    global _learning_thread
    board.post(
        "System",
        "Start requested. The researchers will run the Pine scripts.",
        kind="system",
        channel="headquarters",
    )
    while True:
        with _learning_lock:
            board.request_research()
            thread = _learning_thread
            if thread is not None and thread.is_alive() and not _leaving:
                return {"ok": True, "started": True}
            if thread is None or not thread.is_alive():
                _learning_thread = threading.Thread(target=run_learning, args=(board,), daemon=True, name="futures-learning")
                _learning_thread.start()
                return {"ok": True, "started": True}
            stale = thread
        stale.join(timeout=2)


def load_hold() -> bool:
    """The Hold button from the last time this desk was running. A restart does not flip it."""
    try:
        return bool(json.loads(HOLD_PATH.read_text(encoding="utf-8")).get("hold_new"))
    except (OSError, ValueError):
        return False


def _save_hold(held: bool) -> None:
    try:
        HOLD_PATH.parent.mkdir(parents=True, exist_ok=True)
        HOLD_PATH.write_text(json.dumps({"hold_new": bool(held)}), encoding="utf-8")
    except OSError:
        return


def hold_chart_research(board) -> dict:
    """Stop the strategy team from writing another script. The chart engines keep backtesting the learning folder."""
    board.set_hold_new(True)
    _save_hold(True)
    board.post(
        "2min strategy developer",
        "The strategy team is not writing a new strategy. Each chart finishes the learning folder, then studies createdStrategies.",
        kind="report",
        channel="headquarters",
    )
    return {"ok": True, "hold_new": True}


def release_chart_research(board) -> dict:
    """Let the strategy team write scripts again."""
    global _engine4_learning
    _engine4_learning = False
    board.set_hold_new(False)
    _save_hold(False)
    board.post(
        "2min strategy developer",
        "The strategy team is writing strategies again.",
        kind="report",
        channel="headquarters",
    )
    return {"ok": True, "hold_new": False}


def engine4_should_study_learning() -> bool:
    """Restart put engine 4 onto the learning folder while the strategy team is held."""
    return _engine4_learning


def _spare_charts() -> list[tuple[str, dict]]:
    """Every (instrument, chart) engine 4 may help with: each research instrument's three charts.

    An instrument in research_instruments() that has no chart engines of its own (a new chart
    folder) comes first, since no other engine studies it.
    """
    from futuresfund.roster import CHART_DESKS, ENGINE_ROOTS

    roots = [str(root).upper() for root in research_instruments()]
    ordered = [root for root in roots if root not in ENGINE_ROOTS] + [root for root in roots if root in ENGINE_ROOTS]
    return [(root, desk) for root in ordered for desk in CHART_DESKS]


def study_learning_on_engine_4(board, pause) -> str:
    """Take the next learning script no chart engine has claimed and run it on engine 4."""
    take = _job_take
    release = _job_release
    if take is None or release is None:
        return "none"
    from futuresfund.strategy_team import CREATION_ENGINE, CREATION_TESTER

    engine = CREATION_ENGINE + 1
    for root, desk in _spare_charts():
        path = take(root, desk["timeframe"])
        if path is None:
            continue
        from futuresfund.backtrader_engine import begin_study

        begin_study(CREATION_ENGINE, path.name)
        board.post(
            CREATION_TESTER,
            f"Engine {engine} is studying the learning folder. Taking {path.name} on the {root} {desk['label']} chart.",
            kind="log",
            channel=CREATION_TESTER,
        )
        board.set_activity(CREATION_TESTER, f"Studying {path.name} on the {root} {desk['label']} chart.")
        try:
            studied = _study(
                board,
                path,
                {},
                pause,
                CREATION_TESTER,
                desk["timeframe"],
                CREATION_TESTER,
                engine_slot=CREATION_ENGINE,
                root=root,
            )
            if studied == "restarted":
                # Restart stopped a hung backtest. Engine 4 does not take the same script again at once: that
                # chart's own engine gives it back (release_retries) once its queue is empty, and the stage resumes.
                hold_script(path, desk["timeframe"], root)
        except Exception as exc:
            board.post(
                CREATION_TESTER,
                f"{path.name} stopped on an engine error: {exc}.",
                kind="error",
                channel=CREATION_TESTER,
            )
            studied = "error"
        finally:
            release(path, root, desk["timeframe"])
        return studied or "studied"
    board.set_task(CREATION_TESTER, "Not developing a new script.", working=False)
    return "none"


def _take_engine_restart(index: int) -> bool:
    """True once after the engine's Restart, read at the top of its loop between studies.

    Restart also sets the skip flag when the engine looked busy. When no search read that cut (the
    engine's busy flag was stale, or the cut landed after the last attempt), the skip flag is cleared
    here too, so the next study is not ended as an owner Skip. request_skip clears the restart flag, so
    a restart still set here means Restart was the last button pressed.
    """
    from futuresfund.backtrader_engine import clear_restart, clear_skip, restarted

    if not restarted(index):
        return False
    clear_restart(index)
    clear_skip(index)
    return True


def restart_research_engine(engine_id: int, board) -> dict:
    """Restart one engine. A hung backtest stops. Engine 4 then takes a learning script."""
    global _engine4_learning
    from futuresfund.backtrader_engine import request_restart
    from futuresfund.roster import CREATION_ENGINE_ID, chart_engine

    result = request_restart(engine_id)
    engine_id = int(engine_id)
    if engine_id == CREATION_ENGINE_ID:
        _engine4_learning = True
        board.post(
            "Creation tester",
            f"Engine {CREATION_ENGINE_ID} restarted. It will take the next script from the learning folder.",
            kind="log",
            channel="Creation tester",
        )
    else:
        desk = chart_engine(engine_id)
        if desk is not None:
            board.post(
                desk["developer"],
                f"Engine {engine_id} ({desk['root']} {desk['label']}) restarted. The hung backtest was stopped.",
                kind="log",
                channel=desk["developer"],
            )
    starter = _engine_starters.get(engine_id)
    thread = _engine_threads.get(engine_id)
    if starter is not None and (thread is None or not thread.is_alive()):
        replacement = starter()
        replacement.start()
        _engine_threads[engine_id] = replacement
        result["thread_started"] = True
    return result


def end_research(board) -> dict:
    """Stop the researchers. The strategy and any open order stay as they are."""
    board.post(
        "System",
        "Stop requested. The strategy was not changed and no order was sent.",
        kind="system",
        channel="headquarters",
    )
    board.mark_stopped()
    return {"ok": True}


def run_learning(board) -> None:
    """Keep studying until Stop. Start during a shutdown begins another run."""
    global _leaving
    while True:
        with _learning_lock:
            _leaving = False
            board.take_restart()
            board.cancel.clear()
            board.set_research("running")
        _run_learning(board)
        with _learning_lock:
            if board.take_restart():
                continue
            _leaving = True
            stopped = board.cancel.is_set()
            if stopped:
                board.acknowledge_stop()
        if stopped:
            board.post(
                "System",
                "Researchers stopped. The strategy was not changed and no order was sent.",
                kind="system",
                channel="headquarters",
            )
        return


def _run_learning(board) -> None:
    # The settings the owner saved on the Research workflow Design page apply from here, when research
    # starts, and never when this module is imported, so every test sees the values written in the code.
    try:
        from futuresfund.workflow_design import apply_saved, describe

        changed = apply_saved()
        if changed and board is not None:
            board.post("System", describe(changed), kind="log", channel="headquarters")
    except Exception:
        traceback.print_exc()
    from futuresfund.session import learning_pause
    from futuresfund.backtrader_engine import warm_engines
    from futuresfund.roster import CHART_DESKS, CREATION_ENGINE_ID, ENGINE_DESKS, engine_layout, engines_of

    LEARNED.mkdir(parents=True, exist_ok=True)
    NOTES.mkdir(parents=True, exist_ok=True)
    clear_holds()
    from futuresfund.backtrader_engine import clear_skips

    clear_skips()
    from futuresfund.backtrader_engine import clear_restarts

    clear_restarts()
    from futuresfund.proving import stop_testing

    # Nothing is running a stage yet, so a stage still testing was cut off when research or the desk last stopped.
    # It did not fail, so its chart is pending again and the next study resumes at that stage. A restart is
    # not the script's fault, so it does not count against MAX_RESUMES (only the far larger MAX_INTERRUPTIONS).
    try:
        stop_testing("Research stopped or the desk restarted before this stage finished.", resume=True, counted=False)
    except (OSError, ValueError, TypeError):
        pass
    instruments = ", ".join(research_instruments())
    warm_engines()
    from futuresfund.engine_workers import at_once

    pool = at_once()
    for desk in CHART_DESKS:
        mine = [item for item in ENGINE_DESKS if item["developer"] == desk["developer"]]
        named = ", ".join(f"{item['engine']} {item['root']}" for item in mine)
        board.set_activity(desk["developer"], f"Engines {', '.join(str(number) for number in engines_of(desk['developer']))} are ready for the {desk['label']} charts")
        board.post(
            desk["developer"],
            f"This desk runs the {desk['label']} chart of every instrument, each on its own engine: {named}. "
            f"The backtests share {pool} worker processes, so at most {pool} run at once and an engine waits for a free worker. "
            "Each engine sends a worker only the last year of its chart.",
            kind="log",
            channel=desk["developer"],
        )
    frames = _load_frames()
    from futuresfund.charts import chart_columns, charts_have_volume

    volume_ready = charts_have_volume()
    if not volume_ready:
        listed = "; ".join(f"{name} is {', '.join(cols)}" for name, cols in chart_columns().items())
        for desk in CHART_DESKS:
            board.post(
                desk["developer"],
                "The chart files have no volume column. "
                f"{listed}. The EMA numbers sit next to the price, so they are a moving average, not contract volume. "
                "A script that reads volume cannot enter on these bars, so those scripts stay in the learning folder. "
                "The price alert can send volume, the profile, and delta. They stay here until that data is on the bars.",
                kind="report",
                channel=desk["developer"],
            )
    from futuresfund.strategy import absorb_research_notes

    absorb_research_notes()
    from futuresfund.objective import acceptance_line, goal_line

    loaded = ", ".join(f"{name} {len(rows)} bars" for name, rows in frames.items())
    intro = (
        "The learning folder is only the starting point. Each chart stops after 10 successful runs. A run counts when it passes acceptance. "
        "On the full chart the search does not use the headquarters profit target or the trailing drawdown cap. "
        f"{acceptance_line()} That is the bar for every agent on this desk. Move the next input toward those four numbers. "
        "Two or three trades is not a strategy. "
        f"{goal_line()} The take profit is larger than the stop. Set the stop at no more than 1.5 percent of the account. A fill past a stop set there does not fail the run. "
        f"{engine_layout()} Each engine studies only its own instrument's chart, so two instruments can test the same script at once. "
        f"The backtests run on {pool} PineTS worker processes at once; an engine waits for a free worker. "
        f"The strategy developers write new scripts from scratch at all times, and engine {CREATION_ENGINE_ID} develops each one. "
        "A new strategy in the library is read before the next script. A profitable run inside the one-year drawdown cap is filed for the chart engines so the next script can improve on it. "
        "Each chart studies on its own thread. It does not wait while another chart writes a rule or runs a backtest. "
        "A script the engine cannot run is not parked by the desk. The chart researcher asks the owner by email, the script waits in "
        "awaitingAnswerStrategies at the project root, and the engine takes the next script. Only the owner's park answer moves a script to cannotRunStrategies. "
        "When a chart has finished every script in the learning folder, that chart studies createdStrategies. It does not wait while another chart is still in the learning folder. "
        "A backtest uses a year of the chart, the last 260 futures sessions. The Databento file on disk is the five years from 2021-10-04 through 2026-10-03, and TradingView exports in each chart folder continue it. "
        "The chart teams stay on that one-year chart. They do not switch it to the five-year download. "
        "When a script's entry is not already on the engine, the chart developer adds that rule before the attempts. "
        f"Each script is tested once on every instrument ({instruments}) and on each of the three charts. It moves to the learned folder only after those charts are done. "
        f"Loaded {loaded}. "
        "Every attempt changes one thing and records how that change moved the result. The research guide is guidance, not a requirement. "
        "A result under the acceptance line is not filed. The researcher reads that script and changes one input toward the four numbers on the strategy library page. The guideline is researchNotes/how_to_research.md. "
        "A result that already clears that line still moves one input. A change that ranks lower under the objective is reversed, and a change that ranks higher is continued. "
        "A run that repeats an earlier profit, drawdown, and trade count is not counted. "
        f"On a chart engine, three of those in a row ends that chart and the next script is taken. Engine {CREATION_ENGINE_ID} keeps searching. "
        "A second copy of a script this chart already finished is skipped. "
        "The next setting starts from the best result so far. A higher win rate is kept when the profit factor stays above the profit factor on the strategy library page and no trade loses more than 1.5 percent of the account. "
        "A research report is emailed when that chart finishes, after its successful runs or after all of its different results. "
        f"Each chart proves a script in stages: the last 7 days gets up to {STAGE_7_ATTEMPTS} different results, 30 days {STAGE_30_ATTEMPTS}, "
        f"90 days {STAGE_90_ATTEMPTS}, and the year {ATTEMPT_LIMIT}. "
        "A stage does not stop at its first pass. It keeps searching for more profit with less drawdown, "
        f"and stops when its attempts are spent or {STAGE_PATIENCE} attempts in a row found no better pass. "
        "The next stage starts from the best pass. A stage with no pass spends all of its attempts. "
        "The year search does not stop at 10 successful runs. A run counts when it passes acceptance. "
        f"On a chart engine, a year whose best result is still a loss after {LOSS_LOOK} different results ends that chart and the next script is taken."
    )
    board.post(CHART_DESKS[0]["researcher"], intro, kind="report", channel=CHART_DESKS[0]["researcher"])
    import os
    from futuresfund.config import load_env

    load_env()
    try:
        from futuresfund.questions import start as start_questions

        # The Portfolio Manager's outbox and inbox threads. A test run or FUTURES_MAIL_OFF never starts them.
        start_questions(board)
    except Exception:
        traceback.print_exc()
    if not os.environ.get("SMTP_PASSWORD", "").strip():
        board.post(
            "Portfolio Manager",
            "Research reports are addressed to thefutureoffuturestrading@gmail.com. SMTP_PASSWORD is empty, so Gmail has not accepted one yet. Each report is still saved in the research notes.",
            kind="report",
            channel="Portfolio Manager",
        )
    claims = JobClaims(skip_volume=not volume_ready)
    # Compile failures are the file's, shared by every engine, so a script that does not compile is not retried once per engine.
    failed: dict[str, int] = {}
    take_job = claims.take
    release_job = claims.release

    def developer_loop(desk: dict) -> None:
        developer = desk["developer"]
        researcher = desk["researcher"]
        timeframe = desk["timeframe"]
        root = desk["root"]
        engine = desk["engine"]
        index = engine - 1
        # "Engine 8 (NQ 2-minute)": every post names the engine and its instrument.
        where = f"the {root} {desk['label']} chart"
        name = f"Engine {engine} ({root} {desk['label']})"
        announced = False
        waiting_repair = False
        while not board.cancel.is_set():
            if not _wait(board, learning_pause):
                return
            if _take_engine_restart(index):
                board.post(
                    developer,
                    f"{name} restarted. Taking the next script.",
                    kind="log",
                    channel=developer,
                )
            # At most once a minute across every engine, not once per engine per pass.
            file_completed_soon(board)
            path = take_job(root, timeframe)
            if path is None:
                if waiting_repair:
                    follow = pickup_validated(board, developer, frames, learning_pause, index, root=root)
                    if follow == "pending":
                        if not _sleep(board, learning_pause, 5):
                            return
                        continue
                    waiting_repair = follow == "repairing"
                    continue
                if release_retries(timeframe, failed, root):
                    if not _sleep(board, learning_pause, 5):
                        return
                    continue
                if not announced:
                    if board.holding_new_scripts():
                        waiting = (
                            f"{name} has finished the learning folder and createdStrategies on {where}. "
                            "The strategy team is not writing a new strategy."
                        )
                    else:
                        waiting = (
                            f"{name} has finished the learning folder and createdStrategies on {where}. "
                            "Its strategy developer writes the next one."
                        )
                    board.post(developer, waiting, kind="log", channel=developer)
                    announced = True
                if not _sleep(board, learning_pause, 20):
                    return
                continue
            announced = False
            board.post(developer, f"{name} is taking {path.name} from {_study_home(path)}.", kind="log", channel=developer)
            board.post(
                researcher,
                f"{developer} is studying {path.name} on {where}, engine {engine}.",
                kind="log",
                channel=researcher,
            )
            try:
                studied = _study(board, path, frames, learning_pause, developer, timeframe, researcher, index, root=root)
                error = None
            except Exception as exc:
                studied, error = "error", exc
            finally:
                release_job(path, root, timeframe)
            if studied == "repairing":
                # A script that still does not compile gets one more turn this run, not a turn every few seconds.
                # The hold is the whole file's; only the engine that placed it counts the failure.
                if hold_script(path):
                    failed[str(path)] = failed.get(str(path), 0) + 1
                waiting_repair = True
                continue
            if studied == "engine":
                park_engine_script(path, timeframe, root)
                continue
            if waiting_repair:
                follow = pickup_validated(board, developer, frames, learning_pause, index, root=root)
                if follow == "pending":
                    pass
                else:
                    waiting_repair = follow == "repairing"
            if studied == "error":
                if board.cancel.is_set():
                    return
                traceback.print_exc()
                park_engine_script(path, timeframe, root)
                detail = str(error).rstrip(".")
                board.post(
                    developer,
                    f"{path.name} stopped on an engine error on {name}: {detail}. It was not marked finished.",
                    kind="error",
                    channel=developer,
                )
                board.post(
                    researcher,
                    f"{path.name} stopped on an engine error on {where}: {error}. It stays in {_study_home(path)}.",
                    kind="report",
                    channel=researcher,
                )
                continue
            if studied in ("cannot_run", "awaiting"):
                # The file left the queue: parked, or waiting in awaitingAnswerStrategies for the owner's answer.
                continue
            if studied == "busy":
                hold_script(path, timeframe, root)
                continue
            if studied == "failed":
                park_engine_script(path, timeframe, root)
                board.post(
                    developer,
                    f"{path.name} did not finish a backtest on {where}. {name} is taking the next script.",
                    kind="log",
                    channel=developer,
                )
                continue
            if studied == "held":
                continue
            if studied == "restarted":
                # Restart is not Skip: the engine takes the next script, and this one resumes at the stage it
                # stopped at on its next turn (release_retries gives it back once the other scripts were tried).
                hold_script(path, timeframe, root)
                continue
            if studied == "skipped":
                park_engine_script(path, timeframe, root)
                continue
            elif studied == "blocked":
                park_engine_script(path, timeframe, root)
                board.post(
                    researcher,
                    f"{path.name} needs volume on the {root} {desk['label']} chart bars. It stays in {_study_home(path)}.",
                    kind="report",
                    channel=researcher,
                )
                board.post(
                    developer,
                    f"{path.name} needs volume on the chart bars. {name} is taking the next script.",
                    kind="log",
                    channel=developer,
                )
            elif studied == "waiting" and not _sleep(board, learning_pause, 60):
                return

    from futuresfund.strategy_team import run_chart_creation, run_creation_engine

    global _job_take, _job_release, _engine_starters, _engine_threads
    _job_take = take_job
    _job_release = release_job

    def start_chart(desk: dict) -> threading.Thread:
        thread = threading.Thread(target=developer_loop, args=(desk,), daemon=True, name=f"engine-{desk['engine']}-{desk['root']}-{desk['timeframe']}")
        _engine_threads[desk["engine"]] = thread
        return thread

    def start_creation() -> threading.Thread:
        thread = threading.Thread(target=run_creation_engine, args=(board, learning_pause), daemon=True, name="creation-engine")
        _engine_threads[CREATION_ENGINE_ID] = thread
        return thread

    # One developer loop per chart engine: three per instrument. Each developer owns seven of them.
    chart_desks = ENGINE_DESKS
    _engine_starters = {desk["engine"]: (lambda desk=desk: start_chart(desk)) for desk in chart_desks}
    _engine_starters[CREATION_ENGINE_ID] = start_creation
    workers = [start_chart(desk) for desk in chart_desks]
    workers.extend(
        threading.Thread(target=run_chart_creation, args=(board, learning_pause, desk), daemon=True, name=desk["strategy_developer"])
        for desk in CHART_DESKS
    )
    workers.append(start_creation())
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


class JobClaims:
    """Which engine has which script open, keyed (file, timeframe, root).

    One instrument's engine holds a script on its own chart only, so another instrument's engine
    on the same timeframe can study the same file at the same time. Engine 4's spare-time study
    claims through the same object, so it never opens a chart an engine already has.
    """

    def __init__(self, *, skip_volume: bool = False) -> None:
        self.skip_volume = skip_volume
        self.claimed: set[tuple[str, str, str]] = set()
        self.lock = threading.Lock()

    def take(self, root: str, timeframe: str) -> Path | None:
        """The next script this instrument's chart still needs, claimed for this engine. None when there is none."""
        root = str(root or "ES").upper()
        with _held_lock:
            held = set(_held_back) | set(_engine_parked)
        with self.lock:
            # A script held out, or already open on this chart's engine, does not keep this engine from createdStrategies.
            busy = held | {key for key in self.claimed if key[2] == root}
        # The folder scan stats every file, which is slow on the desk's bind mount, so it runs outside the
        # lock: 22 engines would otherwise queue behind each other's scans. The claim is checked again under it.
        candidates = [path for path in jobs_for_chart(timeframe, skip_volume=self.skip_volume, held=busy, root=root)
                      if not is_held(held, path, timeframe, root)]
        with self.lock:
            for path in candidates:
                key = (str(path), timeframe, root)
                if key in self.claimed:
                    continue
                self.claimed.add(key)
                return path
            return None

    def release(self, path: Path, root: str, timeframe: str) -> None:
        with self.lock:
            self.claimed.discard((str(path), timeframe, str(root or "ES").upper()))


def study_parameters(base: dict, execute, account_size: float = ACCOUNT_SIZE, limit: int = ATTEMPT_LIMIT) -> list[dict]:
    """Build off the best result so far: the lower drawdown first, then the larger profit."""
    return _search(base, execute, account_size, limit, None, None, None)


def _focus_name(previous: dict, row: dict) -> str | None:
    from futuresfund.research_rules import focus_keys

    changed = focus_keys(previous.get("params") or {}, row.get("params") or {})
    return changed[0] if len(changed) == 1 else None


def _adjustment_order(params: dict, choices: dict | None = None) -> list[str]:
    """Stop, then take profit, then the other inputs in the script."""
    stops, targets, rest = [], [], []
    for name in _input_order(params, choices):
        if _is_stop_name(name):
            stops.append(name)
        elif _is_target_name(name):
            targets.append(name)
        else:
            rest.append(name)
    return stops + targets + rest


def _phase_name(name: str | None) -> str:
    if name and _is_stop_name(name):
        return "Stop"
    if name and _is_target_name(name):
        return "Take profit"
    return "Script"


def _same_direction(old, new, proposed) -> bool | None:
    if isinstance(old, bool) or not isinstance(old, (int, float)):
        return None
    if isinstance(new, bool) or not isinstance(new, (int, float)):
        return None
    if isinstance(proposed, bool) or not isinstance(proposed, (int, float)):
        return None
    delta = new - old
    step = proposed - new
    if delta == 0 or step == 0:
        return None
    return delta * step > 0


def _closest_row(trials: list[dict], account_size: float) -> dict:
    from futuresfund.objective import goal_distance

    chosen = trials[-1]
    best = goal_distance(chosen, account_size)
    for row in trials:
        distance = goal_distance(row, account_size)
        if distance is None:
            continue
        if best is None or distance > best:
            chosen = row
            best = distance
    return chosen


def _failed_reversal(trials: list[dict], index: int, focus: str) -> bool:
    """The step at index went the other way from the step before it on the same input."""
    if index < 2:
        return False
    earlier = _focus_name(trials[index - 2], trials[index - 1])
    if earlier != focus:
        return False
    old = (trials[index - 2].get("params") or {}).get(focus)
    mid = (trials[index - 1].get("params") or {}).get(focus)
    new = (trials[index].get("params") or {}).get(focus)
    return _same_direction(old, mid, new) is False


def _done_names(trials: list[dict], account_size: float) -> set[str]:
    """Inputs that no longer move the result toward the goal.

    A script input that stops helping opens the stop and take profit again.
    """
    from futuresfund.objective import scale_move

    done_exits: set[str] = set()
    done_script: set[str] = set()
    for index in range(1, len(trials)):
        focus = _focus_name(trials[index - 1], trials[index])
        if not focus:
            continue
        move = scale_move(trials[index - 1], trials[index], account_size)
        done = done_exits if _is_exit_name(focus) else done_script
        if move == "closer":
            done.discard(focus)
            continue
        if move == "flat" or _failed_reversal(trials, index, focus):
            done.add(focus)
            if not _is_exit_name(focus):
                done_exits.clear()
    return done_exits | done_script


def _open_name(params: dict, choices: dict | None, done: set[str]) -> str | None:
    for name in _adjustment_order(params, choices):
        if name not in done:
            return name
    return None


def _exit_is_profitable(row: dict) -> bool:
    """A stop or take profit may grow only after a profit that stays under the drawdown line."""
    from futuresfund.objective import account_survives

    return _profit(row) > 0 and account_survives(row)


def _lower_exit(anchor: dict, name: str, seen: set[str]) -> dict | None:
    """Move one take profit down by $50. It stays one step above the stop."""
    from futuresfund.research_rules import exit_step, hold_reward, reward_pair

    if not _is_target_name(name):
        return None
    step = exit_step(name)
    current = anchor.get(name)
    if step is None or isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    nxt = float(current) - float(step)
    pair = reward_pair(anchor)
    floor = float(step)
    if pair and pair[1] == name:
        floor = float(anchor[pair[0]]) + float(step)
    if nxt < floor - 1e-9:
        return None
    if isinstance(current, int) and not isinstance(current, bool) and float(step) >= 1:
        nxt = int(round(nxt))
    else:
        nxt = round(nxt, 4)
    if nxt == current or nxt <= 0:
        return None
    plan = dict(anchor)
    plan[name] = nxt
    plan = hold_reward(plan)
    if plan is None or plan.get(name) == current or _key(plan) in seen or _dead_move(name, current, plan.get(name)):
        return None
    return plan


def _lower_first_target(anchor: dict, seen: set[str], choices: dict | None, skip: set[str] | None = None) -> dict | None:
    """The next take profit that can still fall by $50."""
    from futuresfund.research_rules import exit_step

    skipped = skip or set()
    for name in _adjustment_order(anchor, choices):
        if name in skipped or not _is_target_name(name) or exit_step(name) is None:
            continue
        plan = _lower_exit(anchor, name, seen)
        if plan:
            return plan
    return None


def _raise_exit(anchor: dict, name: str, seen: set[str]) -> dict | None:
    """Move one stop up by $50. The take profit stays larger than the stop."""
    from futuresfund.research_rules import exit_cap, exit_step, hold_reward

    step = exit_step(name)
    current = anchor.get(name)
    if step is None or isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    nxt = float(current) + float(step)
    cap = exit_cap(name)
    if cap is not None and nxt > float(cap) + 1e-9:
        return None
    if isinstance(current, int) and not isinstance(current, bool) and float(step) >= 1:
        nxt = int(round(nxt))
    else:
        nxt = round(nxt, 4)
    if nxt == current:
        return None
    plan = dict(anchor)
    plan[name] = nxt
    plan = hold_reward(plan)
    if plan is None or plan.get(name) == current or _key(plan) in seen or _dead_move(name, current, plan.get(name)):
        return None
    return plan


def _raise_first_exit(anchor: dict, seen: set[str], choices: dict | None, skip: set[str] | None = None) -> dict | None:
    """The next stop that can still rise by $50. A take profit is not raised."""
    from futuresfund.research_rules import exit_step

    skipped = skip or set()
    for name in _adjustment_order(anchor, choices):
        if name in skipped or not _is_stop_name(name) or exit_step(name) is None:
            continue
        plan = _raise_exit(anchor, name, seen)
        if plan:
            return plan
    return None


SWEEP_DOLLARS = 100.0


def _meets_profit_acceptance(row: dict) -> bool:
    """The profit number on the strategy library page. A smaller profit does not count."""
    from futuresfund.book import strategy_acceptance

    limits = strategy_acceptance()
    if not limits.get("use_profit"):
        return False
    try:
        profit = float(row.get("net_profit"))
        floor = float(limits.get("min_profit"))
    except (TypeError, ValueError):
        return False
    return profit > floor


def _opening_is_best(trials: list[dict]) -> bool:
    """The opening stop and take profit already clear the library profit. That pair stays."""
    if not trials or not _meets_profit_acceptance(trials[0]):
        return False
    params = trials[0].get("params") or {}
    return any(_is_exit_name(str(name)) for name in params)


def _indicator_started(trials: list[dict]) -> bool:
    """True once a run has changed an input other than the stop or the take profit."""
    origin = (trials[0].get("params") or {}) if trials else {}
    for row in trials[1:]:
        params = row.get("params") or {}
        for key, value in params.items():
            if key in origin and origin.get(key) != value and not _is_exit_name(str(key)):
                return True
    return False


def _sweep_amount(name: str, account_size: float) -> float | None:
    """One $100 step. A percent input steps by $100 of the account."""
    from futuresfund.research_rules import exit_step

    base = exit_step(name, account_size)
    if base is None:
        return None
    return round(float(base) * (SWEEP_DOLLARS / 50.0), 4)


def _typed_exit(current, nxt: float, step: float):
    if isinstance(current, int) and not isinstance(current, bool) and float(step) >= 1:
        return int(round(nxt))
    return round(float(nxt), 4)


def _start_on_the_ladder(params: dict, account_size: float = ACCOUNT_SIZE) -> dict:
    """A script written with only a $1,000 stop and a $1,500 target starts at $250 and $500. A micro starts at $50 and $100."""
    from futuresfund.research_rules import hold_reward, opening_stop_value

    updated = dict(params or {})
    for name, value in list(updated.items()):
        opened = opening_stop_value(name, value, account_size)
        if opened != value:
            updated[name] = opened
    return hold_reward(updated) or updated


def _raise_stop_hundred(anchor: dict, name: str, seen: set[str], account_size: float) -> dict | None:
    """Raise one stop to the next of $250, $500, and $700, or of $50, $100, and $150 on a micro."""
    from futuresfund.research_rules import exit_cap, exit_step, hold_reward, next_rung

    if not _is_stop_name(name):
        return None
    step = exit_step(name, account_size)
    current = anchor.get(name)
    if step is None or isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    cap = exit_cap(name)
    nxt = next_rung(name, current, account_size)
    if nxt is None:
        return None
    if cap is not None and nxt > float(cap) + 1e-9:
        if float(current) >= float(cap) - 1e-9:
            return None
        nxt = float(cap)
    nxt = _typed_exit(current, nxt, step)
    if nxt == current:
        return None
    plan = dict(anchor)
    plan[name] = nxt
    plan = hold_reward(plan)
    if plan is None or plan.get(name) == current or _dead_move(name, current, plan.get(name)):
        return None
    if _key(plan) in seen:
        return _raise_stop_hundred(plan, name, seen, account_size)
    return plan


def _raise_target_hundred(anchor: dict, name: str, seen: set[str], account_size: float) -> dict | None:
    """Raise one take profit by $250, through $2,000. It stays larger than the stop."""
    from futuresfund.research_rules import exit_cap, exit_step, hold_reward, next_rung, reward_pair

    if not _is_target_name(name):
        return None
    step = exit_step(name, account_size)
    current = anchor.get(name)
    if step is None or isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    cap = exit_cap(name)
    pair = reward_pair(anchor)
    base = float(current)
    if pair and pair[1] == name:
        base = max(base, float(anchor[pair[0]]))
    nxt = next_rung(name, base, account_size)
    if nxt is None:
        return None
    if cap is not None and nxt > float(cap) + 1e-9:
        if float(current) >= float(cap) - 1e-9:
            return None
        nxt = float(cap)
    nxt = _typed_exit(current, nxt, step)
    if nxt == current:
        return None
    plan = dict(anchor)
    plan[name] = nxt
    plan = hold_reward(plan)
    if plan is None or plan.get(name) == current or _dead_move(name, current, plan.get(name)):
        return None
    if _key(plan) in seen:
        return _raise_target_hundred(plan, name, seen, account_size)
    return plan


def _next_sweep(current: dict, seen: set[str], choices: dict | None, account_size: float) -> dict | None:
    """Raise the stop through $250, $500, and $700, then raise the take profit, which has no maximum."""
    for name in _adjustment_order(current, choices):
        if _is_stop_name(name):
            plan = _raise_stop_hundred(current, name, seen, account_size)
            if plan:
                return plan
    for name in _adjustment_order(current, choices):
        if _is_target_name(name):
            plan = _raise_target_hundred(current, name, seen, account_size)
            if plan:
                return plan
    return None


def _richest(trials: list[dict]) -> dict:
    """The highest profit in the exit set. A tie keeps the smaller drawdown."""

    def rank(row: dict) -> tuple[float, float]:
        try:
            drawdown = float(row.get("max_drawdown") or 0)
        except (TypeError, ValueError):
            drawdown = 0.0
        return (_profit(row), -drawdown)

    return max(trials, key=rank)


def _indicator_from_best(trials: list[dict], seen: set[str], choices: dict | None, seeds: dict | None) -> dict | None:
    """Change one indicator input on the most profitable stop and take profit."""
    best = _richest(trials)
    anchor = dict(best.get("params") or {})
    blocked = {name for name in _adjustment_order(anchor, choices) if _is_exit_name(name) or "trail" in str(name).lower()}
    name = _open_name(anchor, choices, blocked)
    if name is None:
        return None
    return _step_one(anchor, name, None, None, 1, seen, choices, seeds, dramatic=True, losing=_profit(best) < 0)


def _exit_phase_plan(trials: list[dict], seen: set[str], choices: dict | None, seeds: dict | None, account_size: float) -> dict | None:
    """A losing open walks the stop, then the take profit. The best pair is kept for the indicators."""
    if not trials or _opening_is_best(trials) or _indicator_started(trials):
        return None
    current = dict((trials[-1].get("params") or {}))
    if not any(_is_exit_name(str(name)) for name in current):
        return None
    plan = _next_sweep(current, seen, choices, account_size)
    if plan is not None:
        return plan
    return _indicator_from_best(trials, seen, choices, seeds)


def _exit_phase_reason(trials: list[dict], plan: dict) -> str:
    """Why this attempt is the next stop, take profit, or indicator."""
    current = dict((trials[-1].get("params") or {}) if trials else {})
    changed = [key for key in plan if plan.get(key) != current.get(key)]
    name = changed[0] if len(changed) == 1 else ""
    if name and _is_stop_name(name):
        from futuresfund.research_rules import dollar_list, stop_ladder

        return (
            f"The opening run is under the library profit. The stop rises through {dollar_list(stop_ladder())}. "
            "The take profit stays larger than the stop."
        )
    if name and _is_target_name(name):
        return (
            "The stop is at its cap. The take profit keeps rising. It has no maximum. "
            "The most profitable pair in this set is the one the indicators start from."
        )
    best = _richest(trials) if trials else {}
    params = best.get("params") or {}
    return (
        "This stop and take profit made the most profit in the set. "
        f"The next change is an indicator, starting from profit {best.get('net_profit')}."
        if params else
        "The next change is an indicator."
    )


def _scale_plan(base: dict, trials: list[dict], seen: set[str], choices: dict | None, seeds: dict | None, account_size: float) -> dict | None:
    """One script input after the stop and take profit have been measured.

    A profitable opening is not changed. A losing opening walks the stop by $100, then the take profit by $100.
    """
    from futuresfund.objective import scale_move

    last = trials[-1]
    current = dict(last.get("params") or base)
    if len(trials) == 1:
        blocked = {name for name in _adjustment_order(current, choices) if _is_exit_name(name)}
        name = _open_name(current, choices, blocked)
        return _step_one(current, name, None, None, 1, seen, choices, seeds, dramatic=True, losing=_profit(last) < 0)

    previous = trials[-2]
    focus = _focus_name(previous, last)
    move = scale_move(previous, last, account_size)
    old = (previous.get("params") or {}).get(focus) if focus else None
    new = current.get(focus) if focus else None
    done = _done_names(trials, account_size)
    if _opening_is_best(trials) or not _exit_is_profitable(last):
        done.update(name for name in _adjustment_order(current, choices) if _is_exit_name(name))
    from futuresfund.objective import walk_scale

    hard_loss = _profit(last) < 0 and walk_scale(trials, account_size) <= 0.2

    if focus and move == "closer" and focus not in done:
        plan = _step_one(current, focus, old, new, 1, seen, choices, seeds, dramatic=hard_loss)
        proposed = plan.get(focus) if plan else None
        if plan and _same_direction(old, new, proposed) is not False:
            return plan
        done.add(focus)

    if focus and move == "farther" and focus not in done and not _failed_reversal(trials, len(trials) - 1, focus):
        # Stepping back means stepping from the row before, unless that row never entered.
        anchor = dict(previous.get("params") or current) if _traded(previous) or not _traded(last) else dict(current)
        plan = _step_one(anchor, focus, old, new, -1, seen, choices, seeds)
        proposed = plan.get(focus) if plan else None
        if plan and _same_direction(old, new, proposed) is False:
            return plan
        done.add(focus)

    if focus and move == "flat":
        done.add(focus)
        if not _is_exit_name(focus) and not _indicator_started(trials) and not _opening_is_best(trials):
            done = {name for name in done if not _is_exit_name(name)}

    anchor = dict(_closest_row(trials, account_size).get("params") or current)
    name = _open_name(anchor, choices, done)
    if name is None and focus and not _is_exit_name(focus):
        name = _open_name(anchor, choices, {item for item in done if not _is_exit_name(item)})
    if name is None:
        return None
    dramatic = name != focus or hard_loss
    return _step_one(anchor, name, None, None, 1, seen, choices, seeds, dramatic=dramatic or move == "flat")


def _plan_inside_trade_loss(trials: list[dict], seen: set[str], choices: dict | None, seeds: dict | None) -> dict | None:
    """Step back from a change that let one trade lose more than $750.

    A wider stop can raise the profit and still be unfileable. The next attempt
    undoes that change instead of taking another step in the same direction.
    """
    from futuresfund.objective import trade_loss_ok

    if not trials or trade_loss_ok(trials[-1]):
        return None
    last = trials[-1]
    current = dict(last.get("params") or {})
    if len(trials) > 1:
        previous = trials[-2]
        focus = _focus_name(previous, last)
        if focus:
            old = (previous.get("params") or {}).get(focus)
            new = current.get(focus)
            anchor = dict(previous.get("params") or current)
            plan = _step_one(anchor, focus, old, new, -1, seen, choices, seeds)
            if plan:
                return plan
    for name in _input_order(current, choices):
        if not _is_stop_name(name):
            continue
        nxt = _scaled(current.get(name), 0.5)
        if nxt is None:
            continue
        plan = dict(current)
        plan[name] = nxt
        if _key(plan) not in seen and len(_changed_keys(current, plan)) == 1:
            return plan
    return None


def next_plan(origin: dict, trials: list[dict], limit: int = ATTEMPT_LIMIT, *, extras: bool = True, extra_seen: set | None = None, choices: dict | None = None, seeds: dict | None = None, account_size: float = ACCOUNT_SIZE) -> dict | None:
    """Change one input. Keep a direction that moves toward the goal. Leave it when it does not."""
    if len(trials) >= limit:
        return None
    seen = {_key(row.get("params") or {}) for row in trials}
    if extra_seen:
        seen.update(extra_seen)
    base = {key: value for key, value in origin.items() if _searchable(key, value, choices)}
    if not trials:
        return dict(base)

    capped = _plan_inside_trade_loss(trials, seen, choices, seeds)
    if capped:
        return capped
    phase = _exit_phase_plan(trials, seen, choices, seeds, account_size)
    if phase is not None:
        return phase

    planned = _scale_plan(base, trials, seen, choices, seeds, account_size)
    if planned:
        return planned

    last = trials[-1]
    previous = trials[-2] if len(trials) > 1 else None
    if not _traded(last):
        revived = _best_trading_row(trials)
        if revived is not None:
            # The last run never entered, and every later zero repeats it, so stepping from it only repeats the zero.
            # The climb starts again from the best run that traded.
            last = revived
            previous = None
    if previous is not None and not _traded(previous) and _traded(last):
        # The row before never entered. The climb starts from this trading row, not from that dead setting.
        previous = None
    current = dict(last.get("params") or base)
    focus = None
    direction = 1
    anchor = current
    old = None
    new = None
    choice = "continue"
    if previous is not None:
        from futuresfund.research_rules import focus_keys

        changed = focus_keys(previous.get("params") or {}, current)
        if len(changed) == 1:
            focus = changed[0]
            old = (previous.get("params") or {}).get(focus)
            new = current.get(focus)
            others = [name for name in _input_order(current, choices) if name != focus]
            choice = _climb_choice(previous, last, bool(others), focus, account_size)
            if choice == "continue":
                direction = 1
                anchor = current
            else:
                direction = -1 if choice == "reverse" else 0
                anchor = dict(previous.get("params") or base)
    if _should_cut_trades(last, account_size):
        if focus and _is_tighten_name(focus) and choice == "continue":
            plan = _step_one(anchor, focus, old, new, direction, seen, choices, seeds, dramatic=True)
            if plan:
                return plan
        if choice != "reverse":
            plan = _tighten_plan(anchor, seen, choices)
            if plan:
                return plan
    losing = _profit(last) < 0
    widen = bool(focus) and _is_stop_name(focus) and losing and choice == "continue" and not _opening_is_best(trials)
    if _opening_is_best(trials) and focus and _is_exit_name(focus):
        focus = None
        choice = "switch"
    if choice != "switch":
        plan = _step_one(
            anchor, focus, old, new, direction, seen, choices, seeds,
            dramatic=_needs_drama(previous, last, focus) if previous is not None else False,
            losing=losing,
            widen=widen,
        )
        if plan:
            return plan
    for name in _input_order(anchor, choices):
        if name == focus or (_opening_is_best(trials) and _is_exit_name(name)):
            continue
        plan = _step_one(anchor, name, None, None, 1, seen, choices, seeds, losing=losing)
        if plan:
            return plan
    widened = _widen(base, anchor, seen, len(trials), extras=extras)
    if widened is not None:
        return widened
    return _force_new(anchor, seen, choices)


class _Trials(list):
    """The different results, plus how many times the engine actually ran.

    attempt_budget is the limit the search had. A stage search also keeps best_pass, its passing run
    with the most profit after drawdown, and patience_stop when it ended for finding no better pass.
    """

    engine_runs = 0
    attempt_budget = 0
    best_pass = None
    patience_stop = False
    # Set when the trade cut ran out of steps or attempts: the chart researcher's question for the owner.
    over_trades = ""
    # Set with skipped when the engine's Restart, not Skip, cut the search. The stage resumes there.
    restarted = False


def _from_best(trials: list, bounds: dict, seen: set, choices: dict | None = None, hold_exits: bool = False) -> dict | None:
    """One unused setting on the best result so far, closest values first.

    The local step can run out while the input limits still have room. The next
    run keeps the settings that ranked highest, which is how a lower drawdown
    stays in place, and moves one input that has not been tried.
    """
    if not trials:
        return None
    best = max(trials, key=_rank)
    anchor = dict(best.get("params") or {})
    if not anchor or (not bounds and not choices):
        return None
    for name in _input_order(anchor, choices):
        if hold_exits and _is_exit_name(name):
            continue
        current = anchor.get(name)
        if isinstance(current, str):
            for nxt in (choices or {}).get(name) or []:
                if nxt == current:
                    continue
                plan = dict(anchor)
                plan[name] = nxt
                if _key(plan) not in seen:
                    return plan
            continue
        low, high = bounds.get(name, (None, None))
        for nxt in _legal_steps(current, low, high):
            plan = dict(anchor)
            plan[name] = nxt
            if plan[name] == current or _key(plan) in seen:
                continue
            return plan
    return None


def _legal_steps(value, low, high) -> list:
    """Values inside the input limits, nearest to the current value first."""
    if low is None and high is None:
        return []
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    if isinstance(value, int) and not isinstance(value, bool):
        start = int(low) if low is not None else int(value)
        end = int(high) if high is not None else int(value)
        if end < start:
            start, end = end, start
        return sorted(range(start, end + 1), key=lambda item: (abs(item - int(value)), item))
    start = float(low) if low is not None else float(value)
    end = float(high) if high is not None else float(value)
    if end < start:
        start, end = end, start
    steps = []
    cursor = start
    while cursor <= end + 1e-9:
        steps.append(round(cursor, 4))
        cursor += 0.25
    return sorted(steps, key=lambda item: (abs(item - float(value)), item))


def _took(started: float) -> str:
    """How long a chart's attempts have been running, in plain words."""
    seconds = max(0, int(time.monotonic() - started))
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} minutes {sec} seconds"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hours {minutes} minutes"


def _reasoned_next(board, path, script: str, trials: list, bounds: dict, choices: dict, why: str = "", response: dict | None = None, redesign: bool = False) -> dict | None:
    """The researcher's decision. None keeps the mechanical step."""
    if board is None or path is None or not trials:
        return None
    try:
        from futuresfund.attempt_reason import reasoned_change

        return reasoned_change(script, trials, bounds, choices, why, response, redesign)
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


# The researcher's questions, one at a time on the shared model. A max thinking pass runs for
# minutes and waits behind other desks, so no engine waits for one. The engine backtests, and
# the answer is used the next time a chart opens that script.
# A failed stage goes first, because it is the question that can bring a redesign. A stop or take profit that does not work comes next.
_decisions: PriorityQueue = PriorityQueue()
_decisions_turn = itertools.count()
_decisions_open: set[str] = set()
_decisions_lock = threading.Lock()
_decisions_worker: threading.Thread | None = None


def _ensure_worker() -> None:
    global _decisions_worker
    with _decisions_lock:
        if _decisions_worker is None or not _decisions_worker.is_alive():
            _decisions_worker = threading.Thread(target=_decisions_loop, daemon=True, name="chart-researcher")
            _decisions_worker.start()


def _decisions_loop() -> None:
    while True:
        _run_decision(_decisions.get()[2])


def drain_decisions() -> None:
    """Answer every waiting question on this thread."""
    while True:
        try:
            job = _decisions.get_nowait()[2]
        except Empty:
            return
        _run_decision(job)


def _run_decision(job: dict) -> None:
    try:
        if callable(job.get("call")):
            # A researcher job from questions.py: wording a question for the owner, or applying the owner's answer.
            job["call"]()
            return
        _decide(job)
    except Exception:
        traceback.print_exc()
    finally:
        with _decisions_lock:
            _decisions_open.discard(job["key"])


def ask_researcher(board, path, script: str, trials: list, facts: dict, why: str, response: dict | None, researcher: str, kind: str, urgent: bool = False, review: str = "") -> bool:
    """Queue a question for the researcher and return at once. One of each kind waits per script. review names a failed stage on one chart."""
    if board is None or path is None or not trials:
        return False
    from futuresfund.script_diagnosis import script_key

    key = f"{script_key(script)}|{kind}"
    with _decisions_lock:
        if key in _decisions_open:
            return False
        _decisions_open.add(key)
    _decisions.put((-1 if kind.startswith("stage") else 0 if urgent else 1, next(_decisions_turn), {
        "key": key,
        "kind": kind,
        "review": review,
        "board": board,
        "path": path,
        "script": script,
        "trials": [dict(row) for row in trials],
        "bounds": _input_bounds(facts),
        "choices": _input_choices(facts),
        "why": why,
        "response": response,
        "researcher": researcher,
    }))
    _ensure_worker()
    return True


def _decide(job: dict) -> None:
    """Ask the model, then file the answer for the next chart that opens this script."""
    from futuresfund import redesign as redesigns
    from futuresfund import script_diagnosis as diagnosis

    board = job["board"]
    if board.cancel.is_set():
        return
    # A redesign answers a measured failure. A first read or a repair has not measured one.
    # A repair answer may rewrite the script only while ALLOW_REPAIR_REWRITES is on. Off, it keeps
    # new input values only, like the other answers under VALUES_ONLY.
    values_only = (not ALLOW_REPAIR_REWRITES) if job["kind"] == "repair" else VALUES_ONLY
    can_redesign = (job["kind"] == "stall" or job["kind"].startswith("stage")) and redesigns.room(job["path"]) and not values_only
    why = " ".join([job["why"], *redesigns.earlier_lines(job["path"])]) if can_redesign else job["why"]
    # What each input did when the search moved it, so the answer works on the inputs that matter.
    why = " ".join([why, *input_effects.script_lines(job["path"])])
    if values_only:
        why = " ".join([why, VALUES_ONLY_LINE])
    answer = _reasoned_next(board, job["path"], job["script"], job["trials"], job["bounds"], job["choices"], why, job["response"], can_redesign)
    if answer and values_only:
        # A changed script is not used. Only new input values are kept.
        answer.pop("redesigned", None)
        answer.pop("script", None)
    if answer and answer.get("redesigned"):
        # The redesign replaces this script on its next chart, measured like any rewrite and taken back when it does worse.
        if redesigns.note_redesign(job["path"], str(answer.get("because") or ""), str(answer.get("redesign") or "")):
            answer["script"] = redesigns.keep_title(answer["redesigned"], job["script"])
            answer["in_place"] = True
        answer.pop("redesigned", None)
    with diagnosis.turn(job["script"]):
        record = diagnosis.load(job["script"])
        if job["kind"] == "read":
            # A model that did not answer gets one more chance, on the next chart that reads this script.
            record["asks"] = int(record.get("asks") or 0) + 1
            record["read"] = answer is not None or record["asks"] >= 2
        elif job["kind"] == "repair":
            record["repairs"] = int(record.get("repairs") or 0) + 1
        elif job["kind"] == "stall":
            record["stalled"] = True
        elif job["kind"].startswith("stage"):
            record["reviewed"] = sorted({*(str(item) for item in record.get("reviewed", [])), job.get("review") or job["kind"][5:]})
        if answer and (answer.get("script") or answer.get("params")):
            record["answer"] = {
                "because": answer.get("because") or "",
                "script": answer.get("script"),
                "params": answer.get("params"),
                "settings": dict(job["trials"][-1].get("params") or {}),
                "redesign": bool(answer.get("in_place")),
            }
        diagnosis.save(job["script"], record)
    if answer:
        board.post(job["researcher"], f"{job['path'].name}: {answer.get('because')}", kind="report", channel=job["researcher"])
        if answer.get("in_place"):
            board.post(job["researcher"], f"{job['path'].name} has a redesign waiting. Its next chart measures it against this script and keeps it only when it does better.", kind="report", channel=job["researcher"])
        if (answer.get("script") or answer.get("params")) and REOPEN_FOR_ANSWERS and READ_BEFORE_TESTING:
            # Off, the answer waits in the script's record for the next chart that opens it. With the read
            # before testing off, no chart would use the answer, so reopening would rerun every chart for nothing.
            _reopen_for_answer(board, job["path"], job["researcher"])


def _reopen_for_answer(board, path: Path, researcher: str) -> bool:
    """A finished chart never opens the script again, so it would never use the answer. Its charts run again.

    The old results stay in researchNotes/before_repair. A script reopens at most REOPEN_LIMIT times.
    """
    with _notes_lock:
        notes = _read_notes(path)
        count = int(notes.get("reopened") or 0)
        if not notes.get("instruments") and not notes.get("trials"):
            return False
        if count >= REOPEN_LIMIT:
            board.post(researcher, f"{path.name} has been repaired and tested again {count} times, so the latest answer waits for its next chart.", kind="log", channel=researcher)
            return False
        if not path.is_file() and (LEARNED / path.name).is_file():
            shutil.move(str(LEARNED / path.name), str(path))
        slug = path.stem.replace(" ", "_")
        keep = NOTES / "before_repair"
        keep.mkdir(parents=True, exist_ok=True)
        (keep / f"{slug}_{count + 1}.json").write_text(_notes_json(notes), encoding="utf-8")
        fresh = {key: value for key, value in notes.items() if key not in {"instruments", "trials", "best"}}
        (NOTES / f"{slug}.json").write_text(_notes_json({**fresh, "reopened": count + 1}), encoding="utf-8")
    _forget_resume(path)
    board.post(researcher, f"{path.name} has the researcher's repair waiting, so its charts run again.", kind="report", channel=researcher)
    return True


def _forget_resume(path: Path) -> None:
    """No stopped stage of this file resumes: every chart runs again from the 7-day stage after an answer,
    or the file was parked in cannotRunStrategies."""
    try:
        from futuresfund.proving import drop_resume

        drop_resume(path.name)
    except (OSError, ValueError, TypeError):
        pass


def _plan_mark(raw: dict, bounds: dict, execute) -> str:
    plan = _clamp_plan(raw, bounds) or raw
    holder = getattr(execute, "source", None)
    stamp = int(holder.get("stamp") or 0) if isinstance(holder, dict) else 0
    key = _key(plan)
    return key if stamp == 0 else f"{stamp}:{key}"


def _search(base, execute, account_size, limit, board, path, pause, facts=None, extras: bool = True, developer: str = "2min chart developer", researcher: str = "Chart researcher", until_profitable: bool = False, accept=None, slot: int | None = None, source: str = "", stage_days: int | None = None, year: bool = False):
    """The search below, with a record of what each input did.

    An input whose moves changed nothing is taken out of this search (input_effects), and what every
    move did is written on the script's input file once, when the search ends. year marks the
    chart's one-year search, the only search with the loss stop.
    """
    holder = getattr(execute, "source", None)
    chart = ""
    text = source or ""
    if isinstance(holder, dict):
        chart = f"{holder.get('root') or ''} {holder.get('timeframe') or ''}".strip()
        text = text or str(holder.get("text") or "")
    window = f"{stage_days}-day stage" if stage_days else "one-year search"
    try:
        from futuresfund.delta_core import has_core

        core = has_core(text)
    except Exception:
        core = False
    # Like the research notes, the record is written for a search the desk runs, the one with a board.
    tracker = input_effects.Tracker(path if board is not None else None, chart, window, core=core)
    previous = input_effects.begin(tracker)
    from futuresfund.research_rules import chart_root

    root = str(holder.get("root") or "") if isinstance(holder, dict) else ""
    try:
        with chart_root(root):
            trials = _search_inputs(
                base, execute, account_size, limit, board, path, pause, facts, extras=extras, developer=developer,
                researcher=researcher, until_profitable=until_profitable, accept=accept, slot=slot, source=source, stage_days=stage_days,
                year=year,
            )
    finally:
        input_effects.end(previous)
    tracker.finish()
    if tracker.removed and board is not None and path is not None:
        names = ", ".join(sorted(tracker.removed))
        where = f" on {chart}" if chart else ""
        board.post(
            researcher,
            f"{path.name}: moving {names} changed nothing in the {window}{where}, so that search left "
            f"{'it' if len(tracker.removed) == 1 else 'them'} where {'it was' if len(tracker.removed) == 1 else 'they were'} "
            "and spent its attempts on the inputs that change the result.",
            kind="log",
            channel=researcher,
        )
    return trials


def _search_inputs(base, execute, account_size, limit, board, path, pause, facts=None, extras: bool = True, developer: str = "2min chart developer", researcher: str = "Chart researcher", until_profitable: bool = False, accept=None, slot: int | None = None, source: str = "", stage_days: int | None = None, year: bool = False):
    """Move one input at a time. `accept` is the result the search stops at; `until_profitable` stops at the first profit.

    stage_days is a proving stage on the last that many days. A run that passes objective.stage_passed
    does not end it: the stage keeps searching for a pass with more profit and less drawdown
    (_pass_score), and stops when its `limit` is spent or STAGE_PATIENCE runs in a row found no better
    pass. trials.best_pass is that pass. A stage that never passes spends its whole limit. A stage run
    is not documented, counted, or offered to the library: those read a one-year result.

    year marks the chart's one-year search. Only that search leaves a script that is still a loss
    after LOSS_LOOK results, whatever the limit is set to.
    """
    if accept is None and until_profitable:
        accept = _row_profitable
    started = time.monotonic()
    from futuresfund.research_rules import is_daily_limit, prepare_research

    original_daily = {key: value for key, value in base.items() if is_daily_limit(key)}
    # The trade cut on this chart (kept on the executor, so it carries from stage to stage).
    cut = _cut_state(execute)
    if cut.get("active"):
        # A cut still on when the last search ended (or started from the notes) goes on from this search's
        # own start, the settings the stage before passed with, not from that search's over-cap settings.
        # Each search gets its own TRADE_CUT_ATTEMPTS. The steps already taken stay taken.
        cut["used"] = 0
        cut["anchor"] = None
    holder_start = getattr(execute, "source", None)
    if isinstance(holder_start, dict) and int(holder_start.get("stamp") or 0) > 0 and holder_start.get("text"):
        # The copy this chart runs was changed (a wider cooldown maxval, the desk trade limits, a rewrite).
        # Its bounds come from that copy, so the clamps do not snap a widened value back at the next stage.
        facts = pine_facts(str(holder_start["text"]))
        if source:
            source = str(holder_start["text"])
    # A daily limit the trade cut turned on stays on: zeroing it would bring the trades back.
    kept_daily = dict(original_daily) if cut.get("touched") else {}
    base = {**prepare_research(base), **kept_daily}
    base = _start_on_the_ladder(base, account_size)
    # The owner's emailed answers (questions.owner_settings) start every chart's search, after prepare_research,
    # so a daily limit the owner turned on is not zeroed. Applied once per chart study, not again at each stage.
    base, facts, source = _with_owner_answers(base, execute, path, facts, source, cut, board, researcher)
    choices = _input_choices(facts)
    # The search does not spend attempts on an input the script was measured to ignore.
    known = getattr(execute, "source", None)
    # The zero-trade count is kept per chart and instrument, so one chart cannot park the file for all.
    chart_key = f"{known.get('root') or ''}|{known.get('timeframe') or ''}" if isinstance(known, dict) else ""
    ignored = set(known.get("flat") or ()) if isinstance(known, dict) else set()
    responsive = {key: value for key, value in base.items() if key not in ignored}
    if any(_searchable(key, value, choices) for key, value in responsive.items()):
        base = responsive
    origin = {key: value for key, value in base.items() if _searchable(key, value, choices)}
    trials = _Trials()
    study_name = path.name if path is not None else "the script"
    if slot is not None:
        from futuresfund.backtrader_engine import begin_study

        # The engine card shows "attempt N of M" and which test this search is.
        begin_study(slot, study_name, SUCCESS_COUNT, limit=limit, stage=int(stage_days) if stage_days else (365 if year else 0))
    # A stage keeps its best pass so far, and counts the runs since that pass that did not beat it.
    best_pass = None
    since_best = 0
    seen = set()
    seen_results = set()
    blocked = set()
    carried = None
    spent: set[str] = set()
    # For each input, the moves from a trading setting that stopped every entry: (from, to).
    dead_edges: dict[str, list[tuple]] = {}
    repeats = 0
    runs = 0
    bounds = _input_bounds(facts)
    script = source or ""
    if not script and path is not None:
        try:
            script = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            script = ""
    # A session or plain text input the owner set is not searched, so every plan carries its value.
    from futuresfund.pine_report import text_inputs

    text_named = text_inputs(str(_holder_text(execute) or script or ""))
    pinned_text = {name: value for name, value in base.items() if isinstance(value, str) and name in text_named}
    if script and board is not None and path is not None:
        from futuresfund.frequency import script_reading

        board.post(researcher, script_reading(script, origin), kind="log", channel=researcher)
    script_errors = 0
    stall_asked = False
    no_effect: list[str] = []
    tracker = input_effects.current()
    # Plans in a direction this search found does nothing are skipped. This many in a row ends the search.
    skipped_dead = 0
    while len(trials) < limit:
        if board is not None and board.cancel.is_set():
            break
        if slot is not None:
            from futuresfund.backtrader_engine import clear_skip, skipped

            if skipped(slot):
                trials.skipped = True
                from futuresfund.backtrader_engine import clear_restart, restarted

                was_restart = restarted(slot)
                # Restart cuts the search like Skip, but the study ends as restarted and its stage resumes.
                trials.restarted = bool(was_restart)
                clear_skip(slot)
                clear_restart(slot)
                if board is not None:
                    if was_restart and (stage_days or year):
                        board.post(
                            developer,
                            f"Engine {slot + 1} restarted. {study_name} resumes at this stage on its next turn. Taking the next script.",
                            kind="log",
                            channel=developer,
                        )
                    elif was_restart:
                        # A search outside the proving stages (an engine 4 draft) has no stage to resume.
                        board.post(developer, f"Engine {slot + 1} restarted. Taking the next script.", kind="log", channel=developer)
                    else:
                        board.post(
                            developer,
                            f"{study_name} was skipped. Taking the next script.",
                            kind="log",
                            channel=developer,
                        )
                break
        if path is not None and not _in_learning(path) and not _in_created(path):
            if board is not None:
                board.post(
                    developer,
                    _left_text(path),
                    kind="log",
                    channel=developer,
                )
            # Another chart moved the file. This search was cut off, so it did not fail.
            trials.skipped = True
            break
        if pause is not None and not _wait(board, pause):
            break
        attempt = len(trials) + 1
        label = path.name if path is not None else "the script"

        if board is not None:
            board.log(researcher, f"Choosing settings for attempt {attempt} on {label}")
        bounds = _input_bounds(facts)
        choices = _input_choices(facts)
        from futuresfund.frequency import frequency_plan, frequency_reason

        if carried is None and trials and not _traded(trials[-1]):
            # The last run never entered. The next change starts from the best run that traded, not from that setting.
            revived = _best_trading_row(trials)
            if revived is not None:
                carried = dict(revived.get("params") or {}) or None
        anchor = carried or ((trials[-1].get("params") if trials else None) or origin)
        cut_text = None
        cut_attempt = False
        if cut.get("active"):
            if int(cut.get("used") or 0) >= TRADE_CUT_ATTEMPTS:
                trials.over_trades = _over_trades_question(label, cut, stage_days, known)
                _post_cut(board, path, researcher, trials.over_trades, kind="report")
                break
            picked = _next_cut(cut, anchor, script, bounds, seen, original_daily, trials, holder_text=_holder_text(execute))
            if picked is None:
                trials.over_trades = _over_trades_question(label, cut, stage_days, known, spent_steps=True)
                _post_cut(board, path, researcher, trials.over_trades, kind="report")
                break
            cut_plan, cut_text, cut_said = picked
            cut_attempt = True
        phase = None if cut_attempt else (_exit_phase_plan(trials, seen | blocked, choices, original_daily, account_size) if trials else None)
        reason = ""
        dead = False
        raw = None
        if cut_attempt:
            raw = cut_plan
            reason = f"To cut trades I {cut_said}"
            cut["touched"] = True
            holder_now = getattr(execute, "source", None)
            if cut_text is not None and isinstance(holder_now, dict) and cut_text != str(holder_now.get("text") or ""):
                # The desk trade limits, or a wider cooldown maxval, go in the copy this chart runs and files.
                holder_now["previous"] = str(holder_now.get("text") or "")
                holder_now["text"] = cut_text
                holder_now["stamp"] = int(holder_now.get("stamp") or 0) + 1
                script = cut_text
                facts = pine_facts(script)
                bounds = _input_bounds(facts)
                choices = _input_choices(facts)
        elif raw is None and phase is not None:
            raw = phase
            reason = _exit_phase_reason(trials, phase)
        elif raw is None:
            paced_change = frequency_plan(origin, trials, script, bounds, seen | blocked, carried, spent | set(tracker.removed if tracker is not None else ()))
            if paced_change is not None:
                raw = paced_change
                reason = frequency_reason(trials[-1].get("params") if trials else {}, paced_change, script)
            elif trials and script and all(_no_trades(row) for row in trials):
                raw = None
                dead = True
            else:
                raw = next_plan(
                    origin, trials, limit, extras=extras, extra_seen=blocked, choices=choices,
                    seeds=original_daily, account_size=account_size,
                )
        if raw is None and not dead:
            raw = _from_best(trials, bounds, seen | blocked, choices, hold_exits=_opening_is_best(trials))
        if raw is None:
            if trials and all(_no_trades(row) for row in trials):
                reason = _flat_reason(path)
                trials[-1]["note"] = reason
                trials[-1]["skipped"] = True
                if board is not None and path is not None:
                    board.post(developer, f"{label}: {reason}", kind="report", channel=developer)
                    board.post(researcher, f"{label}: {reason}", kind="report", channel=researcher)
            elif tracker is not None and tracker.removed and not stage_days and developer in _BACKTEST_DEVELOPERS and not _input_order(anchor, choices):
                # Every input this search could move changed nothing, the way three identical results used to show.
                trials.repeat_stop = True
                if board is not None and path is not None:
                    board.post(
                        developer,
                        f"{label}: moving each of its inputs changed nothing. This chart is leaving the script and taking the next one.",
                        kind="log",
                        channel=developer,
                    )
            break
        plan = _clamp_plan(raw, bounds)
        if pinned_text:
            plan = {**pinned_text, **plan}
        holder = getattr(execute, "source", None)
        stamp = int(holder.get("stamp") or 0) if isinstance(holder, dict) else 0
        mark = _key(plan) if stamp == 0 else f"{stamp}:{_key(plan)}"
        if mark in seen:
            blocked.add(_key(raw))
            blocked.add(mark)
            continue
        moved = _changed_keys(anchor, plan)
        if cut_attempt:
            # A cut is the researcher's deliberate step. It runs even where a move looked like it did nothing.
            pass
        elif tracker is not None and moved and all(tracker.is_removed(name) or tracker.dead_move(name, anchor.get(name), plan.get(name)) for name in moved):
            blocked.add(_key(raw))
            blocked.add(mark)
            skipped_dead += 1
            if skipped_dead > 40:
                break
            continue
        elif _past_any_dead_edge(dead_edges, plan, trials):
            # Moving this input this far already stopped every entry, so going further cannot bring trades back.
            blocked.add(_key(raw))
            blocked.add(mark)
            skipped_dead += 1
            if skipped_dead > 40:
                break
            continue
        skipped_dead = 0
        seen.add(mark)
        if board is not None and path is not None:
            change = _change_text(trials[-1]["params"] if trials else None, plan)
            if reason:
                change = f"{change}. {reason}"
            if stage_days:
                change = f"{change}. {stage_days}-day stage"
            board.post(
                researcher,
                f"Attempt {attempt} of {limit} on {label}. {change}.",
                kind="log",
                channel=researcher,
            )
            board.post(
                developer,
                f"Attempt {attempt} of {limit} on {label}. {change}.",
                kind="log",
                channel=developer,
            )
        from futuresfund.backtrader_engine import record_research

        record_research(slot, "search_choices")
        measured = execute(plan) or {}
        if measured.get("script_error") and trials and script_errors < 5:
            # This script has run. The last rewrite or setting broke it, so that change is dropped and the search goes on.
            script_errors += 1
            blocked.add(mark)
            blocked.add(_key(raw))
            from futuresfund.attempt_reason import undo_rewrite

            restored = undo_rewrite(path, execute) if path is not None else None
            if restored:
                script = restored
                facts = pine_facts(script)
                bounds = _input_bounds(facts)
                choices = _input_choices(facts)
            if board is not None:
                undone = "The rewrite was taken back. " if restored else "That setting was dropped. "
                board.post(researcher, f"{label} stopped on attempt {attempt}: {measured.get('error')}. {undone}The search goes on.", kind="log", channel=researcher)
            continue
        if isinstance(holder, dict) and not measured.get("error"):
            holder.pop("previous", None)
        if measured.get("skipped"):
            trials.skipped = True
            was_restart = False
            if slot is not None:
                from futuresfund.backtrader_engine import clear_restart, clear_skip, restarted

                # Restart sets its flag before the skip flag that cut this backtest, so it is already set here.
                was_restart = restarted(slot)
                trials.restarted = bool(was_restart)
                clear_skip(slot)
                if was_restart:
                    clear_restart(slot)
            if board is not None:
                if was_restart and (stage_days or year):
                    board.post(
                        developer,
                        f"Engine {slot + 1} restarted. {label} resumes at this stage on its next turn. Taking the next script.",
                        kind="log",
                        channel=developer,
                    )
                elif was_restart:
                    # A search outside the proving stages (an engine 4 draft) has no stage to resume.
                    board.post(developer, f"Engine {slot + 1} restarted. Taking the next script.", kind="log", channel=developer)
                else:
                    board.post(developer, f"{label} was skipped. Taking the next script.", kind="log", channel=developer)
            break
        # A run over the trade cap is kept, flagged over_cap, and the trade cut starts or goes on.
        if stage_days and best_pass is not None and not cut.get("active"):
            # The stage already has a pass. A run over the cap does not start the trade cut: it is not a better
            # pass, and the next attempt starts again from the best pass.
            over = _over_after_pass(measured, stage_days, board, path, researcher, label, trials, best_pass)
        else:
            over = _track_trade_cut(cut, measured, plan, stage_days, cut_attempt, board, path, researcher, label, chart_key)
        runs += 1
        if _no_trades(measured):
            _record_dead_edges(dead_edges, plan, trials)
        learned = tracker.observe(stamp, plan, measured) if tracker is not None else []
        if stage_days and runs > 2 * limit:
            # A stage skips the repeat stop below, so its engine runs are capped here instead.
            break
        if not stage_days and _zero_streak(path, measured, chart_key) >= FLAT_RUNS:
            if any(_traded(row) for row in trials) or (isinstance(holder, dict) and holder.get("stage_runs")):
                # This script traded in this search or passed a proving stage, so it can run. The
                # settings walked into ones that never enter. This chart ends its search and keeps its
                # best result, and the file stays for the other charts.
                with _flat_lock:
                    _flat_runs.pop(_flat_key(path, chart_key), None)
                if board is not None and path is not None:
                    board.post(
                        developer,
                        f"{label} took no trades {FLAT_RUNS} times in a row on these settings, but it traded earlier in this search. "
                        "This chart keeps its best result and leaves the search. The script stays for the other charts.",
                        kind="log",
                        channel=developer,
                    )
                break
            _park_flat(board, path, developer, trials, label, chart_key, researcher)
            break
        signature = _result_key(measured)
        if signature in seen_results and not measured.get("error") and not measured.get("fatal"):
            # A repeat that showed an input does nothing taught the search where not to look.
            # That input is out of the search now, so it does not count toward leaving the script.
            if not learned:
                repeats += 1
            for name in _changed_keys(anchor, plan):
                if name not in no_effect:
                    no_effect.append(name)
            if repeats >= 3 and not cut.get("active") and not stall_asked and not _no_trades(measured) and board is not None and path is not None:
                stall_asked = True
                moved = ", ".join(no_effect[-12:]) or "the settings"
                _ask_about_stall(
                    board, path, script, trials, facts, researcher, known.get("response") if isinstance(known, dict) else None,
                    f"The last {repeats} runs on this script gave the same profit, drawdown, and trade count. "
                    f"These inputs changed with no effect: {moved}. Find out why before more settings are tried.",
                )
            elif _no_trades(measured) and repeats >= FLAT_RUNS:
                if trials and all(_no_trades(row) for row in trials):
                    reason = _flat_reason(path)
                    trials[-1]["note"] = reason
                    trials[-1]["skipped"] = True
                break
            # On a short window a stop or target often changes no trade. A stage moves on to another input instead of leaving.
            elif not _no_trades(measured) and developer in _BACKTEST_DEVELOPERS and repeats >= 3 and not stage_days and not cut.get("active"):
                trials.repeat_stop = True
                if board is not None:
                    board.post(
                        developer,
                        f"{label} returned the same profit, drawdown, and trade count {repeats} times in a row. "
                        "This chart is leaving the script and taking the next one.",
                        kind="log",
                        channel=developer,
                    )
                break
            blocked.add(mark)
            if _no_trades(measured):
                changed = _changed_keys(anchor, plan)
                if len(changed) == 1:
                    spent.add(changed[0])
                # A script that never entered keeps loosening from here. One that traded goes back to its best trading run.
                revived = _best_trading_row(trials)
                carried = dict(revived.get("params") or plan) if revived is not None else dict(plan)
            if board is not None and path is not None:
                if _no_trades(measured):
                    note = (
                        f"{label} still took no trades. Profit, drawdown, and the trade count did not change, "
                        "so this run is not counted."
                    )
                elif learned:
                    gone = [name for name in learned if tracker is not None and tracker.is_removed(name)]
                    note = (
                        f"{label} repeated an earlier result, so this run is not counted. "
                        f"Moving {', '.join(learned)} that way changed nothing, so this search does not move it that way again."
                        + (f" {', '.join(gone)} changed nothing either way and is out of this search." if gone else "")
                    )
                else:
                    note = (
                        f"{label} repeated an earlier result, so this run is not counted. "
                        "The same variable will be moved further."
                    )
                board.post(researcher, note, kind="log", channel=researcher)
            if stage_days and best_pass is not None:
                # A repeat cannot beat the best pass, so it counts toward the stage's patience.
                since_best += 1
                if since_best >= STAGE_PATIENCE:
                    trials.patience_stop = True
                    _post_patience(board, path, researcher, label, stage_days, trials, best_pass, limit)
                    break
            continue
        repeats = 0
        carried = None
        seen_results.add(signature)
        row = {
            "params": plan,
            "net_profit": measured.get("net_profit"),
            "max_drawdown": measured.get("max_drawdown"),
            "dips": measured.get("dips") or [],
            "trades": measured.get("trades"),
            "win_rate": measured.get("win_rate"),
            "sharpe": measured.get("sharpe"),
            "profit_factor": measured.get("profit_factor"),
            "profit_ratio": measured.get("profit_ratio"),
            "biggest_winner": measured.get("biggest_winner"),
            "biggest_loser": measured.get("biggest_loser"),
            "held_loser": measured.get("held_loser"),
            "stop_dollars": measured.get("stop_dollars"),
            "days": measured.get("days"),
            "bars": measured.get("bars"),
            "tested_from": measured.get("tested_from") or "",
            "tested_through": measured.get("tested_through") or "",
            "trades_per_day": measured.get("trades_per_day"),
            "error": measured.get("error"),
            "runner": measured.get("runner"),
            "stop_reason": measured.get("stop_reason") or "",
            "breached": bool(measured.get("breached")),
            "timeframe": measured.get("timeframe") or ENGINE_CHART,
            "frames": measured.get("frames") or [],
            "blocked": bool(measured.get("blocked")),
            "stopped": bool(measured.get("stopped")),
            "fatal": bool(measured.get("fatal")),
            "engine": measured.get("engine") or "",
            "instrument": measured.get("instrument") or "",
            "contract": measured.get("contract") or "",
            "spans": measured.get("spans") or {},
            "exits": measured.get("exits") or [],
            "clock": measured.get("clock") or {},
        }
        if path is not None and isinstance(holder, dict) and float(row.get("net_profit") or 0) > 0:
            from futuresfund.attempt_reason import keep_version

            row["source_sha"] = keep_version(path, str(holder.get("text") or ""))
        row["passed"] = _passed(row, account_size)
        if over:
            # Kept so the cut starts from these settings, but a run over the trade cap never passes or is filed.
            row["over_cap"] = True
            row["passed"] = False
        row["change"] = _change_text(trials[-1]["params"] if trials else None, plan)
        row["impact"] = _impact(trials[-1] if trials else None, row)
        if (row.get("error") or row.get("fatal")) and not _rejected_setting(row.get("error")):
            _journal_engine_error(developer, path, label, attempt, row)
        if row.get("take_profit") is None:
            row["take_profit"] = measured.get("take_profit", plan.get("tp_dollars"))
        if measured.get("note"):
            row["note"] = measured["note"]
        trials.append(row)
        if slot is not None:
            from futuresfund.objective import scale_move, walk_scale

            from futuresfund.backtrader_engine import record_attempt, record_scale

            record_attempt(slot, len(trials))

            earlier = trials[-2] if len(trials) > 1 else None
            move = "start" if earlier is None else scale_move(earlier, row, account_size)
            changed = _focus_name(earlier, row) if earlier is not None else None
            phase = "Start" if earlier is None else _phase_name(changed)
            position = walk_scale(trials, account_size)
            if position <= 0.2 and _profit(row) < 0:
                phase = f"{phase} · drastic change" if phase else "Drastic change"
            record_scale(slot, position, move, phase)
        if compile_problem(row.get("error")):
            row["compile"] = True
            break
        if measured.get("script_error") and len(trials) == 1:
            # The script's own settings raise inside PineTS. Every later run raises the same way.
            row["unrunnable"] = str(row.get("error") or "The script stopped in PineTS.")
            break
        if row.get("fatal"):
            row["engine_fault"] = True
            break
        reason = _unrunnable_reason(row)
        if reason:
            row["unrunnable"] = reason
            break
        if row.get("stopped"):
            break
        from futuresfund.objective import counted_success, drawdown_share_text, stage_passed

        if stage_days:
            row["stage_days"] = int(stage_days)
            passed = stage_passed(row, stage_days, account_size) and not row.get("over_cap")
            row["stage_passed"] = passed
            # A pass does not end the stage. It keeps searching for more profit with less drawdown.
            first_pass = passed and best_pass is None
            better = passed and (best_pass is None or _pass_score(row) > _pass_score(best_pass))
            if better:
                best_pass = row
                since_best = 0
            elif best_pass is not None:
                since_best += 1
                if row.get("over_cap"):
                    carried = dict(best_pass.get("params") or {}) or None
            if board is not None and path is not None:
                verdict = "passed" if passed else "did not pass"
                from futuresfund.objective import stage_miss_lines

                # Which bar was missed, with this stage's pro-rated numbers.
                bar_text = "" if passed else " ".join(stage_miss_lines(row, stage_days))
                if first_pass:
                    after = (
                        f"The stage keeps searching for more profit with less drawdown. It stops after {STAGE_PATIENCE} "
                        f"attempts in a row without a better pass, or at {limit} attempts."
                    )
                elif better:
                    after = "This is the best pass so far: more profit after its drawdown than any pass before it."
                elif best_pass is not None:
                    after = (
                        f"The best pass is still attempt {_attempt_number(trials, best_pass)} ({_pass_text(best_pass)}). "
                        f"{since_best} of {STAGE_PATIENCE} attempts without a better pass."
                    )
                else:
                    after = ""
                head = (
                    f"{label} {verdict} the {stage_days}-day stage on attempt {len(trials)} of {limit}. Profit {row.get('net_profit')}, "
                    f"drawdown {row.get('max_drawdown')}, trades {row.get('trades')}."
                )
                board.post(
                    researcher,
                    " ".join(part for part in (head, bar_text, after) if part),
                    kind="log",
                    channel=researcher,
                )
            if row.get("blocked") or row.get("fatal"):
                break
            if best_pass is not None and since_best >= STAGE_PATIENCE:
                trials.patience_stop = True
                _post_patience(board, path, researcher, label, stage_days, trials, best_pass, limit)
                break
            continue
        if not row.get("over_cap") and counted_success(row, account_size):
            won = sum(1 for item in trials if not item.get("over_cap") and counted_success(item, account_size))
            if slot is not None:
                from futuresfund.backtrader_engine import record_success

                record_success(slot)
            if board is not None and path is not None:
                detail = f"Drawdown {row.get('max_drawdown')} is inside the pro account rule, {drawdown_share_text()}."
                board.post(
                    researcher,
                    f"Successful run {won} of {SUCCESS_COUNT} on {label}. Profit {row.get('net_profit')}. {detail}",
                    kind="log",
                    channel=researcher,
                )
            if won >= SUCCESS_COUNT and board is not None and path is not None:
                board.post(
                    researcher,
                    f"{path.name} has {SUCCESS_COUNT} successful runs. "
                    f"The search keeps going until {limit} attempts are finished.",
                    kind="log",
                    channel=researcher,
                )
        if accept is not None and developer not in _BACKTEST_DEVELOPERS and not row.get("over_cap") and accept(row):
            if board is not None and path is not None:
                board.post(
                    developer,
                    f"{path.name} met the target on this engine. Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, "
                    f"trades {row.get('trades')}. The search stops.",
                    kind="report",
                    channel=developer,
                )
            if board is not None and path is not None:
                _document(board, facts or {}, path, row, len(trials), developer, researcher, limit=limit)
            break
        if board is not None and path is not None:
            _document(board, facts or {}, path, row, len(trials), developer, researcher, limit=limit)
        if len(trials) == 1 and _opening_is_best(trials) and developer in _BACKTEST_DEVELOPERS and path is not None:
            _offer_library(board, facts or {}, path, row, developer, opening=True)
        if row.get("blocked") or row.get("fatal"):
            break
        # Only the chart's one-year search has the loss stop. It is marked by the caller, not by its limit,
        # so changing the year search attempts never turns this stop off.
        if developer in _BACKTEST_DEVELOPERS and year and len(trials) >= LOSS_LOOK and _best_profit(trials) <= 0:
            trials.loss_stop = True
            if board is not None and path is not None:
                board.post(
                    developer,
                    f"{label} is still a loss after {len(trials)} different results. "
                    "This chart is leaving the script and taking the next one.",
                    kind="log",
                    channel=developer,
                )
            break
        if (
            path is not None
            and _in_created(path)
            and developer not in _BACKTEST_DEVELOPERS
            and len(trials) >= CREATED_PROFIT_ATTEMPTS
            and _best_profit(trials) <= 0
        ):
            trials.scrapped = True
            reason = (
                f"{path.name} had no profit after {len(trials)} attempts. "
                "It was scrapped. The researcher who wrote it will create a new script."
            )
            if trials:
                trials[-1]["note"] = reason
            if _in_drafts(path):
                set_unproven(path, reason)
            else:
                set_aside(path, reason)
            if board is not None:
                board.post(developer, reason, kind="log", channel=developer)
            break
    trials.engine_runs = runs
    trials.attempt_budget = int(limit)
    if stage_days:
        trials.best_pass = best_pass
    return trials


def _with_owner_answers(base: dict, execute, path, facts, source: str, cut: dict, board, researcher: str):
    """Put the owner's values into this chart's first search. Returns (base, facts, source).

    A desk_ trade limit installs the desk trade-limit block in the copy this chart runs (and files). The
    script's own daily limits the owner set stay on at the later stages, like the trade cut's.
    """
    if path is None:
        return base, facts, source
    holder = getattr(execute, "source", None)
    if isinstance(holder, dict):
        if holder.get("owner_applied"):
            return base, facts, source
    elif getattr(execute, "_owner_applied", False):
        return base, facts, source
    try:
        from futuresfund.questions import owner_settings

        owner = owner_settings(path)
    except Exception:
        traceback.print_exc()
        owner = {}
    if not owner:
        return base, facts, source
    if isinstance(holder, dict):
        holder["owner_applied"] = True
    else:
        try:
            execute._owner_applied = True
        except (AttributeError, TypeError):
            pass
    desk = {name: value for name, value in owner.items() if str(name).startswith("desk_")}
    if desk and isinstance(holder, dict):
        from futuresfund.strategy_team import ensure_trade_limits, has_trade_limits

        text = str(holder.get("text") or "")
        if text and not has_trade_limits(text):
            installed = ensure_trade_limits(text)
            if installed != text:
                holder["previous"] = text
                holder["text"] = installed
                holder["stamp"] = int(holder.get("stamp") or 0) + 1
                facts = pine_facts(installed)
                if source:
                    source = installed
    from futuresfund.pine_report import text_inputs

    known = set(_pine_params(facts or {})) | set(base)
    # A session or plain text input the owner set is not searched, but the engine still gets its value.
    script_text = str(holder.get("text") or "") if isinstance(holder, dict) else (source or "")
    if not script_text:
        try:
            script_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            script_text = ""
    known |= {name for name, value in owner.items() if isinstance(value, str) and name in text_inputs(script_text)}
    used = {name: value for name, value in owner.items() if name in known or (name in desk and isinstance(holder, dict))}
    if not used:
        return base, facts, source
    from futuresfund.research_rules import is_daily_limit

    if desk or any(is_daily_limit(name) for name in used):
        # Like the trade cut: a limit turned on stays on at the next stage instead of being zeroed again.
        cut["touched"] = True
    if board is not None:
        shown = ", ".join(f"{name} = {value}" for name, value in used.items())
        board.post(researcher, f"{path.name} starts this chart with the owner's values: {shown}.", kind="log", channel=researcher)
    return {**base, **used}, facts, source


def _cut_state(execute) -> dict:
    """This chart's trade cut (trade_cut.new_state), kept on the executor so it carries from stage to stage."""
    from futuresfund.trade_cut import new_state

    holder = getattr(execute, "source", None)
    if isinstance(holder, dict):
        state = holder.get("trade_cut")
        if not isinstance(state, dict):
            state = new_state()
            holder["trade_cut"] = state
        return state
    state = getattr(execute, "_trade_cut", None)
    if not isinstance(state, dict):
        state = new_state()
        try:
            execute._trade_cut = state
        except (AttributeError, TypeError):
            pass
    return state


def _holder_text(execute) -> str | None:
    """The script copy this chart runs, or None for an executor with no copy (then only inputs can move)."""
    holder = getattr(execute, "source", None)
    if isinstance(holder, dict):
        return str(holder.get("text") or "")
    return None


def _next_cut(cut: dict, anchor: dict, script: str, bounds: dict, seen: set, seeds: dict, trials, holder_text: str | None = None):
    """The next trade-cut step that was not run yet: (settings, script text or None, what the researcher did)."""
    from futuresfund.objective import stage_bars
    from futuresfund.trade_cut import plan_step

    start = dict(cut.get("anchor") or anchor or {})
    try:
        weekly = float(stage_bars(None).get("weekly_profit") or 0)
    except Exception:
        weekly = 0.0
    text = holder_text if holder_text is not None else (script or "")
    last = trials[-1] if trials else None
    for _ in range(40):
        picked = plan_step(start, text, bounds, cut, seeds=seeds, row=last, weekly_profit=weekly, can_rewrite=holder_text is not None)
        if picked is None:
            return None
        plan, text_out, said = picked
        if _key(plan) in seen and text_out == text:
            # Already run with these settings. The cut moves on to its next step from here.
            start = plan
            continue
        cut["anchor"] = dict(plan)
        return plan, (text_out if holder_text is not None else None), said
    return None


def _track_trade_cut(cut: dict, measured: dict, plan: dict, stage_days, cut_attempt: bool, board, path, researcher: str, label: str, chart_key: str) -> bool:
    """Count one run toward this chart's trade cut. True when the run is over the trade cap."""
    from futuresfund.objective import TRADE_SAVE_MAX, over_trade_cap, trade_cap_text

    if measured.get("error") or measured.get("fatal") or measured.get("stopped"):
        return False
    over = over_trade_cap(measured, stage_days)
    count = int(measured.get("trades") or 0)
    if over:
        cut["anchor"] = dict(plan)
        cut["count"] = count
        if cut_attempt:
            cut["used"] = int(cut.get("used") or 0) + 1
        if count > TRADE_SAVE_MAX and path is not None and (_in_learning(path) or _in_created(path)):
            _mark_over_cap(path, count, chart_key)
        if not cut.get("active"):
            cut["active"] = True
            cut["used"] = 0
            _post_cut(
                board, path, researcher,
                f"{label[:1].upper()}{label[1:]} {trade_cap_text(measured, stage_days)}. I am cutting its trades on this chart, one step per attempt: "
                "its own daily limits on, a longer cooldown, the desk daily loss and profit limits, then most trades a day. "
                f"After {TRADE_CUT_ATTEMPTS} attempts still over the cap I ask the owner.",
            )
        elif cut_attempt:
            _post_cut(
                board, path, researcher,
                f"{label} still took {count:,} trades after that cut ({cut['used']} of {TRADE_CUT_ATTEMPTS} cut attempts).",
            )
        return True
    if cut.get("active") and cut_attempt:
        cut["active"] = False
        cut["used"] = 0
        cut["anchor"] = None
        steps = "; ".join(cut.get("steps") or []) or "cut its trades"
        _post_cut(
            board, path, researcher,
            f"{label} took {count:,} trades after I {steps}. That is under the trade cap, so the search goes on from these settings.",
            kind="report",
        )
    return False


def _over_after_pass(measured: dict, stage_days, board, path, researcher: str, label: str, trials, best_pass: dict) -> bool:
    """A run over the trade cap after the stage already passed. True when it is over the cap.

    The trade cut is not started: the stage keeps its best pass, the run counts toward the stage's
    patience, and the next attempt starts from the best pass's settings.
    """
    from futuresfund.objective import over_trade_cap, trade_cap_text

    if measured.get("error") or measured.get("fatal") or measured.get("stopped"):
        return False
    if not over_trade_cap(measured, stage_days):
        return False
    _post_cut(
        board, path, researcher,
        f"{label[:1].upper()}{label[1:]} {trade_cap_text(measured, stage_days)}. The stage already passed, so I am not cutting "
        f"its trades. It keeps its best pass, attempt {_attempt_number(trials, best_pass)}, and the next attempt starts from those settings.",
    )
    return True


def _post_cut(board, path, researcher: str, text: str, kind: str = "log") -> None:
    """The trade cut is the chart researcher's work, so its posts are the researcher's."""
    if board is None or path is None or not text:
        return
    board.post(researcher, text, kind=kind, channel=researcher)


def _over_trades_question(label: str, cut: dict, stage_days, source, spent_steps: bool = False) -> str:
    """The chart researcher's question for the owner when the trade cut did not get under the cap."""
    from futuresfund.objective import TRADE_SAVE_MAX, TRADES_YEAR_MAX

    chart = ""
    if isinstance(source, dict) and (source.get("root") or source.get("timeframe")):
        chart = f" on the {source.get('root') or ''} {source.get('timeframe') or ''}".rstrip() + " chart"
    window = f"the {stage_days}-day test" if stage_days else "the year"
    tried = "; ".join(cut.get("steps") or []) or "nothing, because the script has no input the desk could use to cut trades"
    why = "I ran out of cuts to try" if spent_steps else f"{TRADE_CUT_ATTEMPTS} cut attempts in a row were still over the cap"
    count = int(cut.get("count") or 0)
    return (
        f"{label[:1].upper()}{label[1:]} still takes too many trades{chart}: {count:,} in {window}. A run is filed only with at most "
        f"{TRADE_SAVE_MAX:,} trades and {TRADES_YEAR_MAX:,} a year. I tried: {tried}. {why}. "
        "What should the desk do? Reply with settings such as desk_max_trades_day = 2 or desk_daily_loss = 300, "
        "or park, or retry."
    )


def _start_cut_from_notes(board, path, execute, notes: dict, root: str, timeframe: str, researcher: str) -> None:
    """A chart that already measured a run over the trade cap starts this study in the trade cut."""
    from futuresfund.objective import chart_over_cap

    known = chart_over_cap(notes, root, timeframe)
    if not known:
        return
    cut = _cut_state(execute)
    cut["active"] = True
    cut["count"] = known
    _post_cut(
        board, path, researcher,
        f"{path.name} already took {known:,} trades on the {root} {timeframe} chart. I start this study by cutting its trades.",
    )


def _ask_over_trades(board, path: Path, researcher: str, question: str) -> str:
    """The trade cut did not get under the cap. The chart researcher asks the owner (questions kind over_trades).

    The researcher wrote the question. If the questions module fails, the script is set aside the old way
    with the question as its reason, so the chart does not take it again and again.
    """
    try:
        from futuresfund.questions import ask_owner
    except ImportError:
        ask_owner = None
    if ask_owner is not None:
        try:
            return ask_owner(board, path, researcher, question, "over_trades") or "awaiting"
        except Exception:
            traceback.print_exc()
    return leave_unrunnable(board, path, researcher, question) or "over_trades"


def _post_patience(board, path, researcher: str, label: str, stage_days: int, trials, best_pass: dict, limit: int) -> None:
    """The stage found no better pass in STAGE_PATIENCE runs in a row, so it ends with its best pass."""
    if board is None or path is None:
        return
    board.post(
        researcher,
        f"{label}: {STAGE_PATIENCE} attempts in a row found no better pass on the {stage_days}-day stage, so the stage ends "
        f"after {len(trials)} of its {limit} attempts. It keeps the best pass, attempt {_attempt_number(trials, best_pass)} "
        f"({_pass_text(best_pass)}).",
        kind="log",
        channel=researcher,
    )


def _journal_engine_error(developer: str, path, label: str, attempt: int, row: dict) -> None:
    """A backtest the engine could not finish goes in the error journal for the Systems Administrator's report."""
    try:
        from futuresfund.errors import record

        name = path.name if path is not None else "the script"
        record(
            developer or "Engine",
            f"{name} attempt {attempt} on {label}: {row.get('error') or 'the engine stopped without a result'}",
            source="engine",
        )
    except Exception:
        pass


def _best_profit(trials: list) -> float:
    profits = []
    for row in trials:
        if not isinstance(row, dict):
            continue
        try:
            profits.append(float(row.get("net_profit")))
        except (TypeError, ValueError):
            continue
    return max(profits) if profits else 0.0


def _clears_acceptance(row: dict) -> bool:
    """A one-year result that passes acceptance."""
    from futuresfund.library import library_ready, measured_on_one_year

    return bool(library_ready(row) and measured_on_one_year(row))


def _no_trades(measured: dict) -> bool:
    """A finished run that never entered. An error is a different problem."""
    if measured.get("error") or measured.get("fatal") or measured.get("blocked") or measured.get("stopped"):
        return False
    try:
        return int(measured.get("trades") or 0) == 0
    except (TypeError, ValueError):
        return False


def _zero_result(measured: dict) -> bool:
    """A finished run with no profit and no trades."""
    if not _no_trades(measured):
        return False
    try:
        return float(measured.get("net_profit")) == 0
    except (TypeError, ValueError):
        return False


def _traded(row) -> bool:
    """A finished run that entered at least once. An error row is not a trading run."""
    if not isinstance(row, dict) or row.get("error") or row.get("fatal") or row.get("blocked"):
        return False
    try:
        return int(row.get("trades") or 0) > 0
    except (TypeError, ValueError):
        return False


def _best_trading_row(trials) -> dict | None:
    """The highest-ranked run of this search that took trades, or None when none did."""
    traded = [row for row in trials or [] if _traded(row)]
    return max(traded, key=_rank) if traded else None


def _past_dead_edge(edges: dict, name: str, start, value) -> bool:
    """True when value for name is as far as, or past, a move that stopped every entry.

    start is where the move begins. None means only the value is checked.
    """
    for origin, dead in edges.get(name, ()):
        if any(isinstance(item, bool) for item in (origin, dead, start, value)):
            continue
        try:
            origin, dead, value = float(origin), float(dead), float(value)
            begin = origin if start is None else float(start)
        except (TypeError, ValueError):
            continue
        if dead > origin and begin < dead and value >= dead:
            return True
        if dead < origin and begin > dead and value <= dead:
            return True
    return False


def _record_dead_edges(edges: dict, plan: dict, trials) -> None:
    """A run that took no trades, one input away from a run that traded: that move stopped every entry."""
    for row in trials or []:
        if not _traded(row):
            continue
        params = row.get("params") or {}
        changed = _changed_keys(params, plan)
        if len(changed) == 1 and (params.get(changed[0]), plan.get(changed[0])) not in edges.get(changed[0], []):
            edges.setdefault(changed[0], []).append((params.get(changed[0]), plan.get(changed[0])))


def _past_any_dead_edge(edges: dict, plan: dict, trials) -> bool:
    """True when some input in this plan is as far as, or past, a move that stopped every entry.

    A value a trading run already used is never blocked, so the search can always return to it.
    """
    if not edges:
        return False
    for name, value in plan.items():
        if name not in edges:
            continue
        if any(_traded(row) and (row.get("params") or {}).get(name) == value for row in trials or []):
            continue
        if _past_dead_edge({name: edges[name]}, name, None, value):
            return True
    return False


def _flat_key(path: Path, chart: str = "") -> str:
    try:
        base = str(path.resolve())
    except OSError:
        base = str(path)
    return f"{base}|{chart}" if chart else base


def _zero_streak(path: Path | None, measured: dict, chart: str = "") -> int:
    """How many finished runs of this script on this chart in a row made no profit and took no trades.

    A trade or a profit clears the count. Each chart and instrument keeps its own count, so one
    chart's settings that never enter do not count against another chart.
    """
    if path is None:
        return 0
    key = _flat_key(path, chart)
    with _flat_lock:
        if not _zero_result(measured):
            _flat_runs.pop(key, None)
            return 0
        count = _flat_runs.get(key, 0) + 1
        _flat_runs[key] = count
        return count


def _park_flat(board, path: Path | None, developer: str, trials, label: str, chart: str = "", researcher: str = "") -> None:
    """Stop a script that cannot enter. Its researcher asks the owner, and it waits in awaitingAnswerStrategies.

    An engine 4 draft is not asked about: it goes to cannotRunStrategies the old way, and the team writes another.
    trials.flat_outcome is the study outcome: "awaiting", or "cannot_run" for a parked draft.
    """
    reason = f"{label} took no trades and made no profit {FLAT_RUNS} times in a row."
    trials.flat_stop = True
    trials.flat_outcome = "cannot_run"
    if trials:
        trials[-1]["note"] = reason
        trials[-1]["skipped"] = True
    key = _flat_key(path, chart) if path is not None else ""
    if path is not None and (_in_learning(path) or _in_created(path)):
        if _in_drafts(path):
            leave_unrunnable(board, path, developer, reason)
        else:
            asked = _ask_owner(board, path, researcher or developer, f"{reason} {_flat_reason(path)}", "no_trades", developer)
            trials.flat_outcome = asked or "cannot_run"
    if key:
        with _flat_lock:
            _flat_runs.pop(key, None)


def _flat_reason(path) -> str:
    """Why the developer is leaving a script that cannot enter, read from the script itself."""
    text = ""
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    reasons = [
        "The conditions that allow an entry were changed and the script still took no trades.",
        "Profit, drawdown, and the trade count stayed at zero, so stepping the other inputs would repeat that result.",
        "This chart is leaving the script for a rewrite.",
    ]
    if "0930-1600" in text or "in_entry_window" in text:
        reasons.append("It only enters inside the session window written in the script.")
    if "recent_bull_fvg" in text or "recent_bear_fvg" in text:
        reasons.append("A fair value gap in the breakout direction is required, and this chart did not print one the rule accepts.")
    if 'time("D")' in text or "time('D')" in text:
        reasons.append("The opening range resets on the session day, from time(\"D\"), and still never broke out.")
    if not text:
        reasons.append("Nothing in the settings changed the trade count from zero.")
    reasons.append("The script stays on file and the other charts are still tested.")
    return " ".join(reasons)


def _row_profitable(row: dict) -> bool:
    """A result with a profit above zero and at least one trade."""
    if row.get("error") or row.get("fatal") or row.get("blocked"):
        return False
    try:
        profit = float(row.get("net_profit"))
        trades = int(row.get("trades") or 0)
    except (TypeError, ValueError):
        return False
    return profit > 0 and trades >= 1


def adjustment_notes(trials: list[dict]) -> tuple[str, str]:
    """A table the portfolio manager can put in the report without another pass."""
    from futuresfund.objective import acceptance_line

    if not trials:
        return "The engine recorded no attempts.", "The engine recorded no attempts."
    origin = trials[0]
    by_objective = sorted(trials, key=_rank, reverse=True)
    by_profit = sorted(trials, key=lambda row: float(row.get("net_profit") or -10**12), reverse=True)
    made = [row for row in trials if float(row.get("net_profit") or 0) > 0]
    lost = [row for row in trials if float(row.get("net_profit") or 0) < 0]
    winner = max(made, key=lambda row: float(row["net_profit"])) if made else None
    loser = min(lost, key=lambda row: float(row["net_profit"])) if lost else None
    lines = [
        "Attempt results. Each row builds on the prior result, not a fresh copy of the original script. "
        "Rows are in search order: 130 to 600 trades a year first (an aim of the search, not a pass rule), then the drawdown rule, then the win rate, the profit factor, and the profit. "
        "Acceptance needs the library trade count, pro-rated to each test, and no more than 600 trades a year.",
        "",
        f"{'Change':<44} {'Profit':>14} {'Drawdown':>14} {'Trades':>8} {'Trail':>7}",
        f"{'-' * 44} {'-' * 14} {'-' * 14} {'-' * 8} {'-' * 7}",
        _note_row("Original", origin),
    ]
    for row in by_objective[:5]:
        if row is origin:
            continue
        lines.append(_note_row(_change_label(origin, row), row))
    lines.append("")
    lines.append("Best change under the objective (130 to 600 trades a year, then the lowest drawdown, then the larger profit)")
    lines.append(_note_row(_change_label(origin, by_objective[0]), by_objective[0]))
    lines.append("")
    lines.append("Most profitable change")
    lines.append(_note_row(_change_label(origin, by_profit[0]), by_profit[0]))
    lines.append("")
    lines.append("A change that made money" if winner else "No change made money")
    if winner:
        lines.append(_note_row(_change_label(origin, winner), winner))
    lines.append("")
    lines.append("A change that lost money" if loser else "No change lost money")
    if loser:
        lines.append(_note_row(_change_label(origin, loser), loser))
    quant = "\n".join(lines)
    best_params = by_objective[0].get("params") or {}
    added = best_params.get("added_indicator")
    extra = f" The best version adds a {added} filter on new entries." if added else ""
    indicator = (
        "The next setting follows the last result. A change that raises profit, without a higher drawdown, is increased again until the drawdown rises or the profit falls by 5 percent. "
        "The stop and the take profit are inputs and are moved along with every other input. One input changes per run, and that change is the label on the result. "
        f"{acceptance_line()} Two or three trades is not a strategy. "
        "Among the stored runs, the lower drawdown still ranks first and the larger profit after that. "
        f"The best version uses {_short_params(best_params)}.{extra} "
        f"Attempts recorded: {len(trials)}."
    )
    return quant, indicator


def _note_row(label: str, row: dict) -> str:
    trail = "yes" if row.get("passed") else "no"
    return (
        f"{label:<44} {_money(row.get('net_profit')):>14} {_money(row.get('max_drawdown')):>14} "
        f"{str(row.get('trades')):>8} {trail:>7}"
    )


def _change_label(origin: dict, row: dict) -> str:
    keys = [key for key in origin.get("params") or {} if row.get("params", {}).get(key) != origin["params"].get(key)]
    if not keys:
        return "Original"
    if len(keys) == 1:
        key = keys[0]
        return f"{key} {origin['params'].get(key)} to {row['params'].get(key)}"
    return "several settings"


def _money(value) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def research_guidelines() -> str:
    """The one research list. Limit the loss first, then raise the profit."""
    from futuresfund.indicator_guidance import price_guidance

    return price_guidance()


def notes_digest(limit: int = 20) -> str:
    """Prior lessons, plus any line appended after the last summary."""
    path = NOTES / "LESSONS.md"
    if not path.is_file():
        return "No learned strategy notes yet."
    summary = NOTES / "LESSONS_SUMMARY.md"
    offset = _summary_offset(summary)
    if summary.is_file() and offset is not None and path.stat().st_size > _lesson_heavy_bytes():
        body = _summary_body(summary)
        extra = _lessons_after(path, offset)
        combined = body if not extra else f"{body}\n{extra}" if body else extra
        lines = [line for line in combined.splitlines() if line.strip()]
        return "\n".join(lines[-limit:])
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def append_window_lesson(result: dict | None) -> str | None:
    """Append one new measured lesson. The same result, and a result with no measurement, add nothing.

    The research note JSON and the research guide are not rewritten.
    """
    line = _lesson_line(result)
    if line is None or not isinstance(result, dict):
        return None
    path = NOTES / "LESSONS.md"
    with _notes_lock:
        NOTES.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if _lesson_already_recorded(existing, result):
            return None
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return line


def maintain_lessons() -> bool:
    """Shrink the lessons when they are too large for the fast research window.

    Each script keeps its best measured lesson, including the changes that beat the original.
    Repeated attempt tables are removed. The portfolio manager emails that the file was reduced.
    """
    path = NOTES / "LESSONS.md"
    summary = NOTES / "LESSONS_SUMMARY.md"
    with _notes_lock:
        if not path.is_file() or path.stat().st_size <= _lesson_heavy_bytes():
            return False
        size = path.stat().st_size
        original = path.read_text(encoding="utf-8", errors="replace")
        shrunk = _shrink_lessons(original)
        shrunk_size = len(shrunk.encode("utf-8"))
        if shrunk_size >= size:
            offset = _summary_offset(summary)
            if offset is not None and size <= offset + _lesson_heavy_bytes():
                return False
            _write_lesson_summary(path, summary, size)
        else:
            path.write_text(shrunk, encoding="utf-8")
            summary.write_text(f"# summarized through byte {shrunk_size}\n{shrunk}", encoding="utf-8")
    _email_lesson_size(size)
    return True


def _lesson_heavy_bytes() -> int:
    from futuresfund.llm import CONTEXT_CAP

    return CONTEXT_CAP * 4


def _lesson_line(result: dict | None) -> str | None:
    if not isinstance(result, dict):
        return None
    script = str(result.get("script") or result.get("title") or "").strip()
    change = str(result.get("change") or "").strip()
    profit = result.get("net_profit", result.get("profit"))
    drawdown = result.get("max_drawdown", result.get("drawdown"))
    trades = result.get("trades")
    chart = _lesson_chart(result)
    if not script or not change or not chart:
        return None
    if profit is None or drawdown is None or trades is None:
        return None
    try:
        profit_text = _plain_number(profit)
        drawdown_text = _plain_number(drawdown)
        trade_count = int(float(trades))
    except (TypeError, ValueError):
        return None
    nxt = str(result.get("next") or result.get("impact") or "").replace("\n", " ").strip().rstrip(".")
    if not nxt:
        nxt = "Try one different input on this chart."
    if len(nxt) > 180:
        nxt = nxt[:179].rstrip() + "…"
    return (
        f"{script} on the {chart}: {change}. "
        f"Profit {profit_text}. Drawdown {drawdown_text}. Trades {trade_count}. "
        f"Next: {nxt}."
    )


def _lesson_chart(result: dict) -> str:
    named = str(result.get("chart") or "").strip()
    if named:
        return named
    timeframe = str(result.get("timeframe") or "").strip()
    instrument = str(result.get("instrument") or result.get("contract") or "").strip()
    names = {"1m": "1-minute", "2m": "2-minute", "5m": "5-minute", "15m": "15-minute"}
    frame = names.get(timeframe, timeframe)
    return f"{frame} {instrument}".strip()


def _plain_number(value) -> str:
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _number_forms(value) -> set[str]:
    number = float(value)
    forms = {str(value).strip(), _plain_number(value), f"{number:.2f}", f"{number:g}"}
    if number == int(number):
        forms.add(str(int(number)))
    return {item for item in forms if item}


def _lesson_already_recorded(text: str, result: dict) -> bool:
    script = str(result.get("script") or result.get("title") or "").strip()
    change = str(result.get("change") or "").strip()
    profit = result.get("net_profit", result.get("profit"))
    drawdown = result.get("max_drawdown", result.get("drawdown"))
    trades = result.get("trades")
    try:
        trade_forms = _number_forms(int(float(trades)))
        profit_forms = _number_forms(profit)
        drawdown_forms = _number_forms(drawdown)
    except (TypeError, ValueError):
        return False
    lines = text.splitlines()
    for start in range(len(lines)):
        window = "\n".join(lines[start:start + 6])
        if script not in window or change not in window:
            continue
        if not any(_has_number(window, form) for form in profit_forms):
            continue
        if not any(_has_number(window, form) for form in drawdown_forms):
            continue
        if any(_has_number(window, form) for form in trade_forms):
            return True
    return False


def _has_number(text: str, form: str) -> bool:
    """A measured number, including when a period ends the sentence."""
    return re.search(rf"(?<![\d.]){re.escape(form)}(?!\d)(?!\.\d)", text) is not None


def _summary_offset(path: Path) -> int | None:
    if not path.is_file():
        return None
    first = path.read_text(encoding="utf-8").splitlines()[:1]
    if not first:
        return None
    match = re.search(r"through byte (\d+)", first[0])
    if match is None:
        return None
    return int(match.group(1))


def _summary_body(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines[1:] if line.strip() and not line.startswith("# summarized")]
    return "\n".join(kept)


def _lessons_after(path: Path, offset: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(min(max(offset, 0), size))
        return handle.read().decode("utf-8", errors="replace").strip()


def _shrink_lessons(text: str) -> str:
    """One short lesson per script: the highest measured profit, and the change that beat the original."""
    lines = text.replace("\x00", "").splitlines()
    chosen: dict[str, tuple[float, str]] = {}
    for index, line in enumerate(lines):
        found = _measured_lesson(line)
        if found is None:
            continue
        key, profit = found
        short = _brief_lesson(line, _change_after(lines, index))
        prior = chosen.get(key)
        if prior is None or profit >= prior[0]:
            chosen[key] = (profit, short)
    if not chosen:
        kept = [line.strip() for line in lines if line.strip() and "\x00" not in line][-8:]
        return ("\n".join(kept) + "\n") if kept else ""
    ordered = sorted(chosen.items(), key=lambda item: (item[1][0], item[0].lower()))
    return "\n".join(line for _key, (_profit, line) in ordered) + "\n"


def _measured_lesson(line: str) -> tuple[str, float] | None:
    stripped = line.replace("\x00", " ").strip()
    if not stripped:
        return None
    best = re.match(r"^(.*?):\s*attempts\s+\d+,\s*best profit\s+(-?(?:\d+(?:\.\d+)?))", stripped, re.IGNORECASE)
    if best:
        return best.group(1).strip(), float(best.group(2))
    one = re.match(r"^(.*?) on the ([^:]+):.+?\bProfit\s+(-?(?:\d+(?:\.\d+)?))", stripped)
    if one and "drawdown" in stripped.lower() and "trades" in stripped.lower():
        return f"{one.group(1).strip()} on the {one.group(2).strip()}", float(one.group(3))
    return None


_CHANGE_ROW = re.compile(
    r"^(?P<label>.+?)\s+(?P<money>-?\$[\d,]+\.\d+)\s+-?\$[\d,]+\.\d+\s+-?\d+\s+(?:yes|no)\s*$"
)


def _change_after(lines: list[str], start: int) -> str:
    """The named change under a lesson, when the summary line itself does not carry it."""
    want = False
    for line in lines[start + 1:start + 80]:
        stripped = line.replace("\x00", "").strip()
        if not stripped:
            continue
        if _measured_lesson(stripped):
            break
        if stripped.lower().startswith("single changes that beat"):
            tail = stripped.split(":", 1)[-1].strip().rstrip(".")
            if tail and tail.lower() != "original":
                return tail
        if stripped.lower().startswith("best change under the objective"):
            want = True
            continue
        if not want:
            continue
        match = _CHANGE_ROW.match(stripped)
        if match is None:
            continue
        label = match.group("label").strip()
        if label and label != "Original":
            return label
        want = False
    return ""


def _brief_lesson(line: str, change: str) -> str:
    """Profit, drawdown, trades, and the change that beat the original. The attempt table stays out."""
    stripped = " ".join(line.replace("\x00", " ").split())
    cut = stripped.find(" Attempt results.")
    if cut != -1:
        stripped = stripped[:cut].rstrip(" .")
    if change and change.lower() not in stripped.lower():
        stripped = f"{stripped}. The change that beat the original: {change}."
    return stripped


def _beating_change(trials: list) -> str:
    """The single change whose profit beat the original. A table is not included."""
    if not trials:
        return ""
    origin = trials[0]
    try:
        origin_profit = float(origin.get("net_profit") or 0)
    except (TypeError, ValueError):
        origin_profit = 0.0
    winners = []
    for row in trials[1:]:
        try:
            profit = float(row.get("net_profit") or 0)
        except (TypeError, ValueError):
            continue
        if profit > origin_profit:
            winners.append(row)
    if not winners:
        return ""
    best = max(winners, key=lambda row: float(row.get("net_profit") or 0))
    label = _change_label(origin, best)
    if not label or label == "Original":
        return ""
    return label


def _saved_lesson(title: str, trials: list, best: dict) -> str:
    lesson = (
        f"{title}: attempts {len(trials)}, "
        f"best profit {best.get('net_profit')}, drawdown {best.get('max_drawdown')}, "
        f"trades {best.get('trades')}."
    )
    change = _beating_change(trials)
    if change:
        lesson += f" Single changes that beat the original on profit and drawdown: {change}."
    return lesson


def _write_lesson_summary(path: Path, summary: Path, size: int) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    measured = [line.strip() for line in text.splitlines() if _line_has_measurement(line)]
    chosen: list[str] = []
    total = 0
    for line in reversed(measured):
        if chosen and total + len(line) + 1 > 1600:
            break
        chosen.append(line)
        total += len(line) + 1
    chosen.reverse()
    if not chosen:
        chosen = [line.strip() for line in text.splitlines() if line.strip()][-8:]
    body = "\n".join(chosen)
    summary.write_text(f"# summarized through byte {size}\n{body}\n", encoding="utf-8")


def _line_has_measurement(line: str) -> bool:
    lowered = line.lower()
    return "profit" in lowered and "trade" in lowered


def _email_lesson_size(size: int) -> None:
    from futuresfund.llm import CONTEXT_CAP
    from futuresfund.mailer import send_report

    tokens = (size + 3) // 4
    send_report(
        "\n".join([
            "Portfolio Manager",
            "",
            "The size of the lessons learned is impacting performance.",
            f"LESSONS.md is {tokens} tokens. The research window that still runs at full speed is {CONTEXT_CAP} tokens, "
            "and a prompt leaves room for the reply at about 20,000 tokens.",
            "The notes were summarized down to the best measured lesson for each script.",
            "The lesson keeps the profit, the drawdown, the trades, and the changes that beat the original.",
            "Repeated attempt tables were removed from LESSONS.md.",
        ]),
        subject="Lessons learned are too large",
    )


EMPTY_CHARTS = 3         # charts with no trades before the researcher is asked why the script never enters
REPAIR_TRIES = 3         # times the researcher is asked to repair a stop, take profit, or input that does not work
REOPEN_LIMIT = 3         # times a finished script's charts run again for a researcher's answer
# Research changes input values only: no researcher rewrite, no added indicator, no redesign, and no
# added skip window. The script's logic stays as written. A repair that lets a script compile still runs.
VALUES_ONLY = True
VALUES_ONLY_LINE = (
    "Only a SETTING line or KEEP is used for this script. Its logic stays as written, so a REWRITE, an INDICATOR, "
    "or a REDESIGN is not used. Change the value of one existing input."
)
# Different results each proving stage gets on one chart. The year gets ATTEMPT_LIMIT. Read through
# stage_attempts(days) when a stage starts, so a value the owner saved on the Design page applies.
STAGE_7_ATTEMPTS = 100
STAGE_30_ATTEMPTS = 150
STAGE_90_ATTEMPTS = 200
# A stage keeps searching after its first pass for more profit with less drawdown. It stops when this many
# runs in a row, repeats included, found no better pass, or when its attempts are spent.
STAGE_PATIENCE = 25
# A run over the trade cap (objective.over_trade_cap) starts the trade cut on that chart. After this many cut
# attempts in a row still over the cap, the chart researcher asks the owner (kind over_trades).
TRADE_CUT_ATTEMPTS = 12
STAGE_REVIEWS = 6       # failed charts the researcher reviews on one script
REVIEW_BACKLOG = 12      # failed-stage reviews waiting on the one model before another is left out
# Switches the owner can change on the Research workflow Design page (futuresfund/workflow_design.py).
# The values here are the defaults. Saved changes are applied when research starts, never at import.
USE_ALERT_BARS = True         # a study adds the TradingView alert candles after the last Databento download
READ_BEFORE_TESTING = True    # measure how the script responds and let the researcher read it before the stages
ALLOW_REPAIR_REWRITES = True  # a researcher's repair answer may rewrite the script, even with VALUES_ONLY on
STAGES_OFF: frozenset = frozenset()  # proving stages, by days, that are skipped: no search and no ledger mark
STAGE_REVIEW_ON = True        # a failed stage, or a failed year, goes to the researcher for a review
REOPEN_FOR_ANSWERS = True     # a researcher's answer runs the script's finished charts again
RECHECK_BEFORE_FILING = False  # a year run is filed only when the same settings also pass the last 7, 30, and 90 days


def _mark_stage(path, root, timeframe, days, status, row=None, source="", fresh=False, resumed=False):
    """Write the stage to the proving ledger. A ledger that cannot be written never stops research.

    fresh clears the passes of an earlier study. The 7-day 'testing' mark always does; fresh does it
    for the first stage that runs when the owner turned the earlier stages off. resumed marks a
    stopped stage starting again where it stopped, which the ledger counts in its resumes.
    """
    if path is None or not root:
        return
    from futuresfund.proving import mark

    flags = {}
    if fresh:
        flags["fresh"] = True
    if resumed:
        flags["resumed"] = True
    try:
        mark(path.name, root, timeframe, days, status, row, source, **flags)
    except (OSError, ValueError, TypeError):
        pass


def stage_attempts(days: int | None) -> int:
    """The different results a proving stage gets on one chart: 7, 30 and 90 days each have their own, the year has ATTEMPT_LIMIT.

    The module values are read here, when a stage starts, so a value the owner saved on the Design page applies.
    Another day count gets the budget of the shortest stage at least that long.
    """
    if days is None:
        return int(ATTEMPT_LIMIT)
    budgets = ((7, STAGE_7_ATTEMPTS), (30, STAGE_30_ATTEMPTS), (90, STAGE_90_ATTEMPTS))
    for stage, budget in budgets:
        if int(days) <= stage:
            return int(budget)
    return int(ATTEMPT_LIMIT)


def _pass_score(row: dict) -> tuple:
    """How good a passing stage run is: the profit minus the largest drawdown, then the lower drawdown."""
    try:
        profit = float(row.get("net_profit") or 0)
    except (TypeError, ValueError):
        profit = 0.0
    try:
        drawdown = abs(float(row.get("max_drawdown") or 0))
    except (TypeError, ValueError):
        drawdown = 0.0
    return (round(profit - drawdown, 6), -drawdown)


def _best_pass(trials) -> dict | None:
    """The passing stage run with the most profit and the least drawdown. A tie keeps the earlier run."""
    best = None
    for row in trials or ():
        if isinstance(row, dict) and row.get("stage_passed") and (best is None or _pass_score(row) > _pass_score(best)):
            best = row
    return best


def _pass_text(row: dict) -> str:
    return f"profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}"


def _response_names(params: dict, facts: dict) -> list[str]:
    """The inputs worth moving, exits first. The session stays where it is, so its inputs go last."""
    choices = _input_choices(facts)
    names = [key for key, value in params.items() if _searchable(key, value, choices)]

    def order(name: str) -> int:
        if _is_exit_name(name):
            return 0
        return 2 if name.startswith(("sess_", "skip_")) or name in ("use_session", "use_skip") else 1

    return sorted(names, key=order)


def rewrite_holds(before: dict, after: dict, repaired: list[str], exits_fixed: bool = False) -> bool:
    """A rewrite stays when it connected an input without costing profit, or when the result is better and earns no less.

    A rewrite that makes a broken stop or take profit work stays while the script still trades. The old
    profit came from exits that were not working, so it is not the number to protect.
    Rank alone puts any run with trades above a run with none, so a script that starts trading at a large loss would pass on it.
    """
    if after.get("error") or after.get("fatal") or after.get("stopped"):
        return False
    if int(before.get("trades") or 0) and not int(after.get("trades") or 0):
        return False
    if exits_fixed:
        return True
    profit = float(before.get("net_profit") or 0)
    if repaired and float(after.get("net_profit") or 0) >= profit - max(100.0, abs(profit) * 0.1):
        return True
    return _rank(after) > _rank(before) and float(after.get("net_profit") or 0) >= profit


def redesign_holds(before: dict, after: dict) -> bool:
    """A redesign stays only when it ranks above the script it replaced and makes at least as much profit.

    Anything less is a worse script, so the one before it goes back.
    """
    if after.get("error") or after.get("fatal") or after.get("stopped"):
        return False
    if not int(after.get("trades") or 0):
        return False
    return _rank(after) > _rank(before) and float(after.get("net_profit") or 0) >= float(before.get("net_profit") or 0)


def _install_checked(board, path: Path, revised: str, execute, before: dict, researcher: str, slot: int | None, because: str, redesign: bool = False) -> str | None:
    """Write the researcher's rewrite, measure it at the same settings, and take it back unless it holds.

    A redesign stays only when it does better than the script it replaced (redesign_holds).
    """
    from futuresfund.attempt_reason import install_script, script_refusal, undo_rewrite
    from futuresfund.backtrader_engine import record_research, set_engine_thinking
    from futuresfund import script_diagnosis as diagnosis

    holder = execute.source
    record_research(slot, "rewrites_tried")
    installed = install_script(path, revised, execute)
    if not installed:
        from futuresfund.strategy_team import engine_problem

        reason = script_refusal(revised, str(holder.get("text") or "")) or engine_problem(revised)
        board.post(researcher, f"{path.name}: the researcher's rewrite did not pass the script checks, so it was not used. {reason}".strip(), kind="report", channel=researcher)
        return None
    old = holder.get("response") or {}
    new_facts = pine_facts(installed)
    settings = {**_pine_params(new_facts), **dict(before.get("params") or {})}
    try:
        set_engine_thinking(slot, "Measuring the researcher's rewrite")
        after = execute(settings) or {}
        names = [name for name in _response_names(settings, new_facts) if name in diagnosis.ignored_inputs(old)]
        response = diagnosis.measure_response(installed, settings, execute, names, _input_bounds(new_facts), _input_choices(new_facts))
        repaired = list(response.get("moved") or {})
        was_broken = diagnosis.exit_faults(str(holder.get("previous") or ""), old) if holder.get("previous") else []
        merged_flat = {**response, "baseline": response.get("baseline") or old.get("baseline")}
        holds = redesign_holds(before, after) if redesign else rewrite_holds(before, after, repaired, bool(was_broken) and len(diagnosis.exit_faults(installed, merged_flat)) < len(was_broken))
        if holds:
            record_research(slot, "rewrites_kept")
            holder.pop("previous", None)
            merged = {
                **response,
                "moved": {**(old.get("moved") or {}), **(response.get("moved") or {})},
                "read": True,
            }
            if not response.get("partial"):
                diagnosis.save(installed, merged)
            holder["response"] = merged
            holder["flat"] = diagnosis.ignored_inputs(merged)
            fixed = f" {', '.join(repaired)} now change the result." if repaired else ""
            kind_done = "redesigned" if redesign else "rewritten"
            board.post(
                researcher,
                f"{path.name} was {kind_done} and kept. {because}{fixed} Profit {before.get('net_profit')} became {after.get('net_profit')}, drawdown {before.get('max_drawdown')} became {after.get('max_drawdown')}.",
                kind="report",
                channel=researcher,
            )
            return installed
    except Exception:
        traceback.print_exc()
        after = {}
    finally:
        set_engine_thinking(slot, "")
    undo_rewrite(path, execute)
    board.post(
        researcher,
        f"{path.name}: the {'redesign did worse, so it' if redesign else 'rewrite did not hold, so it'} was taken back and the script before it is used again. {because} Profit {before.get('net_profit')} became {after.get('net_profit')}, drawdown {before.get('max_drawdown')} became {after.get('max_drawdown')}.",
        kind="log",
        channel=researcher,
    )
    return None


def _read_before_testing(board, path: Path, execute, facts: dict, base: dict, researcher: str, slot: int | None) -> tuple[dict, dict]:
    """Test how the script responds, then let the researcher read it. Each script is read once."""
    holder = getattr(execute, "source", None)
    if board is None or path is None or not isinstance(holder, dict):
        return base, facts
    from futuresfund.backtrader_engine import set_engine_thinking
    from futuresfund import script_diagnosis as diagnosis

    script = str(holder.get("text") or "")
    lock = diagnosis.turn(script)
    if not lock.acquire(blocking=False):
        # Another chart is testing this script. This chart backtests meanwhile, and leaves out what the script shows it ignores.
        known = diagnosis.load(script)
        holder["response"] = known
        holder["flat"] = diagnosis.ignored_inputs(known) or set(diagnosis.unread_inputs(script))
        return base, facts
    try:
        record = diagnosis.load(script)
        if record.get("answer"):
            return _use_answer(board, path, execute, facts, base, researcher, slot, script, record)
        chart = f"{holder.get('root') or ''} {holder.get('timeframe') or ''}".strip()
        empty = list(record.get("empty_charts") or [])
        # A chart with no trades cannot show what an input or a repair does, so it never settles a script.
        if not (record.get("baseline") or {}).get("trades") and chart not in empty:
            set_engine_thinking(slot, "Testing how the script responds to each input")
            try:
                measured = diagnosis.measure_response(script, base, execute, _response_names(base, facts), _input_bounds(facts), _input_choices(facts))
            finally:
                set_engine_thinking(slot, "")
            if measured.get("partial"):
                return base, facts
            if measured["baseline"].get("trades"):
                # This is the first chart that can show the researcher anything. A read of a chart with no trades does not count.
                record = {**measured, "reviewed": record.get("reviewed") or [], "empty_charts": empty}
            else:
                empty.append(chart)
                record = {**record, **{key: value for key, value in measured.items() if key not in record}, "empty_charts": empty}
            diagnosis.save(script, record)
        holder["response"] = record
        holder["flat"] = diagnosis.ignored_inputs(record)
        if holder["flat"]:
            board.post(researcher, f"{path.name}: these inputs do not change a trade, so the search leaves them alone: {', '.join(sorted(holder['flat']))}.", kind="report", channel=researcher)
        if "baseline" not in record:
            return base, facts
        traded = bool(record["baseline"].get("trades"))
        broken_exits = diagnosis.exit_faults(script, record) if traded else []
        faults = broken_exits + (diagnosis.input_faults(script) if traded else [])
        if record.get("read"):
            if faults and int(record.get("repairs") or 0) < REPAIR_TRIES:
                why = "This script does not work as written. Repair it so each of these works: " + " ".join(faults)
                ask_researcher(board, path, script, [record["baseline"]], facts, why, record, researcher, "repair", urgent=bool(broken_exits))
            return base, facts
        if not traded and len(empty) < EMPTY_CHARTS:
            board.post(researcher, f"{path.name} took no trades on the {chart} chart in the last month, so the researcher waits for a chart that trades before reading it.", kind="log", channel=researcher)
            return base, facts
        why = "Read this script before its first backtest. Say what is broken, if anything, and repair it."
        if faults:
            why += " These do not work as written: " + " ".join(faults)
        if not traded:
            why = f"This script took no trades in the last month on {len(empty)} charts: {', '.join(empty)}. Find out why the entry never passes, and repair it."
        ask_researcher(board, path, script, [record["baseline"]], facts, why, record, researcher, "read", urgent=bool(broken_exits))
        return base, facts
    finally:
        lock.release()


def _use_answer(board, path: Path, execute, facts: dict, base: dict, researcher: str, slot: int | None, script: str, record: dict) -> tuple[dict, dict]:
    """Use what the researcher answered while the engines were backtesting. The answer is used once."""
    from futuresfund.backtrader_engine import record_research
    from futuresfund import script_diagnosis as diagnosis

    holder = execute.source
    answer = record.pop("answer")
    diagnosis.save(script, record)
    holder["response"] = record
    holder["flat"] = diagnosis.ignored_inputs(record)
    because = str(answer.get("because") or "")
    if answer.get("script"):
        settings = dict(answer.get("settings") or base)
        before = {**(record.get("baseline") or {}), "params": settings}
        window = holder.get("sessions")
        holder["sessions"] = diagnosis.RESPONSE_SESSIONS
        redesign = bool(answer.get("redesign"))
        try:
            if redesign:
                # A redesign has to beat this script at the same settings, on the same window, so measure this script there first.
                measured = execute(settings) or {}
                if not any(measured.get(name) for name in ("error", "fatal", "stopped")):
                    before = {**measured, "params": settings}
            installed = _install_checked(board, path, answer["script"], execute, before, researcher, slot, because, redesign=redesign)
        finally:
            holder["sessions"] = window
        if installed:
            facts = pine_facts(installed)
            base = {**_with_contract_point(_pine_params(facts), str(holder.get("root") or "ES")), **{key: value for key, value in base.items() if key in _pine_params(facts)}}
    if answer.get("params"):
        record_research(slot, "model_choices")
        base = {**base, **{key: value for key, value in answer["params"].items() if key in base}}
        board.post(researcher, f"{path.name} starts from the researcher's settings. {because}", kind="report", channel=researcher)
    return base, facts


def _ask_about_stall(board, path, script: str, trials, facts: dict, researcher: str, response: dict | None, why: str) -> None:
    """Settings stopped moving the result. The researcher is asked once per script, and the search carries on."""
    from futuresfund import script_diagnosis as diagnosis

    record = diagnosis.load(script)
    if record.get("stalled"):
        return
    ask_researcher(board, path, script, trials, facts, why, response, researcher, "stall")


def _review_failed_stage(board, path, trials, days: int, execute, facts: dict, researcher: str) -> None:
    """A failed stage goes to the researcher, once for each chart and up to STAGE_REVIEWS on a script.

    While REVIEW_BACKLOG reviews wait on the one model this adds no other, and the next failed chart asks again.
    The answer is used on this script's next chart, or it is a redesign the charts take next.
    """
    holder = getattr(execute, "source", None)
    if board is None or path is None or not trials or not isinstance(holder, dict) or board.cancel.is_set():
        return
    if any(getattr(trials, name, False) for name in ("skipped", "held", "flat_stop", "repeat_stop")):
        return
    last = trials[-1]
    if any(last.get(name) for name in ("error", "fatal", "stopped", "compile", "engine_fault", "unrunnable", "blocked")):
        return
    # A chart with no trades shows nothing to review, and it must not use up this script's review of the stage.
    if all(_no_trades(row) for row in trials):
        return
    from futuresfund import script_diagnosis as diagnosis

    script = str(holder.get("text") or "")
    chart = f"{holder.get('root') or ''} {holder.get('timeframe') or ''}".strip()
    review = f"{days} {chart}".strip()
    reviewed = [str(item) for item in diagnosis.load(script).get("reviewed") or []]
    with _decisions_lock:
        waiting = sum("|stage" in key for key in _decisions_open)
    if review in reviewed or len(reviewed) >= STAGE_REVIEWS or waiting >= REVIEW_BACKLOG:
        return
    best = max(trials, key=_rank)
    shown = [row for row in trials[-5:] if row is not best] + [best]
    where = f" on the {chart} chart" if chart else ""
    budget = int(getattr(trials, "attempt_budget", 0) or stage_attempts(days))
    # The year search is judged and described as the year, not as a 365-day stage.
    bar_days = None if int(days) >= 365 else days
    test = "year search" if bar_days is None else f"{days}-day stage"
    why = (
        f"This script failed the {test}{where} after {len(trials)} different results, of the {budget} that test allows. "
        "The last attempt listed is the best of them. Find out why it fails before more settings are tried."
    )
    from futuresfund.objective import stage_bars_text, stage_miss_lines

    # The stage's own bars, pro-rated from the library boxes, and what the best attempt missed.
    missed = " ".join(stage_miss_lines(best, bar_days))
    why = f"{why} {stage_bars_text(bar_days)} {missed}".rstrip()
    ask_researcher(board, path, script, shown, facts, why, holder.get("response"), researcher, f"stage{days}", review=review)


def _try_skip_window(board, path, trials, execute, researcher: str, slot: int | None, window: dict | None = None, must_improve: bool = False) -> dict | None:
    """Skip the time of day where the best run lost the most. Returns the settings to search from, or None.

    A script without a Skip Window section gets one, measured at the same settings and kept only
    when it holds, like any rewrite. A script that has one only has its window moved. `window`
    names the time to skip instead of reading it from the best run. With must_improve, as after a
    passed stage, a moved window is measured first and used only when the result is better.

    With VALUES_ONLY on, the script's logic is not changed, so no window is added.
    """
    if VALUES_ONLY:
        return None
    from futuresfund import skip_window

    holder = getattr(execute, "source", None)
    if board is None or path is None or not trials or not isinstance(holder, dict) or board.cancel.is_set():
        return None
    runs = [
        row for row in trials
        if isinstance(row, dict) and (row.get("clock") or window)
        and not any(row.get(name) for name in ("error", "fatal", "stopped", "blocked"))
    ]
    if not runs:
        return None
    best = max(runs, key=_rank)
    window = window or skip_window.worst_window(best.get("clock"))
    if window is None:
        return None
    wanted = skip_window.window_params(window)
    settings = dict(best.get("params") or {})
    because = skip_window.describe(window)
    text = str(holder.get("text") or "")
    if skip_window.has_skip(text):
        if all(settings.get(name) == value for name, value in wanted.items()):
            return None
        moved = {**settings, **wanted}
        if must_improve:
            after = execute(moved) or {}
            if not rewrite_holds(best, after, []):
                board.post(researcher, f"{path.name}: {because} Moving the skip window there did not raise the result, so it stays.", kind="log", channel=researcher)
                return None
        board.post(researcher, f"{path.name}: {because} The skip window moves there.", kind="report", channel=researcher)
        return moved
    revised = skip_window.add_skip_window(text, window)
    if revised == text:
        return None
    installed = _install_checked(board, path, revised, execute, best, researcher, slot, because)
    if not installed:
        return None
    return {**settings, **wanted}


def _as_input_type(current, value, options=None):
    """A value read back from the ledger, as the type of the script's own input. A value that does not fit keeps the input's."""
    if isinstance(current, bool):
        return value if isinstance(value, bool) else current
    if isinstance(current, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return current
        return int(round(value)) if isinstance(current, int) else float(value)
    if isinstance(current, str):
        if not isinstance(value, str) or (options and value not in options):
            return current
    return value


def _resume_settings(base: dict, params: dict, facts: dict | None, root: str | None = None) -> dict:
    """The params of a pass kept on the ledger, merged into the base like a carried-over best pass.

    Only the script's own inputs are taken, each as its input's type, inside its min and max.
    """
    known = dict(_pine_params(facts or {}))
    known.update(base or {})
    bounds = _input_bounds(facts)
    choices = _input_choices(facts)
    settings = dict(base or {})
    for name, value in (params or {}).items():
        if name not in known:
            continue
        settings[name] = _limit_value(name, _as_input_type(known[name], value, choices.get(name)), bounds)
    return _with_contract_point(settings, root) if root else settings


def _resume_stage(path, root, timeframe, running: list, base: dict, facts) -> dict | None:
    """Where a stage of this chart that stopped without a result resumes, or None to start at the first stage.

    It resumes when the proving ledger row is resumable (proving.resumable) at a stage that runs, or at
    the year, and every stage that runs before it has a pass kept on the row. That stage starts from the
    params of the last kept pass.
    """
    if path is None or not root:
        return None
    try:
        from futuresfund.proving import resume_point

        point = resume_point(path.name, root, timeframe)
    except (OSError, ValueError, TypeError):
        return None
    if not point:
        return None
    stage = int(point.get("stage") or 0)
    if stage != 365 and stage not in running:
        return None
    kept = [days for days in running if days < stage]
    passed = point.get("passed") or {}
    if any(not isinstance(passed.get(str(days)), dict) for days in kept):
        return None
    settings = dict(base or {})
    if kept:
        settings = _resume_settings(settings, passed[str(kept[-1])].get("params") or {}, facts, root)
    return {"stage": stage, "settings": settings, "kept": kept, "passed": passed, "attempts": point.get("attempts") or {}}


def _carry_resumed_limits(holder: dict, execute, base: dict, resume: dict, root) -> dict:
    """The resumed stage's settings, with the kept pass's trade limits still on. Returns the settings.

    An unbroken study carries its trade cut on the executor from stage to stage: the desk trade limits
    written into its copy of the script, and the daily limits it turned on. A resumed study has a new
    executor, so both are put back here. The copy gets the desk trade limits again when the kept pass
    set one (as _with_owner_answers does), and the settings are merged again with that copy's inputs,
    so the desk_* values and their bounds are kept. A daily limit or desk limit the pass had on marks
    the cut touched, so prepare_research keeps it on in this stage and in the year search.
    """
    settings = resume["settings"]
    kept = resume.get("kept") or []
    if not kept:
        return settings
    params = dict((resume["passed"].get(str(kept[-1])) or {}).get("params") or {})
    if not params:
        return settings
    from futuresfund.research_rules import is_daily_limit
    from futuresfund.strategy_team import TRADE_LIMIT_INPUTS, ensure_trade_limits, has_trade_limits

    if any(name in params for name in TRADE_LIMIT_INPUTS):
        text = str(holder.get("text") or "")
        if text and not has_trade_limits(text):
            installed = ensure_trade_limits(text)
            if installed != text:
                holder["previous"] = text
                holder["text"] = installed
                holder["stamp"] = int(holder.get("stamp") or 0) + 1
                text = installed
        if has_trade_limits(text):
            settings = _resume_settings(dict(base or {}), params, pine_facts(text), root)

    def on(value) -> bool:
        if isinstance(value, bool):
            return value
        return isinstance(value, (int, float)) and value != 0

    if any((is_daily_limit(name) or str(name).startswith("desk_")) and on(value) for name, value in params.items()):
        _cut_state(execute)["touched"] = True
    return settings


def _keep_resumed_stages(holder: dict, resume: dict) -> None:
    """The kept passes stand in for the stages that are not run again, in the notes and for the end of the study.

    stage_runs gets each kept pass's numbers and params, without the clock a run has, so the losing-time
    check reads only the runs it measured. stage_attempts gets the attempts the ledger kept for each stage.
    """
    from futuresfund.proving import _METRICS

    for days in resume["kept"]:
        passed = resume["passed"][str(days)]
        counted = resume["attempts"].get(str(days)) if isinstance(resume["attempts"].get(str(days)), dict) else {}
        used = passed.get("attempts_used") if passed.get("attempts_used") is not None else counted.get("used")
        budget = passed.get("attempt_budget") if passed.get("attempt_budget") is not None else counted.get("budget")
        holder["stage_attempts"][days] = {
            "used": int(used or 0),
            "budget": int(budget or stage_attempts(days)),
            "passed": True,
            "best_attempt": passed.get("best_attempt"),
            "resumed": True,
        }
        holder["stage_runs"][days] = {
            **{key: passed.get(key) for key in _METRICS if passed.get(key) is not None},
            "params": dict(passed.get("params") or {}),
            "stage_days": days,
            "resumed": True,
        }


def _resume_line(path: Path, resume: dict) -> str:
    stage = int(resume["stage"])
    where = "the one-year search" if stage >= 365 else f"the {stage}-day stage"
    kept = resume["kept"]
    if not kept:
        return f"{path.name} resumes at {where}, where it stopped. No earlier pass was needed."
    if len(kept) == 1:
        passes = f"The {kept[0]}-day pass is kept."
    else:
        passes = f"The {', '.join(str(days) for days in kept[:-1])} and {kept[-1]}-day passes are kept."
    return f"{path.name} resumes at {where}, where it stopped, from the {kept[-1]}-day pass. {passes}"


# The recent-window check of the study running on this thread. Filing reads it.
def _prove_stages(base, execute, board, path, pause, facts, developer, researcher, slot, root=None, timeframe=""):
    """Search the last 7 days, then 30, then 90, each from the settings of the best pass of the stage before.

    A stage passes on objective.stage_passed. It keeps searching after its first pass and ends with
    its best pass, the most profit after drawdown (_pass_score), when its attempts (stage_attempts)
    are spent or STAGE_PATIENCE runs in a row found no better pass. The best pass of each stage starts
    the next stage, and the settings that pass all three start the one-year search. Returns those
    settings and None, or None and the trials of the stage the script stopped at, marked with
    stage_failed. An executor without a window (a test double) skips the stages. The best pass of
    each stage is kept in execute.source["stage_runs"], so the end of the study can read the time of
    day those passed runs keep losing, and the attempts each stage used and had are kept in
    execute.source["stage_attempts"] for the notes and the proving ledger.

    A stage that stopped without a result on this chart (a desk or research restart, the engine's
    Restart, an error) resumes there (_resume_stage): the stages before it keep their ledger passes and
    are not run again, and it starts from the params of the last of them, with that pass's trade limits
    still on (_carry_resumed_limits). At the year, the settings are returned at once for the year
    search. Until the study ends, the file is not filed (_resuming).
    """
    from futuresfund.objective import PROVING_STAGES

    holder = getattr(execute, "source", None)
    if not isinstance(holder, dict):
        return base, None
    settings = dict(base or {})
    holder["stage_runs"] = {}
    holder["stage_attempts"] = {}
    # The first stage that runs clears the ledger passes of an earlier study, even when the 7-day stage is off.
    fresh = True
    running = [days for days, _sessions in PROVING_STAGES if days not in STAGES_OFF]
    resume = _resume_stage(path, root, timeframe, running, settings, facts)
    start = 0
    if resume is not None:
        start = int(resume["stage"])
        # Until this study ends the file is not filed, though the row reads "testing" from the next mark.
        if path is not None:
            _begin_resuming(path.name, root, timeframe)
        settings = _carry_resumed_limits(holder, execute, base, resume, root)
        _keep_resumed_stages(holder, resume)
        # Resuming at the first stage that runs starts it like a new study; a later stage keeps the passes.
        fresh = not resume["kept"]
        if board is not None:
            board.post(researcher, _resume_line(path, resume), kind="report", channel=researcher)
        if start >= 365:
            # The three stages passed before the stop, so the year search starts now from the last pass.
            _mark_stage(path, root, timeframe, 365, "testing", {"attempt_budget": stage_attempts(None)}, fresh=fresh, resumed=True)
            return settings, None
    try:
        for days, sessions in PROVING_STAGES:
            if days in STAGES_OFF:
                # The owner turned this stage off. The next stage starts from the same settings.
                continue
            if days < start:
                # Passed before the stop. Its pass stays on the ledger and in stage_runs.
                continue
            budget = stage_attempts(days)
            holder["sessions"] = sessions
            if days == start:
                _mark_stage(path, root, timeframe, days, "testing", {"attempt_budget": budget}, fresh=fresh, resumed=True)
            else:
                _mark_stage(path, root, timeframe, days, "testing", {"attempt_budget": budget}, fresh=fresh)
            fresh = False
            trials = _search(
                settings, execute, ACCOUNT_SIZE, budget, board, path, pause, facts,
                extras=False, developer=developer, researcher=researcher, slot=slot, stage_days=days,
            )
            used = len(trials)
            passed = _best_pass(trials)
            if _stage_cut_off(trials, board, passed):
                # The search was cut off (skipped, restarted, the file was moved, research stopped, or the
                # trade cut needs the owner before any pass). The stage did not fail.
                return None, trials
            if passed is None:
                # One more search of this stage when skipping the time of day that lost the most helps.
                retry = None
                try:
                    retry = _try_skip_window(board, path, trials, execute, researcher, slot)
                except Exception:
                    traceback.print_exc()
                if retry is not None:
                    trials = _search(
                        retry, execute, ACCOUNT_SIZE, budget, board, path, pause, facts,
                        extras=False, developer=developer, researcher=researcher, slot=slot, stage_days=days,
                    )
                    used += len(trials)
                    # The retry is a second search with its own attempts, so the budget shown counts both.
                    budget += budget
                    passed = _best_pass(trials)
                    if _stage_cut_off(trials, board, passed):
                        return None, trials
            spent = {
                "used": used,
                "budget": budget,
                "passed": passed is not None,
                "engine_runs": int(getattr(trials, "engine_runs", 0) or 0),
                "patience_stop": bool(getattr(trials, "patience_stop", False)),
            }
            if passed is not None:
                spent["best_attempt"] = _attempt_number(trials, passed)
            holder["stage_attempts"][days] = spent
            counts = {"attempts_used": used, "attempt_budget": budget}
            if passed is None:
                trials.stage_failed = days
                _mark_stage(path, root, timeframe, days, "failed", counts)
                if STAGE_REVIEW_ON:
                    try:
                        _review_failed_stage(board, path, trials, days, execute, facts, researcher)
                    except Exception:
                        traceback.print_exc()
                return None, trials
            settings = {**settings, **dict(passed.get("params") or {})}
            holder["stage_runs"][days] = passed
            _mark_stage(
                path, root, timeframe, days, "passed",
                {**passed, "params": settings, **counts, "best_attempt": spent["best_attempt"]},
                str(holder.get("text") or ""),
            )
            if board is not None and path is not None:
                later = [stage for stage in running if stage > days]
                after = f"The {later[0]}-day stage" if later else "The one-year search"
                board.post(
                    researcher,
                    f"{path.name} passed the {days}-day stage. Its best pass was attempt {spent['best_attempt']} of the "
                    f"{used} it used ({budget} allowed): {_pass_text(passed)}. {after} starts from those settings.",
                    kind="report",
                    channel=researcher,
                )
    finally:
        holder["sessions"] = None
    _mark_stage(path, root, timeframe, 365, "testing", {"attempt_budget": stage_attempts(None)}, fresh=fresh)
    return settings, None


def _stage_cut_off(trials, board, passed) -> bool:
    """A stage search that was cut off rather than failed. A trade-cut question after a pass does not cut it off:
    the stage keeps that pass and the next stage starts from it."""
    if getattr(trials, "skipped", False) or (board is not None and board.cancel.is_set()):
        return True
    return bool(getattr(trials, "over_trades", "")) and passed is None


def _attempt_number(trials, row) -> int | None:
    """Which attempt of the search this row was, counting from 1."""
    for index, item in enumerate(trials or ()):
        if item is row:
            return index + 1
    return None


_stage_gate = threading.local()


def _stage_checks(execute):
    """Check one-year settings on the last 7, 30, and 90 days. Returns a check, or None for an executor without a window.

    The year search chooses the settings. The check runs those settings on each recent window, shortest
    first, and returns the days of the first window that fails, or None when all pass. Each window's
    run is kept in check.runs, so the ledger can show the numbers. Settings are checked once.
    """
    holder = getattr(execute, "source", None)
    if not isinstance(holder, dict):
        return None
    from futuresfund.objective import PROVING_STAGES, stage_passed

    cache: dict[str, tuple] = {}

    def check(row: dict) -> int | None:
        params = dict(row.get("params") or {})
        key = json.dumps(params, sort_keys=True, default=str)
        if key not in cache:
            failed, runs = None, {}
            saved = holder.get("sessions")
            try:
                for days, sessions in PROVING_STAGES:
                    holder["sessions"] = sessions
                    measured = dict(execute(params) or {})
                    measured["params"] = params
                    runs[days] = measured
                    if any(measured.get(name) for name in ("error", "fatal", "stopped")) or not stage_passed(measured, days, ACCOUNT_SIZE):
                        failed = days
                        break
            finally:
                holder["sessions"] = saved
            cache[key] = (failed, runs)
        failed, check.runs = cache[key]
        return failed

    check.runs = {}
    return check


def _mark_checks(path, root, timeframe, check, row, source: str) -> int | None:
    """Write the recent-window checks of these settings to the proving ledger. Returns the days that failed, or None."""
    failed = check(row)
    for days, measured in sorted(check.runs.items()):
        _mark_stage(path, root, timeframe, days, "failed" if days == failed else "passed", measured, source)
    return failed


_describing: set[str] = set()
_describe_lock = threading.Lock()


def describe_later(board, path: Path, text: str) -> threading.Thread | None:
    """The Trading Analyst describes the script on a side thread, so the chart backtests while the shared model is busy.

    Writing and checking the description waited minutes behind the strategy
    developers on the one model before the first backtest. The accepted
    description goes above the script only while the file still holds the text
    that was described. Charts studying the same file share one description.
    """
    from futuresfund.script_brief import prepare_description

    key = str(path)
    with _describe_lock:
        if key in _describing:
            return None
        _describing.add(key)

    def work() -> None:
        try:
            noted = prepare_description(text, board)
            if noted == text:
                return
            with _describe_lock:
                try:
                    current = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return
                if current == text:
                    path.write_text(noted, encoding="utf-8")
        except Exception:
            traceback.print_exc()
        finally:
            with _describe_lock:
                _describing.discard(key)

    thread = threading.Thread(target=work, daemon=True, name=f"describe-{path.name}")
    thread.start()
    return thread


# Why the study on this thread ended without a stage result, set where it ends.
_stage_end = threading.local()

_END_REASONS = {
    "held": "It is waiting for the researcher's answer. It starts again at the 7-day stage after that.",
    "over_trades": "It still took too many trades after the trade cut. The chart researcher asked the owner.",
    "awaiting": "It waits in awaitingAnswerStrategies for the owner's emailed answer.",
    "skipped": "It was skipped on this chart.",
    "restarted": "The engine was restarted during this stage. It resumes at this stage on its next turn.",
    "cannot_run": "It cannot be run on this chart.",
    "blocked": "It was not filed.",
}
# Study outcomes that end a stage without a result for a reason that is not the script's: the engine's
# Restart, and an engine fault. With no outcome at all (research stopped, the search ended early) or an
# exception the stage resumes too. Skip pressed by the owner, a question, a repair or a park do not.
_RESUME_OUTCOMES = frozenset({"restarted", "engine"})


def _study(board, path: Path, frames: dict, pause, developer: str = "2min chart developer", timeframe: str = "2m", researcher: str = "Chart researcher", engine_slot: int | None = None, *, root: str | None = None):
    """Study one script on one chart. A stage still testing when the study returns is marked stopped, with why.

    root is the instrument of the engine that took the script. Only that instrument's stages are
    marked stopped, so another instrument's engine on the same file and chart keeps its stage.
    A stage cut off by the engine's Restart, an engine fault, an error, or research stopping
    resumes there (proving.resumable); a Skip, a question, a repair or a park does not. Research
    stopping is not counted against proving.MAX_RESUMES (only against MAX_INTERRUPTIONS).
    """
    from futuresfund.proving import _now, stop_testing

    since = _now()
    _stage_end.reason = ""
    _stage_end.chart = {"timeframe": timeframe, "developer": developer, "researcher": researcher,
                        "engine": (int(engine_slot) + 1) if engine_slot is not None else None}
    if root:
        _stage_end.chart["root"] = str(root).upper()
    studied = None
    raised = False
    try:
        if root:
            studied = _study_chart(board, path, frames, pause, developer, timeframe, researcher, engine_slot, root=root)
        else:
            studied = _study_chart(board, path, frames, pause, developer, timeframe, researcher, engine_slot)
        return studied
    except Exception:
        raised = True
        _stage_end.reason = _stage_end.reason or "The search stopped on an error."
        raise
    finally:
        # Read before the reason is filled in: an outcome with no reason of its own ended without a result.
        resume = raised or studied in _RESUME_OUTCOMES or (studied is None and not _stage_end.reason)
        # Research stopping (Stop, or the desk going down) is not the script's fault, so that stop does not
        # count against MAX_RESUMES. Restart, an engine fault and an error do.
        cancelled = board is not None and board.cancel.is_set()
        reason = _stage_end.reason or _END_REASONS.get(str(studied))
        if not reason:
            reason = "Research was stopped before this stage finished." if cancelled else "The search ended before this stage had a result."
        studied_root = (getattr(_stage_end, "chart", None) or {}).get("root") or root
        _stage_end.reason = ""
        _stage_end.chart = {}
        try:
            stop_testing(reason, file=path.name, timeframe=timeframe, since=since, root=studied_root or None, resume=resume, counted=not cancelled)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            # The resumed stage of this study is over and its row is settled, so filing reads the ledger again.
            _end_resuming(path.name, studied_root, timeframe)


def _study_chart(board, path: Path, frames: dict, pause, developer: str = "2min chart developer", timeframe: str = "2m", researcher: str = "Chart researcher", engine_slot: int | None = None, *, root: str | None = None) -> None:
    """Study one script on one instrument's chart. root is the engine's instrument; without one the first pending instrument.

    A root that is no longer pending (another copy or another run finished it) returns at once.
    Only this chart's bars are loaded.
    """
    if not _is_strategy_file(path):
        return
    # An ATR exit is not refused: prepare_backtest converts it to the dollar stop below.
    if not _in_learning(path) and not _in_created(path):
        if board is not None:
            board.post(
                developer,
                _left_text(path),
                kind="log",
                channel=developer,
            )
        return
    from futuresfund.roster import chart_desk

    notes = _read_notes(path)
    # A script over the trade cap is not refused. The chart that measured it cuts its trades (_track_trade_cut).
    # A stage of this chart that stopped and resumes keeps it pending, even with its attempts in the notes.
    pending = _pending_roots(path, timeframe, notes)
    if not pending:
        if ready_to_file(notes):
            _file_ready(board, path, notes)
        return
    again = _resumable_roots(path, timeframe)
    pending = [root for root in pending if root in again or not _copy_finished(path, timeframe, root)]
    if not pending:
        if ready_to_file(_read_notes(path)):
            _file_ready(board, path, _read_notes(path))
        return
    if root:
        # Each engine studies its own instrument. Another copy or a run already finished it: nothing to do here.
        root = str(root).upper()
        if root not in pending:
            return
    else:
        from futuresfund.delta_core import is_core_file, uncovered_first

        if is_core_file(path):
            # An instrument that has no delta volume breakout result yet is tried first.
            pending = uncovered_first(pending)
        root = pending[0]
    label = chart_desk(timeframe)["label"]
    # Which chart a question to the owner comes from (questions.ask_owner reads it through _chart_context).
    _stage_end.chart = {**_chart_context(), "root": root, "timeframe": timeframe, "developer": developer, "researcher": researcher}
    board.post(
        researcher,
        f"Testing {path.name} on the {label} {root} chart. It stays in {_study_home(path)} until the other charts and instruments are finished.",
        kind="report",
        channel=researcher,
    )
    text = path.read_text(encoding="utf-8", errors="replace")
    from futuresfund.entry_rules import repair_dead_entry

    from futuresfund.risk_style import point_value as contract_point
    from futuresfund.strategy_team import convert_atr_exit, prepare_backtest

    source = text
    repaired = repair_dead_entry(source)
    prepared = promote_fixed_pivots(promote_fixed_exits(repaired))
    opened = prepare_backtest(prepared, root)
    # prepare_backtest made the same conversion. This tells the board and the notes what it changed.
    converted, atr_note = convert_atr_exit(prepared, contract_point(root), root=root)
    if atr_note:
        # The conversion is the chart researcher's step in this study, so the post and the notes are the researcher's.
        atr_note = f"{researcher} {atr_note[:1].lower()}{atr_note[1:]}"
        board.post(
            researcher,
            f"{path.name} set its exit with ATR. {atr_note}",
            kind="report",
            channel=researcher,
        )
    if opened != source:
        # The study runs on this prepared copy. The file stays as it was loaded, and the library keeps
        # the exact script each result was measured on.
        text = opened
        if repaired != source:
            board.post(
                developer,
                f"{path.name} had an entry check that could not pass on any bar. It was corrected before the attempts.",
                kind="log",
                channel=developer,
            )
        if promote_fixed_exits(repaired) != repaired:
            board.post(
                developer,
                f"{path.name} had a fixed stop and take profit. They are inputs now, so each attempt can move them.",
                kind="log",
                channel=developer,
            )
        if prepared != promote_fixed_exits(repaired):
            board.post(
                developer,
                f"{path.name} had a fixed pivot length. It is an input now, so the search can measure it.",
                kind="log",
                channel=developer,
            )
        if opened != prepared:
            from futuresfund.strategy_team import own_exit

            board.post(
                developer,
                f"{path.name} keeps its own exit, with an open-loss close at its dollar stop."
                if own_exit(converted)
                else f"{path.name} runs on the shared risk block: a dollar stop, a dollar take profit, and the trail off.",
                kind="log",
                channel=developer,
            )
    describe_later(board, path, text)
    facts = pine_facts(text) if text else {"title": path.stem, "inputs": [], "named": {}}
    file_indicators(board, path.name, text, researcher)
    # Only this engine's chart: the other two timeframes are not read for this study.
    frames = _load_frames(root, timeframes=(timeframe,))
    bars = frames.get(timeframe) or []
    if len(bars) < 80:
        board.post(
            developer,
            f"{root} has no {label} bars yet. {path.name} is set aside on this chart until those bars are there, "
            "and this chart is taking the next script.",
            kind="report",
            channel=developer,
        )
        return "skipped"
    from futuresfund.backtrader_engine import _bars_from, finish_study, set_engine_notice, slot_for
    from futuresfund.entry_rules import ensure_entry_rule
    from futuresfund.pine_runtime import supported
    from futuresfund.strategy_team import compile_error, repair_for_pine

    slot = engine_slot if engine_slot is not None else slot_for(timeframe, root)
    _stage_end.chart = {**_chart_context(), "engine": int(slot) + 1}
    fixed, repairs = repair_for_pine(text)
    if fixed != text:
        # Only the copy this chart runs is repaired. The file stays as it was loaded.
        text = fixed
        if board is not None:
            board.post(
                developer,
                f"{path.name} was repaired so the backtest can start. " + " ".join(repairs),
                kind="log",
                channel=developer,
            )
    refused = compile_error(text)
    if refused or not supported(text):
        return send_to_repair(board, path, developer, timeframe, text, refused or "The script did not compile.", slot, researcher)
    set_engine_notice(slot, "")
    facts = pine_facts(text) if text else {"title": path.stem, "inputs": [], "named": {}}
    added = ensure_entry_rule(board, developer, text, lambda: _bars_from(bars), wait=False)
    if added is None:
        set_engine_notice(
            slot,
            f"Engine {slot + 1} is not backtesting {path.name}. Another chart is still writing its entry rule, so PineTS has not started.",
        )
        return "busy"
    if not added:
        set_engine_notice(
            slot,
            f"Engine {slot + 1} is not backtesting {path.name}. The entry rule was not added, so this script never reached PineTS.",
        )
        return _ask_owner(
            board,
            path,
            researcher,
            "The engine could not add an entry rule, so this script cannot be run as it is.",
            "entry_rule",
            developer,
        )
    execute = _engine_execute(text, frames, facts, board, root, developer, timeframe, slot)
    settings = _with_contract_point(_pine_params(facts), root)
    try:
        if isinstance(getattr(execute, "source", None), dict):
            from futuresfund.backtrader_engine import begin_study

            begin_study(slot, path.name, SUCCESS_COUNT)
            execute.source["root"] = root
            execute.source["timeframe"] = timeframe
            _start_cut_from_notes(board, path, execute, notes, root, timeframe, researcher)
            if READ_BEFORE_TESTING:
                try:
                    settings, facts = _read_before_testing(board, path, execute, facts, settings, researcher, slot)
                    text = str(execute.source.get("text") or text)
                except Exception:
                    traceback.print_exc()
        # Prove in stages: the last 7 days, then 30, then 90, each from the best pass of the stage before.
        # Each stage keeps searching after its first pass for more profit with less drawdown.
        # The year is searched only after all three pass, starting from those settings.
        proven, stopped = _prove_stages(
            settings, execute, board, path, pause, facts, developer, researcher, slot,
            root=root, timeframe=timeframe,
        )
        if stopped is None and RECHECK_BEFORE_FILING:
            # A year run that meets acceptance is filed only when its settings also pass the last 7, 30, and 90 days.
            _stage_gate.check = _stage_checks(execute)
        year_budget = stage_attempts(None)
        # year=True marks this as the chart's one-year search, the one search with the loss stop.
        trials = stopped if stopped is not None else _search(
            proven,
            execute,
            ACCOUNT_SIZE,
            year_budget,
            board,
            path,
            pause,
            facts,
            extras=False,
            developer=developer,
            researcher=researcher,
            slot=slot,
            year=True,
        )
    finally:
        # The recent-window check belongs to this study only. The next study on this thread sets its own.
        _stage_gate.check = None
        finish_study(slot)
    if trials and trials[-1].get("compile"):
        reason = str(trials[-1].get("error") or "The script did not compile.")
        return send_to_repair(board, path, developer, timeframe, text, reason, slot, researcher)
    if trials and trials[-1].get("engine_fault"):
        reason = str(trials[-1].get("error") or "The backtest did not finish.")
        _tell_administrator(board, slot, path, reason)
        return "engine"
    if getattr(trials, "held", False):
        return "held"
    if getattr(trials, "flat_stop", False):
        return getattr(trials, "flat_outcome", "") or "cannot_run"
    if getattr(trials, "over_trades", ""):
        return _ask_over_trades(board, path, researcher, str(trials.over_trades))
    if getattr(trials, "restarted", False):
        # The engine's Restart cut the search. Unlike Skip, the stage resumes on this chart's next turn.
        return "restarted"
    if getattr(trials, "skipped", False):
        return "skipped"
    if board.cancel.is_set() or (trials and trials[-1].get("stopped")):
        return
    if trials and trials[-1].get("unrunnable"):
        return _ask_owner(board, path, researcher, str(trials[-1]["unrunnable"]), "unrunnable", developer)
    if not _in_learning(path) and not _in_created(path):
        if board is not None:
            board.post(
                developer,
                _left_text(path, label),
                kind="log",
                channel=developer,
            )
        return
    if trials and (trials[-1].get("fatal") or trials[-1].get("error")):
        reason = str(trials[-1].get("error") or "The backtest did not finish.")
        if compile_problem(reason):
            start_compile_repair(board, path, developer, timeframe, text, reason, slot, researcher)
            return "repairing"
        _tell_administrator(board, slot, path, reason)
        board.post(
            researcher,
            f"{path.name} was not marked tested on the {label} {root} chart. The systems administrator has the engine problem. This chart is taking the next script.",
            kind="report",
            channel=researcher,
        )
        return "engine"
    if trials and trials[-1].get("blocked"):
        board.post(
            developer,
            f"{path.name} was not filed. {trials[-1].get('note')}",
            kind="report",
            channel=developer,
        )
        return "blocked"
    failed_stage = getattr(trials, "stage_failed", None)
    quant, indicator = adjustment_notes(trials)
    best = max(trials, key=_rank) if trials else {}
    if isinstance(best, dict):
        best = dict(best)
        best["contract"] = f"{root}1!"
        best["timeframe"] = timeframe
        if failed_stage:
            best["stage"] = f"stopped at the {failed_stage}-day stage"
    if atr_note:
        # The notes say the exit this study measured is the converted dollar stop, not the script's ATR distance.
        facts = {**facts, "exit_conversion": atr_note}
    # The attempts each test used and had on this chart: the stages from _prove_stages, then the year.
    stages = {}
    if isinstance(getattr(execute, "source", None), dict):
        stages = {int(days): dict(spent) for days, spent in (execute.source.get("stage_attempts") or {}).items()}
    if stopped is None:
        stages[365] = {"used": len(trials), "budget": int(getattr(trials, "attempt_budget", 0) or year_budget)}
    _save_notes(path, facts, trials, quant, indicator, best, root, timeframe, stages=stages)
    if failed_stage:
        budget = int((stages.get(int(failed_stage)) or {}).get("budget") or stage_attempts(failed_stage))
        board.post(
            researcher,
            f"{path.name} did not pass the {failed_stage}-day stage on the {label} {root} chart in {len(trials)} of its {budget} attempts. "
            "It was not run on the full year.",
            kind="report",
            channel=researcher,
        )
    else:
        _library_notice(board, facts, path, best)
        if isinstance(getattr(execute, "source", None), dict) and isinstance(best, dict):
            from futuresfund.objective import passes_acceptance

            # The year is the last stage, and it is the same check that files the script.
            source = str(execute.source.get("text") or "")
            winners = [row for row in sorted(trials, key=_rank, reverse=True) if passes_acceptance(row)]
            year = winners[0] if winners else None
            skipped = None
            stage_runs = [run for run in (execute.source.get("stage_runs") or {}).values() if isinstance(run, dict)]
            if year is not None or stage_runs:
                # The passed stages and the year show when these settings keep losing, such as the busy open.
                # Skipping that time is kept for the next chart when it raises the year.
                try:
                    from futuresfund.skip_window import consistent_window

                    shown = year if year is not None else best
                    habit = consistent_window([shown.get("clock")] + [run.get("clock") for run in stage_runs])
                    if habit is not None:
                        skipped = _try_skip_window(board, path, [shown], execute, researcher, slot, window=habit, must_improve=True)
                except Exception:
                    traceback.print_exc()
            counts = {"attempts_used": len(trials), "attempt_budget": (stages.get(365) or {}).get("budget") or year_budget}
            if year is not None:
                _mark_stage(path, root, timeframe, 365, "passed", {**year, **counts, "best_attempt": _attempt_number(trials, year)}, source)
            else:
                _mark_stage(path, root, timeframe, 365, "failed", {**best, **counts}, source)
            if year is None:
                try:
                    # The year is not searched again, but the next chart starts with the losing time skipped.
                    if skipped is None:
                        _try_skip_window(board, path, trials, execute, researcher, slot)
                except Exception:
                    traceback.print_exc()
                if STAGE_REVIEW_ON:
                    try:
                        _review_failed_stage(board, path, trials, 365, execute, facts, researcher)
                    except Exception:
                        traceback.print_exc()
    from futuresfund.contracts import POINT_VALUE

    facts = dict(facts)
    facts["point_value"] = float(POINT_VALUE.get(root, facts.get("point_value") or 50))
    from futuresfund.mailer import write_result

    report = ""
    if not failed_stage and _worth_email(best):
        report = write_result(_result_facts(facts, path, best, root, len(trials), getattr(trials, "engine_runs", len(trials))))
    if report:
        _send(board, f"{facts.get('title') or path.stem} on {root} {label}", report)
    notes = _read_notes(path)
    if ready_to_file(notes):
        _file_ready(board, path, notes)
    else:
        board.post(
            researcher,
            f"{path.name} finished the {label} {root} chart. It stays in {_study_home(path)} until the other charts are tested too.",
            kind="report",
            channel="headquarters",
        )
    board.set_status(researcher, "done")
    board.set_status(developer, "done")


# file_completed_soon runs the filing pass at most once a minute across every engine's loop.
FILE_COMPLETED_EVERY = 60.0
_file_completed_lock = threading.Lock()
_file_completed_at = 0.0


def file_completed_soon(board) -> bool:
    """file_completed_scripts, at most once every FILE_COMPLETED_EVERY seconds for all 21 engine loops together.

    Each pass reads every learning script's notes, so it is shared instead of run on every
    engine's every pass. An engine that finds a pass already running does not wait for it.
    True when this call ran the pass.
    """
    global _file_completed_at
    if not _file_completed_lock.acquire(blocking=False):
        return False
    try:
        now = time.monotonic()
        if _file_completed_at and now - _file_completed_at < FILE_COMPLETED_EVERY:
            return False
        _file_completed_at = now
        file_completed_scripts(board)
        return True
    finally:
        _file_completed_lock.release()


def file_completed_scripts(board) -> None:
    """File a script that already finished every chart. An engine will not open it again."""
    for path in list(queue()):
        if not _in_learning(path):
            continue
        notes = _read_notes(path)
        if ready_to_file(notes):
            _file_ready(board, path, notes)


def _file_ready(board, path: Path, notes: dict) -> None:
    if _resume_pending(path):
        # A chart whose stage stopped and resumes is not finished, even though its attempts are in the notes.
        return
    with _notes_lock:
        if not path.is_file():
            return
        names = ", ".join(sorted(tested_instruments(notes)))
        _file_away(path, None)
    board.post(
        "Chart researcher",
        f"Filed {path.name} after it was tested on {names}. It is not tested on those instruments again.",
        kind="report",
        channel="headquarters",
    )


def promote_fixed_exits(text: str) -> str:
    """Turn a literal close ± points stop and target into inputs the search can move."""
    import re

    pattern = re.compile(r"\b(stop|limit)\s*=\s*close\s*([+-])\s*(\d+(?:\.\d+)?)")
    found = pattern.findall(text or "")
    if not found:
        return text
    stops = {num for kind, _op, num in found if kind == "stop"}
    targets = {num for kind, _op, num in found if kind == "limit"}
    if len(stops) != 1 or len(targets) != 1:
        return text
    if re.search(r"\bstop_points\b", text) or re.search(r"\btake_profit_points\b", text):
        return text
    stop_num = next(iter(stops))
    target_num = next(iter(targets))

    def repl(match):
        kind, op, _num = match.group(1), match.group(2), match.group(3)
        name = "stop_points" if kind == "stop" else "take_profit_points"
        return f"{kind}=close {op} {name}"

    updated = pattern.sub(repl, text)
    declaration = (
        f"\nstop_points = input.float({stop_num}, \"Stop Points\", minval=1, maxval=5000)\n"
        f"take_profit_points = input.float({target_num}, \"Take Profit Points\", minval=1, maxval=10000)\n"
    )
    header = re.search(r"strategy\s*\(.*?\)", updated, re.DOTALL)
    if header is None:
        return declaration.lstrip("\n") + updated
    return updated[: header.end()] + declaration + updated[header.end() :]


def promote_fixed_pivots(text: str) -> str:
    """Turn a literal pivot length into an input the search can move."""
    import re

    pivot_call = re.compile(
        r"ta\.pivot(high|low)\s*\(\s*(?:([A-Za-z_][A-Za-z0-9_]*)\s*,\s*)?(\d+)\s*,\s*(\d+)\s*\)",
        re.I,
    )
    body = text or ""
    if re.search(r"\bpivot_left\s*=", body):
        return body
    matches = list(pivot_call.finditer(body))
    if not matches:
        return body
    pairs = {(int(match.group(3)), int(match.group(4))) for match in matches}
    same = len(pairs) == 1

    def repl(match):
        side = match.group(1).lower()
        source = match.group(2)
        left_name = "pivot_left" if same else f"pivot_{side}_left"
        right_name = "pivot_right" if same else f"pivot_{side}_right"
        if source:
            return f"ta.pivot{side}({source}, {left_name}, {right_name})"
        return f"ta.pivot{side}({left_name}, {right_name})"

    updated = pivot_call.sub(repl, body)
    lines = []
    if same:
        left, right = next(iter(pairs))
        lines.append(f'pivot_left = input.int({left}, "Pivot Left", minval=1, maxval=50)')
        lines.append(f'pivot_right = input.int({right}, "Pivot Right", minval=1, maxval=50)')
    else:
        seen = set()
        for match in matches:
            side = match.group(1).lower()
            left, right = int(match.group(3)), int(match.group(4))
            if side in seen:
                continue
            seen.add(side)
            label = "High" if side == "high" else "Low"
            lines.append(f'pivot_{side}_left = input.int({left}, "Pivot {label} Left", minval=1, maxval=50)')
            lines.append(f'pivot_{side}_right = input.int({right}, "Pivot {label} Right", minval=1, maxval=50)')
    declaration = "\n" + "\n".join(lines) + "\n"
    header = re.search(r"strategy\s*\(.*?\)", updated, re.DOTALL)
    if header is None:
        return declaration.lstrip("\n") + updated
    return updated[: header.end()] + declaration + updated[header.end() :]


def _pine_params(facts: dict) -> dict:
    """Every numeric input, every on/off input, and every input that lists choices."""
    params = {}
    for item in facts.get("inputs") or []:
        default = item.get("default")
        if _tunable_value(default):
            params[item["name"]] = default
        elif item.get("options") and isinstance(default, str) and default:
            params[item["name"]] = default
    return params


def _with_contract_point(params: dict, instrument: str) -> dict:
    """Dollar inputs use this contract's point value. NQ is $20, not the $50 written for ES."""
    from futuresfund.contracts import POINT_VALUE, root_of

    try:
        root = root_of(instrument if "!" in str(instrument) else f"{instrument}1!")
    except ValueError:
        root = str(instrument or "ES").upper()
    point = float(POINT_VALUE.get(root, 50))
    updated = params if isinstance(params, dict) else {}
    for key in list(updated):
        if key in {"pv", "point_value", "pointvalue"}:
            updated[key] = point
    return updated


def _input_overrides(facts: dict, params: dict) -> dict:
    """Each input keyed by its label. Two inputs with one label keep their own names, or one would set the other."""
    by_name = {item["name"]: item for item in facts.get("inputs") or []}
    labels = [item.get("label") for item in by_name.values() if item.get("label")]
    overrides = {}
    for key, value in params.items():
        item = by_name.get(key)
        if item:
            label = item.get("label")
            overrides[label if label and labels.count(label) == 1 else key] = value
    return overrides


def _engine_execute(text: str, frames: dict, facts: dict, board, instrument: str = "ES", developer: str = "2min chart developer", timeframe: str = "2m", slot: int | None = None, full: bool = False):
    from futuresfund.backtrader_engine import run_script
    from futuresfund.roster import chart_desk

    label = chart_desk(timeframe)["label"]
    whole = frames.get(timeframe) or []
    # sessions is the proving stage window; None is the one-year research window. Both end on the last bar.
    current = {"text": text, "stamp": 0, "sessions": None}

    def execute(params: dict) -> dict:
        params = _with_contract_point(params, instrument)
        from futuresfund.frequency import apply_session

        source = apply_session(current["text"], params.get("session_window"))
        if current.get("stamp") and current.get("facts_stamp") != current["stamp"]:
            current["facts"] = pine_facts(current["text"])
            current["facts_stamp"] = current["stamp"]
        overrides = _input_overrides(current.get("facts") or facts, params)
        cancel = None if board is None else board.cancel
        title = facts.get("title") or "the script"
        shelf = "GC1!" if instrument in {"QO", "GC", "MGC"} else f"{instrument}1!"
        if cancel is not None and cancel.is_set():
            return {"stopped": True, "runner": "backtrader", "engine": "backtrader", "timeframe": timeframe, "net_profit": None, "max_drawdown": None, "trades": 0}
        bars = whole
        if len(bars) < 80:
            return {
                "timeframe": timeframe,
                "error": f"The {label} chart is not loaded.",
                "fatal": True,
                "net_profit": None,
                "max_drawdown": None,
                "trades": 0,
                "passed": False,
                "runner": "backtrader",
                "engine": "backtrader",
                "instrument": instrument,
                "contract": shelf,
            }

        if board is not None:
            board.log(developer, f"Running {title} on the {label} {instrument} chart")
        measured = run_script(source, bars, timeframe, overrides, cancel, instrument, title, slot, full=full, sessions=current.get("sessions")) or {}
        if board is not None and not measured:
            if cancel is not None and cancel.is_set():
                return {
                    "stopped": True,
                    "runner": "backtrader",
                    "engine": "backtrader",
                    "timeframe": timeframe,
                    "net_profit": None,
                    "max_drawdown": None,
                    "trades": 0,
                }
            measured = {}
        measured["timeframe"] = timeframe
        measured["instrument"] = instrument
        measured["contract"] = shelf
        measured["passed"] = _passed(measured, ACCOUNT_SIZE)
        measured["take_profit"] = params.get("tp_dollars") or facts.get("target")
        measured["frames"] = [{
            "timeframe": timeframe,
            "net_profit": measured.get("net_profit"),
            "max_drawdown": measured.get("max_drawdown"),
            "trades": measured.get("trades"),
            "passed": bool(measured.get("passed")),
        }]
        if board is not None:
            detail = measured.get("error") or (
                f"profit {measured.get('net_profit')}, drawdown {measured.get('max_drawdown')}, trades {measured.get('trades')}"
            )
            board.post(
                developer,
                f"{title} on the {label} {instrument} chart. {detail}.",
                kind="log",
                channel=developer,
            )
        return measured

    execute.source = current
    return execute


def _chart_blurb(frames) -> str:
    if not frames:
        return ""
    names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
    parts = [
        f"{names.get(item.get('timeframe'), item.get('timeframe'))} profit {item.get('net_profit')} "
        f"drawdown {item.get('max_drawdown')} trades {item.get('trades')}"
        for item in frames
    ]
    return " Charts: " + "; ".join(parts) + "."


def file_indicators(board, script: str, text: str, by: str) -> list[dict]:
    """Put a script's indicators in the indicator library. A new one is announced to the creation team."""
    from futuresfund.indicators import record_script

    added = record_script(script, text, by)
    if added and board is not None:
        names = ", ".join(item["name"] for item in added[:8]) + (f", and {len(added) - 8} more" if len(added) > 8 else "")
        note = f"Added {len(added)} indicator{'s' if len(added) != 1 else ''} from {script} to the indicator library: {names}."
        board.post(by, note, kind="report", channel=by)
        board.post("Card keeper", note, kind="log", channel="Card keeper")
        from futuresfund.roster import CHART_DESKS

        for desk in CHART_DESKS:
            board.post(desk["strategy_developer"], note, kind="log", channel=desk["strategy_developer"])
    return added


def _load_frames(root: str = "ES", *, timeframes=None) -> dict[str, list]:
    """TradingView's exports and the alert candles. Yahoo is not part of a backtest.

    timeframes narrows it to those charts: a chart engine studies one chart, so it builds only that one.
    """
    from futuresfund.research_bars import research_series

    names = [name for name in RESEARCH_CHARTS if timeframes is None or name in timeframes]
    # USE_ALERT_BARS off leaves out the alert candles, so a study runs on the downloaded and exported bars only.
    return {name: research_series(root, name, include_live=USE_ALERT_BARS) for name in names}


def _across_charts(path: Path | None, frames: dict, facts: dict):
    """One attempt runs on the 5-minute engine chart and on the 2-minute and 15-minute charts."""
    runners = {}
    base = None
    for name in RESEARCH_CHARTS:
        bars = frames.get(name) or []
        if len(bars) < 80:
            runners[name] = None
            continue
        found, execute = _executor(path, bars, facts)
        runners[name] = execute
        if base is None:
            base = found
    if base is None:
        raise ValueError("The 2-minute, 5-minute, and 15-minute charts could not be loaded.")

    def execute(params: dict) -> dict:
        rows = []
        for name in RESEARCH_CHARTS:
            runner = runners.get(name)
            if runner is None:
                rows.append({
                    "timeframe": name,
                    "error": f"The {name} chart is not loaded.",
                    "net_profit": None,
                    "max_drawdown": None,
                    "trades": 0,
                    "passed": False,
                })
                continue
            measured = runner(params) or {}
            measured["timeframe"] = name
            measured["passed"] = _passed(measured, ACCOUNT_SIZE)
            rows.append(measured)
        best = dict(_best_frame(rows))
        best["frames"] = [
            {
                "timeframe": row.get("timeframe"),
                "net_profit": row.get("net_profit"),
                "max_drawdown": row.get("max_drawdown"),
                "trades": row.get("trades"),
                "passed": bool(row.get("passed")),
            }
            for row in rows
        ]
        return best

    return base, execute


def _best_frame(rows: list[dict]) -> dict:
    """Keep the chart that cleared the goal. A tie stays on the 5-minute engine chart."""
    def key(row: dict):
        profit = row.get("net_profit")
        profit = float(profit) if profit is not None else -10**12
        drawdown = float(row.get("max_drawdown") or 10**12)
        return (1 if row.get("passed") else 0, profit, -drawdown, 1 if row.get("timeframe") == ENGINE_CHART else 0)

    return max(rows, key=key)


def _executor(path: Path | None, bars: list, facts: dict):
    cls = _load_class(path) if path else None
    no_volume = not _bars_have_volume(bars)
    if cls is not None:
        frame = _frame(bars)
        base = _class_params(cls)
        blocked = no_volume and _needs_volume(cls, facts, path)

        def execute(params: dict) -> dict:
            if blocked:
                return {
                    "net_profit": 0.0,
                    "max_drawdown": 0.0,
                    "trades": 0,
                    "runner": cls.__name__,
                    "take_profit": params.get("tp_dollars"),
                    "blocked": True,
                    "note": (
                        "The chart export has no volume column, so this script cannot enter. "
                        "A different strategy was not substituted."
                    ),
                }
            try:
                from engine.backtester import Backtester

                kind = params.get("added_indicator")
                runner = _with_indicator(cls, str(kind)) if kind else cls
                clean = {
                    key: value
                    for key, value in params.items()
                    if key not in {"added_indicator"}
                }
                clean["initial_capital"] = float(ACCOUNT_SIZE)
                report = Backtester(frame, runner, **clean).run(progress=False)
                metrics = report.metrics
                reason = str(getattr(report, "eval_stop_reason", "") or "")
                trades = int(metrics.get("total_trades") or 0)
                result = {
                    "net_profit": float(metrics.get("net_profit") or 0),
                    "max_drawdown": abs(float(metrics.get("max_drawdown") or 0)),
                    "trades": trades,
                    "win_rate": metrics.get("win_rate"),
                    "runner": runner.__name__,
                    "stop_reason": reason,
                    "breached": "Trailing DD" in reason,
                    "take_profit": params.get("tp_dollars"),
                }
                return result
            except Exception as exc:
                return {"error": str(exc), "runner": cls.__name__}

        return base, execute
    name = _family(facts, path)
    base = _family_base(name)

    def execute(params: dict) -> dict:
        from futuresfund.research import evaluate_rule

        try:
            usable = {
                key: value
                for key, value in params.items()
                if key not in {"added_indicator", "added_length"}
            }
            row = evaluate_rule(bars, "ES1!", ACCOUNT_SIZE, PROFIT_TARGET, name, usable)
        except Exception as exc:
            return {"error": str(exc), "runner": name}
        if not row:
            return {"error": "The bars were too short for this rule.", "runner": name}
        backtest = row["backtest"]
        return {
            "net_profit": backtest.get("net_profit"),
            "max_drawdown": backtest.get("max_drawdown"),
            "trades": backtest.get("trades"),
            "win_rate": backtest.get("win_rate"),
            "runner": name,
        }

    return base, execute


def _family(facts: dict, path: Path | None) -> str:
    blob = f"{facts.get('title') or ''} {path.stem if path else ''}".lower()
    if "bollinger" in blob or "mean" in blob:
        return "bollinger"
    if "macd" in blob:
        return "macd"
    if "supertrend" in blob:
        return "supertrend"
    if "rsi" in blob:
        return "rsi_revert"
    if "vwap" in blob:
        return "vwap_side"
    if "poc" in blob:
        return "poc_pullback"
    if "break" in blob or "orb" in blob or "donchian" in blob:
        return "donchian"
    return "ema_cross"


def _family_base(name: str) -> dict:
    bases = {
        "ema_cross": {"fast": 12, "slow": 50, "stop": 250},
        "bollinger": {"length": 20, "dev": 2, "stop": 250},
        "macd": {"fast": 12, "slow": 26, "signal": 9, "stop": 250},
        "supertrend": {"length": 10, "mult": 3, "stop": 250},
        "rsi_revert": {"period": 14, "low": 30, "high": 70, "stop": 250},
        "vwap_side": {"length": 20, "stop": 250},
        "poc_pullback": {"lookback": 50, "rows": 12, "stop": 250},
        "donchian": {"length": 20, "stop": 250},
    }
    return dict(bases.get(name) or bases["ema_cross"])


def _bars_have_volume(bars: list) -> bool:
    return any(float(bar.get("v") or 0) > 0 for bar in bars)


def _needs_volume(cls, facts: dict, path: Path | None) -> bool:
    blob = " ".join([
        getattr(cls, "__name__", ""),
        str(facts.get("title") or ""),
        path.name if path else "",
    ]).lower()
    return any(word in blob for word in ("delta", "volume", "vwma", "obv", "poc", "profile"))


def _price_rule(params: dict, facts: dict, path: Path | None) -> tuple[str, dict, float]:
    kind = str(params.get("added_indicator") or "")
    raw_length = params.get("length") or params.get("profile_lookback") or params.get("ema_len") or 20
    try:
        length = int(float(raw_length))
    except (TypeError, ValueError):
        length = 20
    length = max(10, min(60, length))
    raw_stop = params.get("stop") or params.get("stop_dollars") or 250
    try:
        stop = float(raw_stop)
    except (TypeError, ValueError):
        stop = 250.0
    stop = max(50.0, min(2000.0, stop))
    raw_target = params.get("tp_dollars") or params.get("take_profit") or 0
    try:
        take_profit = float(raw_target)
    except (TypeError, ValueError):
        take_profit = 0.0
    if kind in {"ema", "sma", "wma", "vwma"}:
        fast = max(5, min(20, max(5, length // 3)))
        slow = max(fast + 5, min(120, max(length, fast + 10)))
        return "ema_cross", {"fast": fast, "slow": slow, "stop": stop}, take_profit
    if kind == "rsi":
        period = max(8, min(21, length // 3 or 14))
        return "rsi_revert", {"period": period, "low": 30, "high": 70, "stop": stop}, take_profit
    return "donchian", {"length": length, "stop": stop}, take_profit


def _price_result(bars: list, params: dict, facts: dict, path: Path | None) -> dict:
    from futuresfund.research import evaluate_rule

    name, rule, take_profit = _price_rule(params, facts, path)
    note = "These bars have no volume, so a delta rule cannot enter. This attempt trades price with the script's stop and take profit."
    try:
        row = evaluate_rule(bars, "ES1!", ACCOUNT_SIZE, PROFIT_TARGET, name, rule, qty=1, take_profit=take_profit or None)
    except Exception as exc:
        return {"error": str(exc), "runner": name, "take_profit": take_profit, "note": note}
    if not row:
        return {"error": "The price rule could not run on these bars.", "runner": name, "take_profit": take_profit, "note": note}
    backtest = row["backtest"]
    return {
        "net_profit": backtest.get("net_profit"),
        "max_drawdown": backtest.get("max_drawdown"),
        "trades": backtest.get("trades"),
        "win_rate": backtest.get("win_rate"),
        "runner": name,
        "breached": bool(backtest.get("breached")),
        "stop_reason": "Trailing DD breached" if backtest.get("breached") else "",
        "take_profit": take_profit,
        "note": note,
    }


def _change_text(before: dict | None, after: dict) -> str:
    if not before:
        return "Starting settings"
    keys = _changed_keys(before, after)
    if not keys:
        return "Same settings"
    parts = []
    for key in keys:
        if key == "session_window" and before.get(key) is None:
            parts.append(f"session window to {after.get(key)}")
        else:
            parts.append(f"{key} {before.get(key)} to {after.get(key)}")
    return ", ".join(parts)


def _load_class(path: Path):
    if not path or not path.is_file():
        return None
    engine = str(ENGINE_ROOT)
    if engine not in sys.path:
        sys.path.insert(0, engine)
    try:
        spec = importlib.util.spec_from_file_location(f"learn_{path.stem}", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    from engine.strategy import Strategy

    found = [
        obj for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, Strategy) and obj is not Strategy
    ]
    return found[0] if found else None


def _class_params(cls) -> dict:
    params = {}
    for key, value in vars(cls).items():
        if key.startswith("_") or key in FROZEN:
            continue
        if _tunable_value(value):
            params[key] = value
    return params


def _frame(bars: list):
    import pandas as pd
    from engine.data_loader import DataLoader

    frame = pd.DataFrame({
        "datetime": [bar.get("t") for bar in bars],
        "open": [float(bar["o"]) for bar in bars],
        "high": [float(bar["h"]) for bar in bars],
        "low": [float(bar["l"]) for bar in bars],
        "close": [float(bar["c"]) for bar in bars],
        "volume": [float(bar.get("v") or 0) for bar in bars],
    })
    return DataLoader._normalize(frame)


def _remember_measured_lesson(facts: dict, path: Path, row: dict, researcher: str, developer: str) -> None:
    """Keep the engine's latest measurement on the desks that will compact it into the lessons."""
    if not isinstance(row, dict):
        return
    if row.get("net_profit") is None or row.get("max_drawdown") is None or row.get("trades") is None:
        return
    change = str(row.get("change") or "").strip()
    if not change:
        return
    from futuresfund.llm import note_result

    measured = {
        "script": str(facts.get("title") or path.stem),
        "timeframe": str(row.get("timeframe") or ""),
        "instrument": str(row.get("instrument") or ""),
        "change": change,
        "net_profit": row.get("net_profit"),
        "max_drawdown": row.get("max_drawdown"),
        "trades": row.get("trades"),
        "impact": str(row.get("impact") or "").strip(),
    }
    note_result(researcher, measured)
    if developer != researcher:
        note_result(developer, measured)


def _document(board, facts: dict, path: Path, row: dict, number: int, developer: str = "2min chart developer", researcher: str = "Chart researcher", limit: int | None = None) -> None:
    """Post and record one counted year result. limit is the attempts this search has, ATTEMPT_LIMIT when not given."""
    from futuresfund.lab import record_trial

    limit = int(limit or ATTEMPT_LIMIT)
    title = f"{facts.get('title') or path.stem} attempt {number}"
    record_trial({
        "id": f"{path.stem}-{number}",
        "title": title,
        "rule": {"name": row.get("runner")},
        "proven": bool(row.get("passed")),
        "backtest": {
            "net_profit": row.get("net_profit"),
            "max_drawdown": row.get("max_drawdown"),
            "trades": row.get("trades"),
        },
    }, title)
    change = row.get("change") or "Starting settings"
    timeframe = row.get("timeframe") or ENGINE_CHART
    names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
    label = names.get(str(timeframe), str(timeframe))
    charts = _chart_blurb(row.get("frames"))
    take_profit = row.get("take_profit")
    target = f" Take profit ${float(take_profit):,.0f}." if isinstance(take_profit, (int, float)) and float(take_profit) > 0 else ""
    note = f" {row.get('note')}" if row.get("note") else ""
    if board is not None:
        board.post(
            researcher,
            f"Attempt {number} of {limit} on {path.name}. {row.get('impact') or row.get('change')}. "
            f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.",
            kind="report",
            channel=researcher,
        )
        board.post(
            "Risk Manager",
            f"Attempt {number} of {limit} on {path.name}. {_attempt_note(row)} "
            f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.",
            kind="report",
            channel="Risk Manager",
        )
        try:
            from futuresfund.risk_style import note_risk

            suggestion = note_risk(row, path.name, facts.get("title") or path.stem)
        except Exception:
            suggestion = None
        if suggestion:
            board.post("Risk Manager", suggestion, kind="report", channel="Risk Manager")
        board.post(
            "Trading Analyst",
            f"Attempt {number} of {limit} on {path.name}. {row.get('impact') or row.get('change')}.",
            kind="log",
            channel="Trading Analyst",
        )
    from futuresfund.strategy import remember_research

    remember_research(row, path.name, facts.get("title") or path.stem)
    try:
        _remember_chart_attempt(path, facts, row, number)
    except Exception:
        traceback.print_exc()
    _remember_measured_lesson(facts, path, row, researcher, developer)
    board.post(
        developer,
        f"Attempt {number} of {limit} on the {label} chart: {path.name}. {change}. "
        f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.{target} "
        f"{_attempt_note(row)}{note}{charts}",
        kind="report",
        channel=developer,
    )
    _offer_library(board, facts, path, row, developer)


_BACKTEST_DEVELOPERS = {
    "2min chart developer",
    "5 min chart developer",
    "15 min chart developer",
}


def _offer_library(board, facts: dict, path: Path, row: dict, developer: str = "2min chart developer", opening: bool = False) -> bool:
    """A chart engine files a one-year run on the Not tested list. The creation engine does not."""
    if developer not in _BACKTEST_DEVELOPERS:
        return False
    from futuresfund.pine_check import tv_problems

    if path is not None and path.is_file() and tv_problems(path.read_text(encoding="utf-8", errors="replace")):
        return False
    engine_name = str(row.get("engine") or row.get("runner") or "")
    if engine_name not in {"pinets", "backtrader"}:
        return False
    try:
        profit = float(row.get("net_profit"))
    except (TypeError, ValueError):
        return False
    from futuresfund.library import library_ready
    from futuresfund.objective import acceptance_reason, consistent_spans, ratio_success, too_many_to_save, TRADE_SAVE_MAX

    if too_many_to_save(row):
        if board is not None:
            board.post(
                developer,
                f"{path.name} took {int(row.get('trades') or 0)} trades. A run with more than {TRADE_SAVE_MAX} trades is never filed; "
                "the desk cuts its trades with daily limits, a longer cooldown and most trades a day.",
                kind="log",
                channel=developer,
            )
        return False
    if _in_created(path) and not consistent_spans(row.get("spans")):
        if board is not None:
            board.post(
                developer,
                f"{path.name} is not profitable on every window of 7, 30, 90, and 365 days, so it was not filed.",
                kind="log",
                channel=developer,
            )
        return False
    if not library_ready(row):
        if opening and board is not None:
            board.post(developer, f"{path.name} was measured with its opening stop and take profit. {acceptance_reason(row)}", kind="log", channel=developer)
        return False
    gate = getattr(_stage_gate, "check", None)
    if gate is not None:
        failed = gate(row)
        if failed is not None:
            if board is not None:
                board.post(
                    developer,
                    f"{path.name} met acceptance on the year, but at these settings it did not pass the last {failed} days, so it was not filed. The attempts continue.",
                    kind="log",
                    channel=developer,
                )
            return False
    instrument = str(row.get("instrument") or "ES").upper()
    shelf = "GC1!" if instrument in {"QO", "GC", "MGC"} else f"{instrument}1!"
    from futuresfund.library import contract_of, record_profitable

    title = facts.get("title") or path.stem
    symbol, _label = contract_of(str(title), path.name, shelf)
    change = str(row.get("change") or "").strip()
    shown = str(title)
    if change and change not in {"Original", "Starting settings"} and not change.startswith("Starting measurement"):
        shown = f"{title} — {change}"
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    fingerprint = json.dumps([symbol, row.get("timeframe"), params, profit, int(row.get("trades") or 0)], sort_keys=True, default=str)
    entry = {
        "id": f"{path.stem}-{__import__('hashlib').sha1(fingerprint.encode()).hexdigest()[:12]}",
        "title": shown,
        "script": str(title),
        "file": path.name,
        "contract": symbol,
        "contract_label": _label,
        "timeframe": row.get("timeframe") or "",
        "net_profit": profit,
        "max_drawdown": row.get("max_drawdown"),
        "dips": row.get("dips") or [],
        "trades": int(row.get("trades") or 0),
        "win_rate": row.get("win_rate"),
        "sharpe": row.get("sharpe"),
        "profit_factor": row.get("profit_factor"),
        "profit_ratio": row.get("profit_ratio"),
        "biggest_winner": row.get("biggest_winner"),
        "biggest_loser": row.get("biggest_loser"),
        "held_loser": row.get("held_loser"),
        "stop_dollars": row.get("stop_dollars"),
        "days": row.get("days"),
        "trades_per_day": row.get("trades_per_day"),
        "params": params,
        "source_sha": row.get("source_sha") or "",
        "engine": engine_name,
        "runner": str(row.get("runner") or engine_name),
    }
    if record_profitable(entry) and board is not None:
        _library_notice(board, facts, path, entry)
        from futuresfund.objective import firm_ratio

        if firm_ratio(entry) and _worth_email(entry):
            _mail_firm_ratio(board, facts, path, entry)
    from futuresfund.library import record_untested

    filed = record_untested(entry)
    if opening and board is not None:
        if filed:
            board.post(
                developer,
                f"Filed {entry.get('title') or path.name} on the Not tested on the full dataset list. "
                f"Profit {entry.get('net_profit')}. That result passes acceptance. The attempts continue.",
                kind="log",
                channel=developer,
            )
        else:
            board.post(
                developer,
                f"{path.name} was measured with its opening stop and take profit. The attempts continue.",
                kind="log",
                channel=developer,
            )
        return bool(filed)
    if filed and board is not None:
        board.post(
            developer,
            f"Filed {entry.get('title') or path.name} on the Not tested on the full dataset list. "
            f"Profit {entry.get('net_profit')}, trades {entry.get('trades')}. The full dataset test comes later.",
            kind="log",
            channel=developer,
        )
    if filed and ratio_success(entry) and _worth_email(entry) and board is not None:
        _mail_ratio(board, facts, path, entry)
    return bool(filed)


def _write_instrument(path: Path, facts: dict, instruments: dict, best: dict) -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    existing = _read_notes(path)
    payload = dict(existing)
    payload["file"] = path.name
    payload["title"] = facts.get("title") or existing.get("title") or path.stem
    payload["instruments"] = instruments
    payload["best"] = best
    slug = path.stem.replace(" ", "_")
    (NOTES / f"{slug}.json").write_text(_notes_json(payload), encoding="utf-8")


def _library_notice(board, facts: dict, path: Path, best: dict) -> None:
    """A finished profitable study goes on the strategy library. The analyst records the account."""
    title = facts.get("title") or path.stem
    try:
        profit = float(best.get("net_profit"))
    except (TypeError, ValueError):
        profit = None
    trades = int(best.get("trades") or 0)
    if profit is None or profit <= 0 or trades < 1 or str(best.get("engine") or best.get("runner") or "") not in {"pinets", "backtrader"}:
        if board is not None:
            board.post(
                "Trading Analyst",
                f"{title} finished testing and was not added to the strategy library. "
                f"Profit {best.get('net_profit')}, trades {best.get('trades')}.",
                kind="report",
                channel="Trading Analyst",
            )
        return
    from futuresfund.library import contract_of

    _symbol, label = contract_of(str(title), path.name, best.get("contract"))
    note = (
        f"{title} is in the {label} strategy library. "
        f"{best.get('timeframe')} profit {profit}, drawdown {best.get('max_drawdown')}, trades {trades}."
    )
    if board is not None:
        from futuresfund.roster import chart_desk

        researcher = chart_desk(str((best or {}).get("timeframe") or "2m"))["researcher"]
        board.post(researcher, note, kind="report", channel=researcher)
        board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")


def _mark_over_cap(path: Path, count: int, chart_key: str = "") -> None:
    """Remember the most trades this chart measured over TRADE_SAVE_MAX, under over_trade_charts["ES|2m"].

    Information only, and per chart: the next study of this chart starts with the trade cut. No chart
    skips the script for it.
    """
    slug = path.stem.replace(" ", "_")
    with _notes_lock:
        existing = _read_notes(path)
        charts = existing.get("over_trade_charts") if isinstance(existing.get("over_trade_charts"), dict) else {}
        key = chart_key or "|"
        charts[key] = max(int(count), int(charts.get(key) or 0))
        existing["over_trade_charts"] = charts
        existing["file"] = existing.get("file") or path.name
        NOTES.mkdir(parents=True, exist_ok=True)
        # Information only, so a write the bind mount refuses is let go; the old note stays whole.
        _replace_note(NOTES / f"{slug}.json", _notes_json(existing))


def _notes_json(payload: dict) -> str:
    """A research note as one line of JSON. The C encoder writes even a 3 MB note in a few
    milliseconds and holds the other engines far less than an indented file did, and the file is half the size."""
    return json.dumps(payload, default=str)


def _read_notes(path: Path) -> dict:
    slug = path.stem.replace(" ", "_")
    file = NOTES / f"{slug}.json"
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_failure(path: Path, message: str) -> None:
    try:
        from futuresfund.errors import record

        record("Chart researcher", f"{path.name}: the engine stopped: {message}", source="engine")
    except Exception:
        pass
    notes = _read_notes(path)
    pending = pending_instruments(notes)
    root = pending[0] if pending else "ES"
    _save_notes(
        path,
        {"title": path.stem},
        [],
        f"The engine stopped: {message}",
        "No indicator pass was completed.",
        {"error": message, "net_profit": None, "max_drawdown": None, "trades": 0, "contract": f"{root}1!"},
        root,
    )


OBJECTIVE_MARK = NOTES / ".objective"
_RERANK_SKIP = {
    "library_index.json",
    "strategy_cards.json",
    "creation_sources.json",
    "indicator_library.json",
    "creation_lessons.json",
}


def rerank_notes(store: Path | None = None, *, force: bool = False) -> int:
    """Choose each chart's best result again under the research objective. Returns how many studies changed.

    A study's trials are kept as they were measured. Only the choice of best, per chart, per instrument,
    and for the study, is made again under objective.py. Runs once per objective; the marker file
    remembers that it ran.
    """
    from futuresfund.objective import stamp

    current = stamp()
    folder = store or NOTES
    mark = folder / ".objective"
    if not folder.is_dir():
        return 0
    if not force and mark.is_file() and mark.read_text(encoding="utf-8").strip() == current:
        return 0
    changed = 0
    with _notes_lock:
        for path in sorted(folder.glob("*.json")):
            if path.name in _RERANK_SKIP:
                continue
            try:
                if path.stat().st_size > 20_000_000:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or _rerank_study(data) == 0:
                continue
            path.write_text(_notes_json(data), encoding="utf-8")
            changed += 1
        mark.write_text(current, encoding="utf-8")
    return changed


def _rerank_study(data: dict) -> int:
    """Re-choose the bests inside one study in place. Returns how many best fields moved."""
    moved = 0

    def choose(current, rows, root: str = "", timeframe: str = ""):
        """Older trial rows do not name their chart, so the chart's root and timeframe fill in for the pace."""
        nonlocal moved

        def key(row: dict):
            if (root and not row.get("instrument")) or (timeframe and not row.get("timeframe")):
                row = {**row, "instrument": row.get("instrument") or root, "timeframe": row.get("timeframe") or timeframe}
            return _rank(row)

        candidates = [row for row in rows if isinstance(row, dict) and row]
        if not candidates:
            return current
        top = max(candidates, key=key)
        placed = isinstance(current, dict) and current and (timeframe or current.get("timeframe"))
        if placed and key(current) >= key(top):
            return current
        moved += 1
        return top

    instruments = data.get("instruments") if isinstance(data.get("instruments"), dict) else {}
    for root, record in instruments.items():
        if not isinstance(record, dict):
            continue
        charts = record.get("charts") if isinstance(record.get("charts"), dict) else {}
        if charts:
            for timeframe, chart in charts.items():
                if isinstance(chart, dict):
                    chart["best"] = choose(chart.get("best"), chart.get("trials") or [], str(root), str(timeframe))
                    if isinstance(chart["best"], dict) and chart["best"]:
                        # The chart's best names its chart, so the study's best can be compared across charts.
                        if not chart["best"].get("timeframe"):
                            chart["best"]["timeframe"] = str(timeframe)
                        if not chart["best"].get("instrument"):
                            chart["best"]["instrument"] = str(root)
            record["best"] = choose(record.get("best"), [chart.get("best") for chart in charts.values() if isinstance(chart, dict)], str(root))
        else:
            record["best"] = choose(record.get("best"), record.get("trials") or [], str(root))
    if instruments:
        data["best"] = choose(data.get("best"), [record.get("best") for record in instruments.values() if isinstance(record, dict)])
    elif isinstance(data.get("trials"), list):
        data["best"] = choose(data.get("best"), data["trials"])
    return moved


def _remember_chart_attempt(path: Path, facts: dict, row: dict, number: int) -> None:
    """Write this finished attempt onto its chart now, so the file counts before the search ends."""
    timeframe = str(row.get("timeframe") or "").strip()
    root = str(row.get("instrument") or "").upper().strip()
    if not timeframe or not root or not isinstance(path, Path):
        return
    slug = path.stem.replace(" ", "_")
    with _notes_lock:
        existing = _read_notes(path)
        instruments = existing.get("instruments") if isinstance(existing.get("instruments"), dict) else {}
        prior = instruments.get(root) if isinstance(instruments.get(root), dict) else {}
        charts = dict(prior.get("charts") or {}) if isinstance(prior.get("charts"), dict) else {}
        chart = charts.get(timeframe) if isinstance(charts.get(timeframe), dict) else {}
        trials = [item for item in (chart.get("trials") or []) if isinstance(item, dict)]
        stored = dict(row)
        if number > 0 and len(trials) >= number:
            trials[number - 1] = stored
        else:
            trials.append(stored)
        best = chart.get("best") if isinstance(chart.get("best"), dict) else {}
        if not best or _rank(stored) >= _rank(best):
            best = stored
        charts[timeframe] = {
            "attempts": len(trials),
            "best": best,
            "quantitative_researcher": chart.get("quantitative_researcher") or "",
            "indicator_researcher": chart.get("indicator_researcher") or "",
            "trials": trials,
            "error": stored.get("error") or "",
            "blocked": bool(stored.get("blocked")),
        }
        chosen = best
        for item in charts.values():
            candidate = item.get("best") if isinstance(item, dict) else None
            if isinstance(candidate, dict) and candidate and _rank(candidate) > _rank(chosen or {}):
                chosen = candidate
        instruments[root] = {
            "attempts": sum(int(item.get("attempts") or 0) for item in charts.values() if isinstance(item, dict)),
            "best": chosen,
            "charts": charts,
            "profitable": list(prior.get("profitable") or []),
            "quantitative_researcher": prior.get("quantitative_researcher") or "",
            "indicator_researcher": prior.get("indicator_researcher") or "",
            "trials": trials,
        }
        overall = chosen if isinstance(chosen, dict) else {}
        for record in instruments.values():
            candidate = record.get("best") if isinstance(record, dict) else None
            if isinstance(candidate, dict) and candidate and _rank(candidate) > _rank(overall or {}):
                overall = candidate
        # Every other key the notes hold stays: the owner's emailed answers, the over-cap charts, the ATR
        # conversion, the reopened count.
        payload = dict(existing) if isinstance(existing, dict) else {}
        payload.update({
            "file": path.name,
            "title": facts.get("title") or existing.get("title") or path.stem,
            "attempts": sum(int(record.get("attempts") or 0) for record in instruments.values() if isinstance(record, dict)),
            "best": overall,
            "quantitative_researcher": existing.get("quantitative_researcher") or "",
            "indicator_researcher": existing.get("indicator_researcher") or "",
            "trials": trials,
            "instruments": instruments,
        })
        NOTES.mkdir(parents=True, exist_ok=True)
        # The desk's bind mount sometimes refuses a write (OSError, Errno 22). The old note stays whole, the
        # attempt is in the search's own trials, and the end of the study writes the whole chart.
        _replace_note(NOTES / f"{slug}.json", _notes_json(payload))


# A note that could not be written is tried this many times, a short pause apart.
NOTE_WRITE_TRIES = 4
NOTE_WRITE_PAUSE = 0.05


def _replace_note(file: Path, text: str) -> bool:
    """Write a note to a temporary file beside it and move it into place. False when every try failed.

    A failed try never leaves a half-written note: the old file stays until the new one replaces it.
    """
    import os

    temp = file.with_name(f"{file.name}.{threading.get_ident()}.tmp")
    problem = None
    for attempt in range(NOTE_WRITE_TRIES):
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, file)
            return True
        except OSError as exc:
            problem = exc
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt + 1 < NOTE_WRITE_TRIES:
                time.sleep(NOTE_WRITE_PAUSE * (attempt + 1))
    print(f"The note {file.name} was not written after {NOTE_WRITE_TRIES} tries: {problem}", file=sys.stderr)
    return False


def _save_notes(path: Path, facts: dict, trials: list, quant: str, indicator: str, best: dict, instrument: str = "ES", timeframe: str = "", *, stages: dict | None = None) -> None:
    """Write this chart's result into the script's notes.

    stages holds the attempts each test used and had on this chart, {days: {"used", "budget", ...}}.
    It is kept on the chart record as "stages", keyed "7", "30", "90" and "365".
    """
    NOTES.mkdir(parents=True, exist_ok=True)
    slug = path.stem.replace(" ", "_")
    with _notes_lock:
        existing = _read_notes(path)
        instruments = existing.get("instruments") if isinstance(existing.get("instruments"), dict) else {}
        if not instruments and (existing.get("trials") or isinstance(existing.get("best"), dict) and existing.get("best")):
            instruments["ES"] = {
                "attempts": existing.get("attempts") or len(existing.get("trials") or []),
                "best": existing.get("best") or {},
                "quantitative_researcher": existing.get("quantitative_researcher") or "",
                "indicator_researcher": existing.get("indicator_researcher") or "",
                "trials": existing.get("trials") or [],
            }
        root = str(instrument or "ES").upper()
        chart = {
            "attempts": len(trials),
            "best": best,
            "quantitative_researcher": quant,
            "indicator_researcher": indicator,
            "trials": trials,
            "error": (best or {}).get("error") or "",
            "blocked": bool((best or {}).get("blocked")),
        }
        budget = int(getattr(trials, "attempt_budget", 0) or 0)
        if budget:
            # The attempts the search these trials came from had: a failed stage's, or the year's.
            chart["attempt_budget"] = budget
        if stages:
            chart["stages"] = {str(int(days)): dict(spent) for days, spent in sorted(stages.items(), key=lambda item: int(item[0]))}
        if facts.get("exit_conversion"):
            # The stop this chart measured: a micro starts at $50, a full contract at $250.
            chart["exit_conversion"] = facts["exit_conversion"]
        prior = instruments.get(root) if isinstance(instruments.get(root), dict) else {}
        charts = dict(prior.get("charts") or {}) if isinstance(prior.get("charts"), dict) else {}
        if timeframe:
            charts[timeframe] = chart
            chosen_chart = best if isinstance(best, dict) else {}
            for item in charts.values():
                candidate = item.get("best") if isinstance(item, dict) else None
                if isinstance(candidate, dict) and candidate and _rank(candidate) > _rank(chosen_chart or {}):
                    chosen_chart = candidate
            instruments[root] = {
                "attempts": sum(int(item.get("attempts") or 0) for item in charts.values() if isinstance(item, dict)),
                "best": chosen_chart,
                "charts": charts,
                "profitable": list(prior.get("profitable") or []),
                "quantitative_researcher": quant,
                "indicator_researcher": indicator,
                "trials": trials,
            }
        else:
            instruments[root] = chart
        chosen = best if isinstance(best, dict) else {}
        for record in instruments.values():
            candidate = record.get("best") if isinstance(record, dict) else None
            if isinstance(candidate, dict) and candidate and _rank(candidate) > _rank(chosen or {}):
                chosen = candidate
        analyst = existing.get("analyst") or ""
        try:
            script_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            script_text = ""
        if "How this script works" in script_text:
            from futuresfund.frequency import mechanism_note

            analyst = mechanism_note(script_text)
        payload = {
            "file": path.name,
            "title": facts.get("title") or existing.get("title") or path.stem,
            "attempts": sum(int(record.get("attempts") or 0) for record in instruments.values() if isinstance(record, dict)),
            "best": chosen,
            "quantitative_researcher": quant,
            "indicator_researcher": indicator,
            "analyst": analyst,
            "trials": trials,
            "instruments": instruments,
        }
        if isinstance(existing.get("owner_answers"), dict) and existing["owner_answers"]:
            # The owner's emailed values start every later chart's search (questions.owner_settings).
            payload["owner_answers"] = existing["owner_answers"]
        if isinstance(existing.get("over_trade_charts"), dict) and existing["over_trade_charts"]:
            # The charts that measured a run over the trade cap start their next study with the trade cut.
            payload["over_trade_charts"] = existing["over_trade_charts"]
        # A script whose ATR exit the desk converted keeps that note, so every later chart's save still shows it.
        conversion = facts.get("exit_conversion") or existing.get("exit_conversion")
        if conversion:
            payload["exit_conversion"] = conversion
        # Through a temporary file: a refused write on the bind mount never leaves the note empty or half
        # written (that would make every other chart of the file pending). When every try fails, the old
        # note stays whole and the error reaches the study, so its stage resumes instead of being filed half-saved.
        if not _replace_note(NOTES / f"{slug}.json", _notes_json(payload)):
            raise OSError(f"The note {slug}.json was not written.")
        lesson = _saved_lesson(payload["title"], trials, best)
        lessons = NOTES / "LESSONS.md"
        recorded = lessons.read_text(encoding="utf-8") if lessons.is_file() else ""
        if lesson not in recorded:
            with lessons.open("a", encoding="utf-8") as handle:
                handle.write(lesson + "\n")


def _result_facts(facts: dict, path: Path, best: dict, root: str, attempts: int, engine_runs: int | None = None) -> str:
    """What the application shows for this result, including whether the library actually has it."""
    from futuresfund.library import contract_of, shelf_for, versions_filed
    from futuresfund.objective import days_for, trades_per_week

    title = str(facts.get("title") or path.stem)
    symbol, label = contract_of(title, path.name, (best or {}).get("contract") or f"{root}1!")
    profit = (best or {}).get("net_profit")
    drawdown = (best or {}).get("max_drawdown")
    trades = (best or {}).get("trades")
    timeframe = (best or {}).get("timeframe") or ""
    names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
    chart = names.get(str(timeframe), str(timeframe) or "chart")
    rate = trades_per_week(best)
    days = days_for(best)
    on_shelf = _same_result(shelf_for(title, symbol), best)
    on_waiting = _same_result(versions_filed(title, symbol, str(timeframe)), best)
    bars = (best or {}).get("bars")
    start = (best or {}).get("tested_from") or ""
    end = (best or {}).get("tested_through") or ""
    from futuresfund.objective import acceptance_line

    lines = [
        "Taken from the application just now.",
        acceptance_line(),
        f"Strategy: {title}.",
        f"Chart tested: {chart} {label} ({symbol}).",
        f"Profit {profit}. Drawdown {drawdown}. Trades {trades}.",
        f"Trades a week: {rate if rate is not None else 'not measured'}"
        + (f" over {days:.0f} trading days." if days else "."),
    ]
    if bars:
        lines.append(f"Bars in this test: {int(bars):,} from {start} through {end}.")
    lines.append(
        "The acceptance line above is the only bar for filing on this desk. Every agent and this report use those four numbers. "
        "The headquarters profit target and trailing drawdown are not used to accept or reject a run."
    )
    if on_shelf:
        lines.append(f"On the strategy library: yes. This result is on the {label} strategy library.")
    elif on_waiting:
        lines.append(
            f"On the strategy library: yes, on the not-tested list for {label}. It has not been run on the five-year dataset."
        )
    else:
        lines.append(f"On the strategy library: no. This result was not filed in the strategy library. {_left_off(best, False, title, symbol)}")
    params = (best or {}).get("params") if isinstance((best or {}).get("params"), dict) else {}
    if params:
        shown = ", ".join(f"{key}={value}" for key, value in list(params.items())[:12])
        lines.append(f"Settings of this version: {shown}.")
    lines.append(f"Different results on this chart: {attempts}.")
    lines.append(f"Engine runs on this chart: {engine_runs if engine_runs is not None else attempts}.")
    return "\n".join(lines)


def _same_result(rows: list[dict], best: dict) -> bool:
    """True when the shelf is holding this result, not merely another run of the same script."""
    try:
        profit = round(float(best.get("net_profit")), 2)
        trades = int(best.get("trades") or 0)
    except (TypeError, ValueError):
        return False
    timeframe = str(best.get("timeframe") or "")
    for row in rows:
        try:
            same_profit = round(float(row.get("net_profit")), 2) == profit
            same_trades = int(row.get("trades") or 0) == trades
        except (TypeError, ValueError):
            continue
        if same_profit and same_trades and str(row.get("timeframe") or "") == timeframe:
            return True
    return False


def _worth_email(row: dict | None) -> bool:
    """A portfolio-manager strategy email needs a result that passes acceptance."""
    from futuresfund.objective import passes_acceptance

    return passes_acceptance(row)


def _left_off(best: dict, meets: bool, title: str = "", contract: str = "") -> str:
    from futuresfund.objective import acceptance_reason

    reason = acceptance_reason(best)
    if reason:
        return reason
    from futuresfund.library import versions_filed

    profit = float(best.get("net_profit"))
    others = [
        row for row in versions_filed(title, contract or str(best.get("contract") or ""), str(best.get("timeframe") or ""))
        if not _same_result([row], best)
    ]
    if not others:
        return "No version of this name is on this contract and timeframe."
    held = max(others, key=lambda row: float(row.get("net_profit") or 0))
    try:
        held_profit = float(held.get("net_profit"))
    except (TypeError, ValueError):
        return "No version of this name is on this contract and timeframe."
    if held_profit > profit:
        return (
            "A stronger version of this name is already on this contract and timeframe. "
            f"That version made ${held_profit:,.2f}. This run made ${profit:,.2f}."
        )
    return (
        f"The version on file made ${held_profit:,.2f}. This run made ${profit:,.2f}. "
        "No stronger version is on this contract and timeframe."
    )


def _mail_script(path: Path, row: dict) -> str:
    """The Pine script the email's numbers came from, with the run's inputs written in, ready to paste into TradingView."""
    from futuresfund.library import _dollars_as_percent, _measured_script
    from futuresfund.strategy import _apply_inputs, pine_text

    try:
        pine = _measured_script(row.get("source_sha")) or pine_text(path.name) or ""
    except (OSError, ValueError, TypeError):
        pine = ""
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    if pine and params:
        try:
            pine = _apply_inputs(pine, _dollars_as_percent(pine, params))
        except (TypeError, ValueError, re.error):
            pass
    if not str(pine).strip():
        return "The script could not be read, so it is not attached. Open it from the strategy library."
    return "\n".join([
        "The script, with the inputs this run used:",
        "----- Pine script start -----",
        str(pine).rstrip(),
        "----- Pine script end -----",
    ])


def _mail_ratio(board, facts: dict, path: Path, row: dict) -> None:
    """The portfolio manager emails a 1.5 profit-to-drawdown run that has not been tested on the full dataset."""
    from futuresfund.objective import acceptance_line, profit_to_drawdown

    title = str(facts.get("title") or path.stem)
    ratio = profit_to_drawdown(row)
    shown = "no drawdown" if ratio == float("inf") else f"{ratio:.2f}"
    report = "\n".join([
        f"Strategy: {title}.",
        f"File: {path.name}.",
        f"Chart: {row.get('timeframe') or ''} {row.get('contract') or ''}.",
        f"Profit {row.get('net_profit')}. Drawdown {row.get('max_drawdown')}. Trades {row.get('trades')}.",
        f"Profit ratio {row.get('profit_ratio')}. That is the average winning trade divided by the average losing trade, and it is above 1.12.",
        f"Profit is {shown} times the drawdown. A profitable run at 1.5 or more counts.",
        acceptance_line(),
        "This run used a year of futures sessions. It has not been tested on the full dataset.",
        "On the not-yet-full-dataset list: yes.",
        "",
        _mail_script(path, row),
    ])
    _send(board, title, report, subject=f"Futures fund library: {title}", emailed="the library notice")


def _mail_firm_ratio(board, facts: dict, path: Path, row: dict) -> None:
    """The portfolio manager emails a run near $50,000 profit and $22,000 drawdown."""
    from futuresfund.library import shelf_for
    from futuresfund.objective import acceptance_line

    title = str(facts.get("title") or path.stem)
    symbol = str(row.get("contract") or "")
    if not _same_result(shelf_for(title, symbol), row):
        return
    labels = {"ES1!": "ES", "NQ1!": "Nasdaq", "GC1!": "Gold"}
    shelf_name = labels.get(symbol, symbol or "the")
    report = "\n".join([
        f"Strategy: {title}.",
        f"File: {path.name}.",
        f"Chart: {row.get('timeframe') or ''} {symbol}.",
        f"Profit {row.get('net_profit')}. Drawdown {row.get('max_drawdown')}. Trades {row.get('trades')}.",
        f"Profit ratio {row.get('profit_ratio')}. That is the average winning trade divided by the average losing trade, and it is above 1.12.",
        f"On the strategy library: yes, under {shelf_name}.",
        acceptance_line(),
        "This profit and drawdown are near $50,000 of profit and $22,000 of drawdown. "
        "A profit $10,000 lower still counts, and any higher profit counts.",
        "",
        _mail_script(path, row),
    ])
    _send(board, title, report, subject=f"Futures fund library: {title}", emailed="the library notice")


def _send(board, title: str, report: str, subject: str | None = None, emailed: str = "the research report") -> None:
    from futuresfund.mailer import send_report

    folder = NOTES / "mail"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() else "_" for ch in title)[:80]
    (folder / f"{safe}.txt").write_text(report, encoding="utf-8")
    subject = subject or f"Futures fund research report: {title}"

    def deliver() -> None:
        result = send_report(report, subject=subject) or {}
        if result.get("sent"):
            lead = f"Emailed {emailed} for {title} to {result.get('to')}."
            board.set_task("Portfolio Manager", f"Emailed {emailed} for {title}", working=False)
        else:
            lead = result.get("reason") or "The report was not emailed."
            board.set_task("Portfolio Manager", f"{emailed[0].upper()}{emailed[1:]} was not emailed", working=False)
        board.post(
            "Portfolio Manager",
            f"{lead}\n\n{report}",
            kind="report",
            channel="Portfolio Manager",
        )

    from futuresfund.crew import get_crew

    # The chart thread keeps studying. The portfolio manager's own thread sends the mail.
    get_crew(board).submit(
        "Portfolio Manager",
        f"Sending {emailed} for {title}",
        deliver,
    )


def _file_away(path: Path, twin: Path | None) -> None:
    """Only the Pine script is filed. The Python copy stays put."""
    if not _is_strategy_file(path):
        return
    LEARNED.mkdir(parents=True, exist_ok=True)
    _move(path)


def _move(path: Path) -> None:
    target = LEARNED / path.name
    if target.exists():
        target = LEARNED / f"{path.stem}_{int(time.time())}{path.suffix}"
    shutil.move(str(path), str(target))


def _risk_line() -> str:
    from futuresfund.prop_rules import active_limits

    rules = active_limits(ACCOUNT_SIZE)
    kind = "trailing" if rules["trailing"] else "fixed"
    return (
        f"The ${rules['max_drawdown']:,.0f} {kind} drawdown must not be crossed "
        f"before the ${rules['profit_target']:,.0f} profit target is met."
    )


def _passed(row: dict, account_size: float) -> bool:
    """The profit target has to be met, and the trailing drawdown must not have been crossed first."""
    if row.get("error") or row.get("net_profit") is None or row.get("max_drawdown") is None:
        return False
    from futuresfund.prop_rules import active_limits

    rules = active_limits(account_size)
    from futuresfund.objective import year_allowance

    trail = year_allowance(float(row["net_profit"]), account_size)
    target = float(rules["profit_target"])
    if float(row["max_drawdown"]) > trail:
        return False
    if "Profit target" in str(row.get("stop_reason") or ""):
        return int(row.get("trades") or 0) >= 1
    return float(row["net_profit"]) >= target and int(row.get("trades") or 0) >= 1


def _rank(row: dict):
    """Full-chart search: 130 to 600 trades a year, then the lower drawdown, then the larger profit."""
    from futuresfund.objective import research_rank

    return research_rank(row)


def _result_key(measured: dict) -> tuple:
    frames = tuple(
        (
            frame.get("timeframe"),
            frame.get("net_profit"),
            frame.get("max_drawdown"),
            frame.get("trades"),
        )
        for frame in measured.get("frames") or []
        if isinstance(frame, dict)
    )
    return (
        measured.get("net_profit"),
        measured.get("max_drawdown"),
        measured.get("trades"),
        frames,
    )


def _better(row: dict, origin: dict) -> bool:
    profit = float(row.get("net_profit") or -10**12)
    base = float(origin.get("net_profit") or -10**12)
    drawdown = float(row.get("max_drawdown") or 10**12)
    base_dd = float(origin.get("max_drawdown") or 10**12)
    return profit > base and drawdown <= base_dd


def _wait(board, pause) -> bool:
    while pause.is_set():
        if board.cancel.is_set():
            return False
        time.sleep(1)
    return not board.cancel.is_set()


def _sleep(board, pause, seconds: int) -> bool:
    for _ in range(seconds):
        if not _wait(board, pause):
            return False
        time.sleep(1)
    return True


def _tunable_value(value) -> bool:
    return isinstance(value, (bool, int, float)) and not isinstance(value, str)


def _clamp(name: str, value: int) -> int:
    lowered = name.lower()
    if "hour" in lowered or lowered.endswith("_hr"):
        return max(0, min(23, value))
    if "minute" in lowered or lowered.endswith("_mn"):
        return max(0, min(59, value))
    return value


def _input_bounds(facts) -> dict:
    """Each input's min and max. A dollar stop or take profit may go down to $50, so a micro search is not pushed back to $250."""
    from futuresfund.research_rules import EXIT_MINVAL, exit_step

    bounds = {}
    for item in (facts or {}).get("inputs") or []:
        if item.get("min") is not None or item.get("max") is not None:
            low = item.get("min")
            name = str(item["name"])
            dollars = exit_step(name) is not None and not name.lower().endswith("pct") and "percent" not in name.lower()
            if dollars and low is not None and float(low) > EXIT_MINVAL:
                low = EXIT_MINVAL
            bounds[item["name"]] = (low, item.get("max"))
    return bounds


def _clamp_plan(plan: dict | None, bounds: dict) -> dict | None:
    if not plan:
        return plan
    limited = {}
    for key, value in plan.items():
        limited[key] = _limit_value(key, value, bounds)
    from futuresfund.research_rules import hold_reward

    return hold_reward(_order_ranges(limited))


def _range_role(name: str) -> tuple[str, str] | None:
    """A min/max pair such as minMult and maxMult. Session start and end are not a pair."""
    for role, prefix in (("min", "min_"), ("max", "max_"), ("min", "min"), ("max", "max")):
        if name.lower().startswith(prefix) and len(name) > len(prefix):
            rest = name[len(prefix):]
            if prefix.endswith("_") or rest[:1].isupper():
                return role, rest.lstrip("_").lower()
    return None


def _order_ranges(plan: dict) -> dict:
    """Keep a minimum at or below its maximum. The script stops when the minimum is larger."""
    groups: dict[str, dict[str, str]] = {}
    for name in plan:
        found = _range_role(name)
        if found is None:
            continue
        role, stem = found
        if not isinstance(plan.get(name), (int, float)) or isinstance(plan.get(name), bool):
            continue
        groups.setdefault(stem, {})[role] = name
    ordered = dict(plan)
    for pair in groups.values():
        low_name, high_name = pair.get("min"), pair.get("max")
        if not low_name or not high_name:
            continue
        low, high = ordered[low_name], ordered[high_name]
        if low <= high:
            continue
        snapped = high if isinstance(low, int) else round(float(high), 4)
        if isinstance(low, int) and not isinstance(high, int):
            snapped = int(round(float(high)))
        ordered[low_name] = snapped
    return ordered


def _rejected_setting(error) -> bool:
    """The script refused the settings. That is a bad attempt, not a broken engine."""
    text = str(error or "")
    return "Min factor > Max factor" in text


def _unrunnable_reason(row: dict) -> str:
    """Why this script cannot be run. Empty when the attempt can simply be changed."""
    if row.get("stopped") or row.get("blocked"):
        return ""
    text = str(row.get("error") or "").strip()
    lowered = text.lower()
    if "not loaded" in lowered or "stopped" in lowered:
        return ""
    if _rejected_setting(text):
        return ""
    if compile_problem(text) or "did not finish" in lowered:
        return ""
    if row.get("fatal"):
        return ""
    if any(phrase in lowered for phrase in ("cannot run", "unsupported", "not supported")):
        return text or "The engine cannot run this script."
    return ""


def _limit_value(name: str, value, bounds: dict):
    """Keep a setting inside the input's min and max so TradingView can load the script."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, int):
        value = _clamp(name, int(value))
    low, high = bounds.get(name, (None, None))
    number = float(value)
    if low is not None:
        number = max(float(low), number)
    if high is not None:
        number = min(float(high), number)
    if isinstance(value, int):
        return int(round(number))
    return round(number, 4)


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, default=str)


def _short_params(params: dict) -> str:
    parts = [f"{key}={value}" for key, value in sorted(params.items())[:8]]
    return ", ".join(parts) if parts else "the original inputs"


def _attempt_note(row: dict) -> str:
    try:
        profit = float(row.get("net_profit"))
        drawdown = float(row.get("max_drawdown") or 0)
        trades = int(row.get("trades") or 0)
    except (TypeError, ValueError):
        return "The engine did not finish a measured result."
    if trades < 1:
        return "This setting did not trade. The search keeps looking for a result that trades."
    from futuresfund.objective import goal_line

    return (
        f"Drawdown {_money(drawdown)}. Profit {_money(profit)}. "
        f"{goal_line()} "
        "The take profit stays larger than the stop. A strategy with one trade down more than $750 is not included."
    )


def _profit(row: dict) -> float:
    value = row.get("net_profit")
    if value is None:
        return -10**12
    return float(value)


def _changed_keys(before: dict, after: dict) -> list[str]:
    names = set(before) | set(after)
    return sorted(name for name in names if before.get(name) != after.get(name))


def _input_choices(facts) -> dict:
    """Listed choices for an input.string, keyed by the input name."""
    choices = {}
    for item in (facts or {}).get("inputs") or []:
        options = [option for option in (item.get("options") or []) if isinstance(option, str) and option]
        if len(options) > 1:
            choices[item["name"]] = options
    return choices


def _taken_out(name: str) -> bool:
    """An input the running search took out, because moving it changed nothing in either direction."""
    tracker = input_effects.current()
    return tracker is not None and tracker.is_removed(name)


def _dead_move(name: str, old, new) -> bool:
    """A move this search already made in this direction without changing the result."""
    tracker = input_effects.current()
    return tracker is not None and tracker.dead_move(name, old, new)


def _searchable(key: str, value, choices: dict | None = None) -> bool:
    """An input the search may move. Account size, commission, and quantity stay fixed."""
    if key in FROZEN or key in {"added_indicator", "added_length"}:
        return False
    if _tunable_value(value):
        return True
    return isinstance(value, str) and bool((choices or {}).get(key))


def _drawdown_rose(previous: dict, row: dict) -> bool:
    return float(row.get("max_drawdown") or 0) > float(previous.get("max_drawdown") or 0) + 0.5


def _profit_drop_impactful(previous: dict, row: dict) -> bool:
    """True when profit fell by 5 percent or more of the previous profit."""
    before = _profit(previous)
    after = _profit(row)
    if after >= before:
        return False
    drop = before - after
    base = abs(before)
    if base < 1:
        return drop >= 1
    return drop / base >= IMPACT_PERCENT


def _lost_pace(previous: dict, row: dict) -> bool:
    from futuresfund.objective import paced

    return paced(previous) is True and paced(row) is False


def _is_stop_name(name: str) -> bool:
    """A dollar stop. A point distance, a trail, and a mode choice are not one."""
    from futuresfund.research_rules import _exit_kind

    return _exit_kind(name) == "stop"


def _is_tighten_name(name: str) -> bool:
    """An input that cuts the trade count when it is moved the strict way. The desk trade limits are ones."""
    lowered = (name or "").lower()
    if lowered in {"desk_daily_loss", "desk_daily_profit", "desk_max_trades_day", "desk_cooldown_bars"}:
        return True
    if "cooldown" in lowered:
        return True
    if "max" in lowered and "trade" in lowered:
        return True
    return lowered.startswith("min") and "minute" not in lowered and not lowered.endswith("_mn")


def _lowers_trade_cap(name: str) -> bool:
    lowered = (name or "").lower()
    return "max" in lowered and "trade" in lowered


def _profit_flat(previous: dict, row: dict) -> bool:
    """True when the profit barely moved. A nudge that small needs a larger next step."""
    before = _profit(previous)
    after = _profit(row)
    if before <= -10**11:
        return False
    delta = abs(after - before)
    base = max(abs(before), 1.0)
    return delta < 1 or delta / base < 0.01


def _needs_drama(previous: dict | None, row: dict | None, name: str | None) -> bool:
    if previous is None or row is None:
        return False
    return _profit_flat(previous, row)


def _best_money(trials: list[dict]) -> dict | None:
    """The result closest to a profit: the larger profit, then the lower drawdown."""
    ranked = [row for row in trials if isinstance(row, dict) and _profit(row) > -10**11]
    if not ranked:
        return None
    return max(ranked, key=lambda row: (_profit(row), -float(row.get("max_drawdown") or 0)))


def _is_target_name(name: str) -> bool:
    lowered = (name or "").lower().replace("_", "")
    return "takeprofit" in lowered or lowered.startswith("tp") or "target" in lowered


def _is_exit_name(name: str) -> bool:
    return _is_stop_name(name) or _is_target_name(name)


def _exit_was_tried(trials: list[dict]) -> bool:
    """True once a stop or a take profit has already been moved."""
    for earlier, later in zip(trials, trials[1:]):
        changed = _changed_keys(earlier.get("params") or {}, later.get("params") or {})
        if any(_is_exit_name(name) for name in changed):
            return True
    return False


def _plan_from_profit(trials: list[dict], best: dict, seen: set[str], choices: dict | None, seeds: dict | None) -> dict | None:
    """Go back to the profitable settings and cut trades while the drawdown is too high."""
    anchor = dict(best.get("params") or {})
    if trials[-1] is best and len(trials) > 1:
        from futuresfund.research_rules import focus_keys

        previous = trials[-2]
        changed = focus_keys(previous.get("params") or {}, best.get("params") or {})
        if len(changed) == 1 and _is_tighten_name(changed[0]):
            others = [name for name in _input_order(anchor, choices) if name != changed[0]]
            choice = _climb_choice(previous, best, bool(others), changed[0])
            if choice == "continue":
                plan = _step_one(
                    anchor, changed[0],
                    (previous.get("params") or {}).get(changed[0]),
                    anchor.get(changed[0]),
                    1, seen, choices, seeds, dramatic=True,
                )
                if plan:
                    return plan
    return _tighten_plan(anchor, seen, choices) or _step_past_exits(anchor, seen, choices, seeds)


def _plan_toward_profit(best: dict, seen: set[str], choices: dict | None, seeds: dict | None) -> dict | None:
    """The stop was already widened and the script is still losing. Cut trades from the smallest loss."""
    anchor = dict(best.get("params") or {})
    return _tighten_plan(anchor, seen, choices) or _step_past_exits(anchor, seen, choices, seeds)


def _step_past_exits(anchor: dict, seen: set[str], choices: dict | None, seeds: dict | None) -> dict | None:
    for name in _input_order(anchor, choices):
        if _is_exit_name(name):
            continue
        plan = _step_one(anchor, name, None, None, 1, seen, choices, seeds, dramatic=True)
        if plan:
            return plan
    return None


def _should_cut_trades(row: dict, account_size: float) -> bool:
    """A profit with a drawdown still past the pro account rule. Cut trades next."""
    from futuresfund.objective import year_allowance

    if _profit(row) <= 0:
        return False
    try:
        drawdown = float(row.get("max_drawdown") or 0)
    except (TypeError, ValueError):
        return False
    return drawdown > year_allowance(_profit(row), account_size)


def _scaled(value, scale: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        nxt = int(round(float(value) * scale))
        if nxt == value:
            nxt = value + (1 if scale > 1 else -1)
        return nxt if nxt > 0 else None
    nxt = round(float(value) * scale, 4)
    return nxt if nxt > 0 and nxt != value else None


def _tighten_order(params: dict, choices: dict | None = None) -> list[str]:
    def rank(name: str) -> tuple:
        lowered = name.lower()
        if "cooldown" in lowered:
            return (0, lowered)
        if _lowers_trade_cap(name):
            return (1, lowered)
        if lowered.startswith("desk_daily"):
            return (2, lowered)
        return (3, lowered)

    return sorted(
        (name for name in params if _is_tighten_name(name) and _searchable(name, params.get(name), choices)),
        key=rank,
    )


def _tighten_plan(anchor: dict, seen: set[str], choices: dict | None = None) -> dict | None:
    """One larger step that lets fewer trades through."""
    from futuresfund.trade_cut import _dollars_up, profit_limit, stop_of, tighten_value

    for name in _tighten_order(anchor, choices):
        value = anchor.get(name)
        lowered = name.lower()
        if lowered.startswith("desk_") or value == 0:
            # 0 is off. Tightening a limit that is off sets its starting value, not a half of nothing.
            seed = None
            if lowered == "desk_daily_loss":
                seed = _dollars_up(2 * stop_of(anchor))
            elif lowered == "desk_daily_profit":
                seed = profit_limit(anchor, 0)
            nxt = tighten_value(name, value, seed)
            if nxt is None and not lowered.startswith("desk_"):
                nxt = _scaled(value, 0.5 if _lowers_trade_cap(name) else 2)
        else:
            nxt = _scaled(value, 0.5 if _lowers_trade_cap(name) else 2)
        if nxt is None:
            continue
        plan = dict(anchor)
        plan[name] = nxt
        if plan[name] == value or _key(plan) in seen or len(_changed_keys(anchor, plan)) != 1 or _dead_move(name, value, nxt):
            continue
        return plan
    return None


def _climb_choice(previous: dict, row: dict, others: bool, name: str | None = None, account_size: float = ACCOUNT_SIZE) -> str:
    """continue, reverse, or switch. A profit gain is extended until drawdown rises or profit falls 5 percent.

    A losing stop is left after one widen. A flat profit takes a larger step. A change that
    stopped every entry is turned back.
    """
    if _traded(previous) and not _traded(row) and not any(row.get(name) for name in ("error", "fatal", "blocked")):
        return "reverse"
    if _drawdown_rose(previous, row):
        return "switch" if others else "reverse"
    if _is_stop_name(name or "") and _profit(row) <= 0 and _profit(previous) < 0:
        return "switch" if others else "reverse"
    if _lost_pace(previous, row) or _profit_drop_impactful(previous, row):
        return "reverse"
    if _profit_flat(previous, row) or _profit(row) != _profit(previous):
        return "continue"
    return "reverse"


def _input_order(params: dict, choices: dict | None = None) -> list[str]:
    """The stop and the take profit come first. Every other searchable input follows."""

    def rank(name: str) -> tuple:
        lowered = name.lower().replace("_", "")
        exit_name = any(word in lowered for word in ("stop", "takeprofit", "target", "stoploss")) or "tp" in lowered or lowered.startswith("sl")
        return (0 if exit_name else 1, lowered)

    return sorted(
        (key for key, value in params.items() if _searchable(key, value, choices) and not _taken_out(key)),
        key=rank,
    )


def _step_one(anchor: dict, name: str | None, old, new, direction: int, seen: set[str], choices: dict | None = None, seeds: dict | None = None, *, dramatic: bool = False, losing: bool = False, widen: bool = False) -> dict | None:
    """Return a plan that differs in exactly one input."""
    if not name or name not in anchor:
        names = _input_order(anchor, choices)
        name = names[0] if names else None
    if not name:
        return None
    current = anchor.get(name)
    candidates = []
    if widen:
        doubled = _scaled(current, 2)
        if doubled is not None:
            candidates.append(doubled)
    elif direction > 0 and old is not None and new is not None:
        nxt = _continue_step(name, old, new, choices, dramatic=dramatic)
        if nxt is not None:
            candidates.append(nxt)
    if direction < 0 and old is not None and new is not None:
        nxt = _reverse_step(name, old, new, choices)
        if nxt is not None:
            candidates.append(nxt)
    probe_drama = dramatic or widen or (losing and _is_stop_name(name) and old is None)
    candidates.extend(_probe_steps(name, current, choices, seeds, dramatic=probe_drama))
    if isinstance(current, bool) and direction > 0 and old is not None and current is True:
        candidates = [item for item in candidates if item is not False]
    for nxt in candidates:
        plan = dict(anchor)
        plan[name] = nxt
        if plan[name] == current or _dead_move(name, current, nxt):
            continue
        if _key(plan) not in seen and len(_changed_keys(anchor, plan)) == 1:
            return plan
    return None


def _impact(previous: dict | None, row: dict) -> str:
    """What the one changed variable did, and what the next run will do with it."""
    if previous is None:
        return "Starting measurement. The next run changes one variable from here."
    keys = _changed_keys(previous.get("params") or {}, row.get("params") or {})
    name = keys[0] if len(keys) == 1 else "That change"
    old = (previous.get("params") or {}).get(name) if len(keys) == 1 else None
    new = (row.get("params") or {}).get(name) if len(keys) == 1 else None
    label = f"{name} {old} to {new}" if len(keys) == 1 else name
    profit_move = _profit(row) - _profit(previous)
    drawdown_move = float(row.get("max_drawdown") or 0) - float(previous.get("max_drawdown") or 0)
    from futuresfund.objective import scale_move

    move = scale_move(previous, row, ACCOUNT_SIZE) if len(keys) == 1 else "flat"
    if move == "closer" and len(keys) == 1:
        return (
            f"{label} moved toward the goal. Profit moved {profit_move:.0f}. Drawdown moved {drawdown_move:.0f}. "
            f"The next run moves {name} further in this direction."
        )
    if move == "farther" and len(keys) == 1:
        return (
            f"{label} moved away from the goal. Profit moved {profit_move:.0f}. Drawdown moved {drawdown_move:.0f}. "
            "The next run reverses this direction."
        )
    return (
        f"{label} did not move the result toward the goal. Profit moved {profit_move:.0f}. Drawdown moved {drawdown_move:.0f}. "
        f"The next run leaves {name} and changes another setting."
    )


def _probe_steps(name: str, value, choices: dict | None = None, seeds: dict | None = None, *, dramatic: bool = False):
    from futuresfund.research_rules import daily_restore, exit_step

    restored = daily_restore(name, value, seeds)
    if restored is not None:
        return [restored]
    dollar = exit_step(name)
    if dollar is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        from futuresfund.research_rules import next_rung

        nxt = next_rung(name, value)
        if _is_target_name(name):
            rungs = []
            try:
                current = float(value)
            except (TypeError, ValueError):
                current = None
            if current is not None:
                from futuresfund.research_rules import exit_ladder

                rungs = [rung for rung in exit_ladder(name) if rung < current - 1e-9]
            nxt = rungs[-1] if rungs else None
        if nxt is None:
            return []
        if isinstance(value, int) and not isinstance(value, bool) and float(nxt) >= 1:
            nxt = int(round(float(nxt)))
        else:
            nxt = round(float(nxt), 4)
        return _fresh_steps(name, value, [nxt])
    if isinstance(value, str):
        return [option for option in (choices or {}).get(name) or [] if option != value]
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int) and not isinstance(value, bool):
        scales = (2, 0.5, 4) if dramatic else (1.5, 0.5, 2, 0.75, 1.25)
        raw = [int(value * scale) for scale in scales]
        if not dramatic:
            raw += [value + 1, value - 1, value + 2, value - 2, value + 4, value - 4]
        return _fresh_steps(name, value, raw)
    if isinstance(value, float):
        scales = (2, 0.5, 4) if dramatic else (1.25, 0.8, 1.5, 0.5, 2)
        raw = [round(value * scale, 4) for scale in scales]
        return _fresh_steps(name, value, raw)
    return []


def _fresh_steps(name: str, value, raw) -> list:
    out = []
    for item in raw:
        if isinstance(value, int) and not isinstance(value, bool):
            item = _clamp(name, int(item))
        if item == value or item in out:
            continue
        if isinstance(item, (int, float)) and item <= 0:
            continue
        out.append(item)
    return out


def _reverse_step(name: str, old, new, choices: dict | None = None):
    """If decreasing hurt, the next value increases, and the other way around."""
    if isinstance(old, str) or isinstance(new, str):
        options = [option for option in (choices or {}).get(name) or [] if option not in (old, new)]
        return options[0] if options else None
    if isinstance(old, bool) or not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return None
    delta = new - old
    if delta == 0:
        return None
    flipped = old - delta
    if isinstance(old, int) and not isinstance(old, bool):
        flipped = _clamp(name, int(round(flipped)))
    else:
        flipped = round(float(flipped), 4)
    if flipped == old or flipped == new or flipped <= 0:
        return None
    return flipped


def _continue_step(name: str, old, new, choices: dict | None = None, *, dramatic: bool = False):
    if isinstance(new, str):
        options = (choices or {}).get(name) or []
        if new in options:
            index = options.index(new)
            if index + 1 < len(options):
                return options[index + 1]
        return None
    if isinstance(old, bool) or not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return None
    delta = new - old
    if delta == 0:
        return None
    if dramatic:
        return _scaled(new, 2 if delta > 0 else 0.5)
    nxt = new + delta
    if isinstance(old, int) and not isinstance(old, bool):
        nxt = _clamp(name, int(round(nxt)))
    else:
        nxt = round(float(nxt), 4)
    if nxt == new or nxt <= 0:
        return None
    return nxt


def _creative(anchor: dict) -> list[dict]:
    if anchor.get("added_indicator"):
        return []
    return [
        {"added_indicator": kind, "added_length": length}
        for kind in ("vwma", "ema", "sma", "wma", "rsi")
        for length in (8, 14, 20, 21, 34, 50, 55, 100, 200)
    ]


_INDICATORS = ("vwma", "ema", "sma", "wma", "rsi")
_LENGTHS = (5, 8, 10, 13, 14, 20, 21, 34, 50, 55, 89, 100, 144, 200)


def _force_new(anchor: dict, seen: set[str], choices: dict | None = None) -> dict | None:
    """One more unseen value for one input. The search stops only when nothing new is left."""
    for name in _input_order(anchor, choices):
        value = anchor.get(name)
        if isinstance(value, str):
            options = [option for option in (choices or {}).get(name) or [] if option != value]
        elif isinstance(value, bool):
            options = [not value]
        elif isinstance(value, int) and not isinstance(value, bool):
            options = [_clamp(name, max(1, int(value) + step)) for step in range(1, 121)]
            options += [_clamp(name, max(1, int(value) - step)) for step in range(1, 61)]
        elif isinstance(value, float):
            options = [round(max(0.01, float(value) + step * 0.5), 4) for step in range(1, 121)]
            options += [round(max(0.01, float(value) * scale), 4) for scale in (0.5, 0.75, 1.5, 2, 3)]
        else:
            continue
        for nxt in options:
            if nxt == value or _dead_move(name, value, nxt):
                continue
            plan = dict(anchor)
            plan[name] = nxt
            if _key(plan) not in seen and len(_changed_keys(anchor, plan)) == 1:
                return plan
    return None


def _widen(origin: dict, anchor: dict, seen: set[str], attempt: int, extras: bool = True) -> dict | None:
    """Keep producing a new version until the attempt limit is used."""
    keys = [
        key for key, value in origin.items()
        if key not in FROZEN and key not in {"added_indicator", "added_length"} and _tunable_value(value) and not _taken_out(key)
    ]
    if not keys and not extras:
        return None
    for offset in range(attempt, attempt + 800):
        plan = dict(anchor)
        if keys:
            name = keys[offset % len(keys)]
            value = origin.get(name, anchor.get(name))
            plan[name] = _widened_value(name, value, offset)
            if _dead_move(name, anchor.get(name), plan[name]):
                continue
            if extras and offset % 2 == 0:
                plan["added_indicator"] = _INDICATORS[(offset // 2) % len(_INDICATORS)]
                plan["added_length"] = _LENGTHS[(offset // 2) % len(_LENGTHS)]
        elif extras:
            plan["added_indicator"] = _INDICATORS[offset % len(_INDICATORS)]
            plan["added_length"] = max(2, _LENGTHS[offset % len(_LENGTHS)] + offset // len(_LENGTHS))
        if _key(plan) not in seen:
            return plan
    return None


def _widened_value(name: str, value, offset: int):
    if isinstance(value, bool):
        return (not value) if offset % 2 else value
    if isinstance(value, int) and not isinstance(value, bool):
        step = (offset % 61) - 20
        return _clamp(name, max(1, int(value) + step))
    if isinstance(value, float):
        scale = 0.2 + (offset % 40) * 0.1
        return round(max(0.01, float(value) * scale), 4)
    return value


def _with_indicator(cls, kind: str):
    """Gate new entries with an added average or RSI. Exits stay on the original strategy."""
    key = (cls, kind)
    cached = _FILTERS.get(key)
    if cached is not None:
        return cached

    class Filtered(cls):
        added_indicator = kind

        def init(self):
            super().init()
            from engine.indicators import ta

            length = max(2, int(getattr(self, "added_length", 20) or 20))
            if kind == "vwma":
                self._gate = ta.vwma(self.close, self.volume, length)
            elif kind == "ema":
                self._gate = ta.ema(self.close, length)
            elif kind == "sma":
                self._gate = ta.sma(self.close, length)
            elif kind == "wma":
                self._gate = ta.wma(self.close, length)
            else:
                self._gate = ta.rsi(self.close, length)

        def entry(self, id, direction, qty=None, limit=None, stop=None, comment=""):
            if not self._gate_allows(direction):
                return
            super().entry(id, direction, qty=qty, limit=limit, stop=stop, comment=comment)

        def _gate_allows(self, direction) -> bool:
            import math

            from engine.types import Direction

            value = self._val(self._gate)
            price = self._val(self.close)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return True
            if kind == "rsi":
                if direction == Direction.LONG:
                    return value <= 70
                if direction == Direction.SHORT:
                    return value >= 30
                return True
            if direction == Direction.LONG:
                return price >= value
            if direction == Direction.SHORT:
                return price <= value
            return True

    Filtered.__name__ = f"{cls.__name__}_{kind}"
    _FILTERS[key] = Filtered
    return Filtered
