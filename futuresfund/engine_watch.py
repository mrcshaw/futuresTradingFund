"""An engine that looks busy with no script open is an incident.

The check runs on its own thread. It does not run inside an API request.
A live backtest holds the engine lock and is left alone. Hold with no file
is not a fault once the creation tester is idle.
"""

from __future__ import annotations

import threading
import time

SYSTEMS = "Systems Administrator"
GRACE_SECONDS = 90

_since: dict[int, float] = {}
_open: set[int] = set()
_supply_sent: set[str] = set()
_state_lock = threading.Lock()


def reset_incidents() -> None:
    with _state_lock:
        _since.clear()
        _open.clear()
        _supply_sent.clear()


def gaps(engines, agent_status, *, research_running: bool, hold_new: bool) -> list[dict]:
    """Engines marked working that have no script open."""
    if not research_running:
        return []
    status = agent_status or {}
    found = []
    for row in engines or []:
        script = str(row.get("script") or "").strip() or str(row.get("study") or "").strip()
        if script:
            continue
        engine_id = int(row.get("id") or 0)
        developer = str(row.get("developer") or "")
        engine_marked = bool(row.get("busy") or row.get("studying"))
        agent_marked = status.get(developer) == "working"
        if engine_id == 4 and hold_new and agent_marked and not engine_marked:
            found.append({
                "engine": 4,
                "developer": developer or "Creation tester",
                "fault": False,
                "action": "idle",
                "immediate": True,
                "reason": (
                    "Engine 4 is not processing a file because Hold is on. "
                    "The strategy team is not writing a new script. It was marked working anyway."
                ),
            })
            continue
        if not engine_marked:
            continue
        found.append({
            "engine": engine_id,
            "developer": developer,
            "fault": True,
            "action": "clear",
            "immediate": False,
            "reason": f"Engine {engine_id} is marked working and is not processing a file.",
        })
    return found


def incident_body(issue: dict, resolution: str, note: str) -> str:
    lines = [
        "Systems Administrator",
        "",
        f"Engine {issue['engine']} incident.",
        issue["reason"],
        resolution,
    ]
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def resolve(board, issue: dict, clear) -> str:
    index = int(issue["engine"]) - 1
    cleared = bool(clear(index)) if clear is not None and index >= 0 else False
    developer = issue.get("developer") or ""
    if issue["action"] == "idle":
        if developer:
            board.set_task(developer, "Not developing a new script.", working=False)
        return "Hold is on, so engine 4 is idle. It is not processing a file, and it is not writing one."
    if developer:
        board.set_task(developer, "Waiting for the next script.", working=False)
    if cleared:
        return "The engine had no file open. The working flag was cleared."
    return "A live backtest still holds this engine, so the lock was left in place."


def administrator_note(issue: dict) -> str:
    """The systems administrator thinks through the incident. A model failure stays out of the queue."""
    from futuresfund.llm import client_for

    return client_for(SYSTEMS).ask(
        "Engine incident. "
        f"Engine {issue['engine']}. {issue['reason']} "
        "Reply in two sentences: why this happened, and the one fix. Do not place an order.",
        fresh=True,
    )


def check(
    board,
    *,
    engines,
    agent_status,
    research_running: bool,
    hold_new: bool,
    send,
    ask=None,
    now: float | None = None,
    clear=None,
    lock_held=None,
) -> list[dict]:
    """Alert, correct a stuck engine, and email one incident report."""
    moment = time.time() if now is None else now
    current = []
    for issue in gaps(engines, agent_status, research_running=research_running, hold_new=hold_new):
        index = int(issue["engine"]) - 1
        if lock_held is not None and index >= 0 and lock_held(index):
            continue
        current.append(issue)
    seen = {item["engine"] for item in current}
    sent = []
    with _state_lock:
        for engine_id in list(_since):
            if engine_id not in seen:
                _since.pop(engine_id, None)
                _open.discard(engine_id)
        for engine_id in list(_open):
            if engine_id not in seen:
                _open.discard(engine_id)
        pending = []
        for issue in current:
            engine_id = issue["engine"]
            if not issue.get("immediate"):
                first = _since.setdefault(engine_id, moment)
                if moment - first < GRACE_SECONDS:
                    continue
            if engine_id in _open:
                continue
            _open.add(engine_id)
            pending.append(issue)
    for issue in pending:
        resolution = resolve(board, issue, clear)
        note = ""
        if ask is not None:
            try:
                note = str(ask(issue) or "").strip()
            except Exception:
                note = ""
        body = incident_body(issue, resolution, note)
        developer = issue.get("developer") or "System"
        board.post(
            developer,
            f"Engine {issue['engine']} alert: not processing a file.",
            kind="alert",
            channel=SYSTEMS,
        )
        board.post(SYSTEMS, body, kind="report", channel=SYSTEMS)
        board.set_task(SYSTEMS, f"Engine {issue['engine']} incident report sent.", working=False)
        send(body, subject=f"Engine {issue['engine']} is not processing a file")
        sent.append(issue)
    return sent


def engines_idle(engines) -> bool:
    """True when no engine has a script open and none is marked busy."""
    for row in engines or []:
        if str(row.get("script") or "").strip() or str(row.get("study") or "").strip():
            return False
        if row.get("busy") or row.get("studying"):
            return False
    return True


def supply_check(
    board,
    *,
    research_running: bool,
    hold_new: bool,
    learning_count: int,
    created_names: list[str],
    engines,
    send,
    move,
    release,
) -> list[dict]:
    """When the learning folder is empty and the engines have no file, keep research moving.

    Created scripts are moved into the learning folder. When that folder is empty too,
    Make new strategies is pressed. Each action is emailed once.
    """
    if not research_running or learning_count or not engines_idle(engines):
        with _state_lock:
            _supply_sent.clear()
        return []
    pending = []
    if created_names:
        key = "move"
        action = "move"
        reason = (
            "The learning folder has no scripts and the engines have no file open. "
            f"createdStrategies has {len(created_names)} script(s)."
        )
    elif hold_new:
        key = "make"
        action = "make"
        reason = (
            "The learning folder and createdStrategies are both empty, and the engines have no file open. "
            "Hold is on, so no new script is being written."
        )
    else:
        with _state_lock:
            _supply_sent.clear()
        return []
    with _state_lock:
        if key in _supply_sent:
            return []
        _supply_sent.add(key)
    if action == "move":
        moved = list(move(list(created_names)) or [])
        if not moved:
            with _state_lock:
                _supply_sent.discard(key)
            return []
        resolution = (
            f"Moved {len(moved)} script(s) from createdStrategies into the learning folder: "
            + ", ".join(moved)
            + ". Research stays running."
        )
    else:
        release()
        resolution = "Pressed Make new strategies. The strategy team can write another script. Research stays running."
        moved = []
    issue = {"action": action, "reason": reason, "moved": moved, "resolution": resolution}
    body = "\n".join([
        "Systems Administrator",
        "",
        "Research queue.",
        reason,
        resolution,
    ])
    board.post(SYSTEMS, body, kind="report", channel=SYSTEMS)
    board.set_task(SYSTEMS, resolution, working=False)
    send(body, subject="Research queue was empty")
    pending.append(issue)
    return pending


def engine_issue_asked(message: str) -> bool:
    """A question about an engine that is not running.

    "isn't" and "running" do not have to sit next to each other. "why isn't engine 3 running" counts.
    """
    asked = (message or "").lower()
    if "engine" not in asked:
        return False
    about_state = any(word in asked for word in (
        "running", "working", "processing", "stopped", "stuck", "idle", "down", "fix",
    ))
    if not about_state:
        return False
    return any(word in asked for word in (
        "not ", "isn't", "isnt", "ain't", "aint", "won't", "wont", "why",
        "stopped", "stuck", "idle", "down", "fix",
    ))


def _engine_ids(message: str) -> list[int]:
    import re

    found = [int(item) for item in re.findall(r"engine\s*(\d+)", (message or "").lower())]
    ids: list[int] = []
    for item in found:
        choices = [item] if 1 <= item <= 4 else [int(digit) for digit in str(item)]
        for engine_id in choices:
            if 1 <= engine_id <= 4 and engine_id not in ids:
                ids.append(engine_id)
    return ids or [1, 2, 3, 4]


def _unknown_engines(message: str) -> list[int]:
    import re

    found = [int(item) for item in re.findall(r"engine\s*(\d+)", (message or "").lower())]
    return [item for item in found if item < 1 or item > 4]


def explain_engines(
    message: str,
    *,
    research_running: bool,
    hold_new: bool,
    learning_count: int,
    created_count: int,
    engines,
    threads_alive: dict[int, bool],
    apply: bool,
    start,
    restart,
    clear,
    move,
    release,
    send,
    board,
) -> str:
    """Say why the named engine is not running, and fix it when asked of the systems administrator."""
    rows = {int(row.get("id") or 0): row for row in engines or []}
    lines = []
    actions = []
    folder_done = False
    any_open = not engines_idle(engines)
    unknown = _unknown_engines(message)
    if unknown:
        listed = ", ".join(str(item) for item in unknown)
        lines.append(f"There is no engine {listed}. The engines are 1, 2, 3, and 4.")
    tasks = dict(getattr(board, "agent_tasks", None) or {})
    for engine_id in _engine_ids(message):
        row = rows.get(engine_id) or {}
        script = str(row.get("script") or "").strip() or str(row.get("study") or "").strip()
        if not research_running:
            lines.append(f"Engine {engine_id} is not running because research is stopped.")
            if apply and "started" not in actions:
                start()
                actions.append("started")
                lines.append("I started the researchers.")
            continue
        if not threads_alive.get(engine_id, True):
            lines.append(f"Engine {engine_id} is not running because its worker stopped.")
            if apply:
                restart(engine_id)
                actions.append(f"restarted {engine_id}")
                lines.append(f"I restarted engine {engine_id}.")
            continue
        if script:
            lines.append(f"Engine {engine_id} is running {script}.")
            continue
        if row.get("busy") or row.get("studying"):
            lines.append(f"Engine {engine_id} is marked working and has no file open.")
            if apply:
                clear(engine_id - 1)
                actions.append(f"cleared {engine_id}")
                lines.append(f"I cleared the working flag on engine {engine_id}.")
            continue
        developer = str(row.get("developer") or "")
        task = str(tasks.get(developer) or "").strip()
        if task and any(phrase in task.lower() for phrase in ("finished the learning folder", "not writing", "not developing")):
            lines.append(f"Engine {engine_id} is not running a backtest. {task}")
            continue
        if learning_count == 0 and created_count and any_open:
            lines.append(
                f"Engine {engine_id} has no file. The learning folder is empty and createdStrategies still has scripts. "
                "Another engine still has a file open, so those scripts stay put until that run finishes."
            )
            continue
        if learning_count == 0 and created_count and not folder_done:
            lines.append(
                f"Engine {engine_id} has no file because the learning folder is empty. "
                f"createdStrategies has {created_count} script(s)."
            )
            if apply:
                moved = list(move() or [])
                folder_done = True
                if moved:
                    actions.append("moved")
                    lines.append(
                        f"I moved {len(moved)} script(s) from createdStrategies into the learning folder: "
                        + ", ".join(moved)
                        + "."
                    )
            continue
        if learning_count == 0 and not created_count and hold_new and not folder_done:
            lines.append(
                f"Engine {engine_id} has no file because the learning folder and createdStrategies are empty, and Hold is on."
            )
            if apply:
                release()
                folder_done = True
                actions.append("make")
                lines.append("I pressed Make new strategies so the strategy team writes another script.")
            continue
        lines.append(
            f"Engine {engine_id} is idle between scripts. Research is running"
            + (" and Hold is on." if hold_new and engine_id == 4 else ".")
        )
    if not lines:
        lines.append("No engine number was in the question.")
    text = "\n".join(lines)
    if apply and actions:
        body = "Systems Administrator\n\n" + text
        board.post(SYSTEMS, body, kind="report", channel=SYSTEMS)
        board.set_task(SYSTEMS, lines[-1], working=False)
        send(body, subject="Engine status and the fix")
    return text


def answer_engines(board, message: str, *, apply: bool) -> str | None:
    """The systems administrator answers an engine question from the live desk and can fix it."""
    if not engine_issue_asked(message):
        return None
    from futuresfund.backtrader_engine import clear_idle_engine, engine_status
    from futuresfund.learn import (
        CREATED,
        LEARNING,
        _engine_threads,
        _strategy_files,
        begin_research,
        release_chart_research,
        restart_research_engine,
        restock_learning,
    )
    from futuresfund.mailer import send_report

    engines = engine_status()
    if _engine_threads:
        threads = {
            engine_id: (_engine_threads.get(engine_id) is not None and _engine_threads[engine_id].is_alive())
            for engine_id in (1, 2, 3, 4)
        }
    else:
        threads = {engine_id: True for engine_id in (1, 2, 3, 4)}
    view = board.watch_view()
    return explain_engines(
        message,
        research_running=view.get("research") == "running",
        hold_new=bool(view.get("hold_new")),
        learning_count=len(_strategy_files(LEARNING)),
        created_count=len(_strategy_files(CREATED)),
        engines=engines,
        threads_alive=threads,
        apply=apply,
        start=lambda: begin_research(board),
        restart=lambda engine_id: restart_research_engine(engine_id, board),
        clear=clear_idle_engine,
        move=lambda: restock_learning(),
        release=lambda: release_chart_research(board),
        send=send_report,
        board=board,
    )


def _engine_locked(index: int) -> bool:
    from futuresfund.backtrader_engine import _ENGINE_LOCKS, _ENGINE_SLOTS

    if index < 0 or index >= _ENGINE_SLOTS:
        return False
    lock = _ENGINE_LOCKS[index]
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _supply_once(board, view: dict) -> list[dict]:
    from futuresfund.backtrader_engine import engine_status
    from futuresfund.learn import CREATED, LEARNING, _strategy_files, release_chart_research, restock_learning
    from futuresfund.mailer import send_report

    created = [path.name for path in _strategy_files(CREATED)]
    return supply_check(
        board,
        research_running=view.get("research") == "running",
        hold_new=bool(view.get("hold_new")),
        learning_count=len(_strategy_files(LEARNING)),
        created_names=created,
        engines=engine_status(),
        send=send_report,
        move=lambda names: restock_learning(),
        release=lambda: release_chart_research(board),
    )


def _once(board) -> list[dict]:
    from futuresfund.backtrader_engine import clear_idle_engine, engine_status
    from futuresfund.mailer import send_report

    view = board.watch_view()
    try:
        _supply_once(board, view)
    except Exception:
        pass
    return check(
        board,
        engines=engine_status(),
        agent_status=view.get("agent_status") or {},
        research_running=view.get("research") == "running",
        hold_new=bool(view.get("hold_new")),
        send=send_report,
        ask=administrator_note,
        clear=clear_idle_engine,
        lock_held=_engine_locked,
    )


def start_engine_watch(board) -> None:
    """Check the engines off the request path. The first look waits so startup stays fast."""

    def _run() -> None:
        while True:
            time.sleep(30)
            try:
                _once(board)
            except Exception:
                continue

    threading.Thread(target=_run, daemon=True, name="engine-watch").start()
