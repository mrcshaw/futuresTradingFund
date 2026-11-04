"""The research desk. One script, one meeting, one engine, then the stage gate."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from research_department.backtest import PineTSError, run_window
from research_department.department import SETTING_KEYS, Department
from research_department.inputs import script_inputs, strategy_title
from research_department.loader import LEARNING, instrument_of, learning_files, place_file
from research_department.measure import summarize
from research_department.meeting import (
    Proposal,
    apply_change,
    board_marks,
    change_kept,
    hold_meeting,
    next_allowed_change,
    priced_source,
    proposal_fault,
    result_note,
    revert_note,
    scales_price,
    tried_key,
)
from research_department.model import (
    ask,
    formatted_text,
    manager_prompt,
    as_speech,
    parse_choice,
    parse_proposal,
    researcher_prompt,
    visible_text,
)
from research_department.phone import phone_url
from research_department.stages import (
    STAGE_DAYS,
    Day,
    StageBook,
    profit_goal_for,
    required_trades,
    reward_note,
    reward_reading,
)
from research_department.worker import Worker


MARKETS = ("ES", "NQ", "MNQ")

SAMPLE = """//@version=5
strategy("Research sample", overlay=true)
if bar_index == 2
    strategy.entry("Long", strategy.long)
if strategy.position_size > 0 and bar_index > 2
    strategy.close("Long")
"""

_STAGE_NUMBER = {7: 1, 30: 2, 90: 3, 365: 4}
_MEASURE = Proposal("script", "unchanged", "", True, True, "The script as written, before a change.")


class ResearchDesk(Department):
    """The routes, plus the work the scripted department leaves outside."""

    def __init__(self, folder: Path | None = None, library_path: Path | None = None) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.shown: str | None = None
        self.folder = Path(folder) if folder is not None else LEARNING
        self.library_path = Path(library_path) if library_path is not None else None
        self.strategies: list[dict] = []
        self._next_strategy = 1
        self.url = ""
        self.ask = ask
        self.backtest = run_window
        self.mail = None
        self.engine_runs: dict[int, list] = {1: [], 2: [], 3: []}
        self.successes: dict[int, list] = {7: [], 30: [], 90: [], 365: []}
        self.worker_running = False
        self.current_file = ""
        self.accepted: list[dict] = []
        self.loader_calls: list[tuple[str, str]] = []
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._file_index = 0
        self._latest: dict[str, dict] = {}
        self.board: list[dict] = []
        self.marks = None
        self.tried: set[tuple] = set()
        self.measured = False
        self.before_source = None
        self._progress: dict | None = None
        self.results: list[dict] = []
        self._load_library()
        self._load_settings()
        self._progress = self._read_progress()
        self._load_catalog()
        self._import_saved_runs()
        self._align_file_index()
        self.reward = self._reward_from(self._progress)

    def bind(self, url: str) -> None:
        self.url = url

    def start_worker(self) -> tuple[int, dict]:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                self.worker_running = True
                return 200, {"running": True}
            if not self.url:
                return 409, {"reason": "The department has no address yet."}
            self._worker_stop.clear()
            self.worker_running = True
            self._worker_thread = threading.Thread(target=self._load_forever, name="learning-loader", daemon=True)
            self._worker_thread.start()
            return 200, {"running": True}

    def stop_worker(self) -> tuple[int, dict]:
        self._worker_stop.set()
        with self._lock:
            self.worker_running = False
        return 200, {"running": False}

    def delete_strategy(self, strategy_id: str) -> tuple[int, dict]:
        with self._lock:
            kept = [item for item in self.strategies if item["id"] != strategy_id]
            if len(kept) == len(self.strategies):
                return 404, {"reason": "That strategy is not in the table."}
            self.strategies = kept
            self._save_library()
            return 200, {"removed": strategy_id}

    def script_text(self, file_name: str, change: str = "") -> tuple[int, dict]:
        path = _script_file(self.folder, file_name)
        if path is None:
            return 404, {"reason": "That script is not in the library folders."}
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            return 404, {"reason": "That script could not be read."}
        return 200, {"file": path.name, "source": _with_change(source, change)}

    def delete_result(self, result_id: str) -> tuple[int, dict]:
        with self._lock:
            kept = [item for item in self.results if item.get("id") != result_id]
            if len(kept) == len(self.results):
                return 404, {"reason": "That result is not in the library."}
            self.results = kept
            self._save_catalog()
            return 200, {"removed": result_id}

    def post_script(self, source: str, instrument: str) -> tuple[int, dict]:
        if not _tradable(instrument):
            return 400, {"refused": True, "reason": "The department runs ES, NQ, and MNQ."}
        with self._lock:
            status, body = super().post_script(source, instrument)
            if status != 200:
                return status, body
            job = self.jobs[body["job_id"]]
            if not job.states:
                job.states.append("meeting")
            job.feed.setdefault("proposals", [])
            job.feed.setdefault("manager", "")
            job.feed.setdefault("lines", [])
            job.feed.setdefault("rows", [])
            job.feed.setdefault("activity", [])
            self.shown = job.job_id
            job_id = job.job_id
        threading.Thread(target=self._execute, args=(job_id,), name=f"research-{job_id}", daemon=True).start()
        return status, body

    def get_job(self, job_id: str) -> tuple[int, dict]:
        with self._lock:
            return super().get_job(job_id)

    def get_feed(self, job_id: str) -> tuple[int, dict]:
        with self._lock:
            return super().get_feed(job_id)

    def get_result(self, job_id: str) -> tuple[int, dict]:
        with self._lock:
            return super().get_result(job_id)

    def get_engines(self) -> tuple[int, dict]:
        with self._lock:
            return super().get_engines()

    def post_engine(self, timeframe: str) -> tuple[int, dict]:
        with self._lock:
            return super().post_engine(timeframe)

    def delete_engine(self, engine_id: int) -> tuple[int, dict]:
        with self._lock:
            return super().delete_engine(engine_id)

    def get_settings(self) -> tuple[int, dict]:
        with self._lock:
            return super().get_settings()

    def put_settings(self, values: dict) -> tuple[int, dict]:
        with self._lock:
            status, saved = super().put_settings(values)
            self.reward = reward_reading(
                self.reward.get("previous"),
                self.reward.get("win_percentage"),
                self.settings["success_rate"],
                self.reward.get("profit"),
                self.reward.get("profit_previous"),
                days=int(self.reward.get("days") or 7),
                daily=float(self.settings["daily_target"]),
            )
        self._save_settings(saved)
        return status, saved

    def has_backtest(self) -> bool:
        """A shutdown left a backtest in progress."""
        progress = self._progress or {}
        return bool(progress.get("order") and progress.get("file"))

    def get_desk(self) -> tuple[int, dict]:
        with self._lock:
            job = self.jobs.get(self.shown or "")
            snapshot = None
            if job is not None:
                snapshot = {
                    "job_id": job.job_id,
                    "state": job.states[-1] if job.states else "meeting",
                    "instrument": job.instrument,
                    "feed": {
                        "proposals": list(job.feed.get("proposals") or []),
                        "manager": job.feed.get("manager") or "",
                        "lines": list(job.feed.get("lines") or []),
                        "rows": list(job.feed.get("rows") or []),
                        "activity": list(job.feed.get("activity") or []),
                        "chats": {
                            name: [dict(message) for message in messages]
                            for name, messages in (job.feed.get("chats") or {}).items()
                        },
                        "line": job.feed.get("line") or "",
                    },
                }
            return 200, {
                "engines": self.engines.cards(),
                "runs": {str(engine_id): [dict(run) for run in rows] for engine_id, rows in self.engine_runs.items()},
                "successes": {str(days): [dict(run) for run in rows] for days, rows in self.successes.items()},
                "settings": dict(self.settings),
                "job": snapshot,
                "phone": phone_url(),
                "markets": list(MARKETS),
                "strategies": [dict(item) for item in self.strategies],
                "results": _ranked(self.results),
                "worker": {
                    "running": self.worker_running,
                    "folder": str(self.folder),
                    "file": self.current_file,
                },
                "reward": dict(self.reward),
            }

    def run_sample(self) -> dict:
        """A few ES sessions and one 5-minute engine. The ordinary tests do not call this."""
        engine = self.engines.engines[1]
        with self._lock:
            self.engines.start(engine.id, "Research sample")
        try:
            days = run_window(SAMPLE, "ES", "5m", 3, sample=True)
        except Exception:
            with self._lock:
                self.engines.did_not_finish(engine.id)
            raise
        with self._lock:
            self.engines.complete(engine.id, summarize(days))
            return engine.card()

    def _execute(self, job_id: str) -> None:
        try:
            self._research(job_id)
        except Exception as exc:
            self._note(job_id, str(exc))
            self._stop(job_id)

    def _research(self, job_id: str) -> None:
        with self._lock:
            job = self.jobs[job_id]
            source = job.source
            instrument = job.instrument
            rate = float(self.settings["success_rate"])
            attempts = int(self.settings["attempts"])
            daily = float(self.settings["daily_target"])
        book = StageBook(success_rate=rate, attempts=attempts, daily_target=daily)
        closed: set[tuple[str, str]] = set()
        source = _readable_source(source)
        title = strategy_title(source)
        if not _can_run(source):
            self._note(job_id, "This script is set aside. PineTS cannot read its strategy title.")
            self._clear_progress()
            self._stop(job_id, book)
            return
        resume_order = None
        resume_finished: list[dict] = []
        saved = self._saved_attempt()
        if saved and saved.get("file") == self.current_file:
            self.reward = self._reward_from(saved)
        else:
            self.reward = reward_reading(None, None, rate)
        self._load_search(saved if saved and saved.get("file") == self.current_file else None)
        if saved and saved.get("file") == self.current_file:
            source = saved.get("source") or source
            title = strategy_title(source) or title
            book.stage_index = min(int(saved.get("stage_index") or 0), len(STAGE_DAYS) - 1)
            book.attempt = max(int(saved.get("attempt") or 1), 1)
            closed = {tuple(pair) for pair in saved.get("closed") or [] if len(pair) == 2}
            if saved.get("order"):
                resume_order = Proposal(**saved["order"])
                resume_finished = list(saved.get("finished") or [])
                self._note(job_id, "Continuing the backtests that finished before the shutdown.")
        while not book.passed_all and not book.stopped:
            if self._worker_stop.is_set():
                self._note(job_id, "Stopped.")
                self._release_engine()
                self._stop(job_id, book)
                return
            before = None
            if resume_order is not None:
                order = resume_order
                prior = resume_finished
                resume_order = None
                resume_finished = []
                before = None if order.variable == "script" else self.before_source
                self._mark(job_id, "running")
            elif not self.measured:
                order = _MEASURE
                prior = []
                self.before_source = None
                self.measured = True
                self._note(job_id, "Measuring the script before any change.")
                self._mark(job_id, "running")
            else:
                self._mark(job_id, "meeting")
                found = script_inputs(source)
                if not found:
                    self._note(job_id, "This script has no numeric input, so there is no change to decide.")
                    self._clear_progress()
                    self._stop(job_id, book)
                    return
                if not _open_inputs(found, closed):
                    self._note(job_id, "Every input direction is closed for this stage.")
                    self._clear_progress()
                    self._stop(job_id, book)
                    return
                order, proposals = self._meet(title, found, closed, self._latest.get(job_id))
                if order is None:
                    self._note(job_id, "Stopped.")
                    self._release_engine()
                    self._stop(job_id, book)
                    return
                try:
                    before = source
                    self.before_source = source
                    source = apply_change(source, order.variable, order.value)
                except ValueError as exc:
                    self.before_source = None
                    self._note(job_id, str(exc))
                    self._clear_progress()
                    self._stop(job_id, book)
                    return
                line = f"{order.variable} {order.value}. {' '.join(order.justification.split())}"
                self._publish(job_id, proposals, line)
                self._mark(job_id, "running")
                prior = []
            if self._worker_stop.is_set():
                self._note(job_id, "Stopped.")
                self._stop(job_id, book)
                return
            previous_reward = dict(self.reward)
            previous_marks = dict(self.marks) if isinstance(self.marks, dict) else None
            previous_board = list(self.board)
            previous_latest = dict(self._latest.get(job_id) or {})
            self._save_attempt(source, title, book, closed, order, prior)
            outcome = self._run_engines(
                job_id, source, instrument, title, book, order, rate, prior=prior, closed=closed
            )
            if outcome == "retry":
                if before is not None:
                    source = before
                    self.before_source = None
                    self._note(
                        job_id,
                        f"Reverted {order.variable} {order.value}. The backtests did not finish, so the change is not kept.",
                    )
                else:
                    self.measured = False
                self._save_attempt(source, title, book, closed, None, [])
                continue
            if outcome == "stopped-now":
                return
            if self._worker_stop.is_set():
                self._note(job_id, "Stopped.")
                self._stop(job_id, book)
                return
            if outcome == "passed":
                self.before_source = None
                self.measured = False
                self._latest.pop(job_id, None)
                closed = set()
                passed_days = STAGE_DAYS[book.stage_index if book.passed_all else book.stage_index - 1]
                row = asdict(book.rows[passed_days][-1])
                row["table"] = f"{passed_days} days"
                row["file"] = self.current_file
                self.deliver(row, job_id)
                self._save_attempt(source, title, book, closed, None, [])
                if book.passed_all:
                    with self._lock:
                        job = self.jobs[job_id]
                        job.result_script = source
                        job.states.append("passed_all")
                        kept = self._keep(job, source, book)
                    self._clear_progress()
                    self.deliver(kept, job_id)
                    return
                self._mark(job_id, f"passed_stage_{_STAGE_NUMBER[passed_days]}")
                self._note(job_id, f"Stage {book.stage_index} passed. The next stage starts at attempt 1.")
                continue
            if outcome == "stopped":
                self._clear_progress()
                self._stop(job_id, book)
                return
            if before is not None and not change_kept(previous_marks, self.marks):
                rejected = self.marks
                source = before
                self.before_source = None
                self.reward = previous_reward
                self.marks = previous_marks
                self.board = previous_board
                if previous_latest:
                    self._latest[job_id] = previous_latest
                else:
                    self._latest.pop(job_id, None)
                self.tried.add(tried_key(order.variable, order.value))
                self._note(job_id, revert_note(order, previous_marks, rejected))
                self._save_attempt(source, title, book, closed, None, [])
            else:
                self.before_source = None
            self.measured = True
            self._note(job_id, f"Attempt {book.attempt - 1} did not pass on ES, NQ, and MNQ.")

    def _meet(self, title, found, closed, last=None):
        """Stay in this meeting until three complete proposals exist and the manager picks one.

        Stop is the only way out before that choice. A missing justification sends that
        researcher back and leaves the attempt unspent. Raising the volume delta while a
        session had no trade does the same.
        """
        replies = ["", "", ""]
        speech = ["", "", ""]
        faults = ["", "", ""]
        heard: list[tuple[str, ...] | None] = [None, None, None]
        proposals = [parse_proposal("", found) for _ in range(3)]
        days = int(self.reward.get("days") or (last or {}).get("days") or 7)
        daily = float(self.settings["daily_target"])
        goal_win = float(self.settings["success_rate"])
        loss_limit = float(self.settings["daily_loss"])
        note = "\n".join(
            part
            for part in (
                trade_note(found, last),
                result_note(self.board, self.marks, days, daily, goal_win),
                reward_note(self.reward),
            )
            if part
        )
        self._show(proposals, replies, note or "The meeting is open. The researchers are working.")
        opened = time.monotonic()
        unanswered = 0

        def _use_open_step(reason: str):
            chosen = next_allowed_change(
                found,
                last,
                self.tried,
                goal_win,
                profit_goal_for(days, daily),
                loss_limit,
                _preferred_input(found, last, loss_limit, goal_win),
            )
            if chosen is None:
                self._show(proposals, replies, "No one-step change is still open.")
                return None
            self._show(proposals, replies, f"{reason} Using {chosen.variable} {chosen.value}.")
            return chosen

        while not self._worker_stop.is_set():
            if time.monotonic() - opened >= 45 * 60:
                chosen = _use_open_step("The meeting has been open for 45 minutes.")
                if chosen is None:
                    return None, proposals
                return chosen, proposals
            for seat in range(3):
                if self._worker_stop.is_set():
                    return None, proposals
                if seat and any(not speech[index] for index in range(seat)):
                    break
                key = tuple(speech[index] for index in range(seat))
                if _ready(proposals[seat]) and not faults[seat] and heard[seat] == key:
                    continue
                who = f"Researcher {seat + 1}"
                prompt = researcher_prompt(
                    title,
                    found,
                    closed,
                    seat,
                    note,
                    _preferred_input(found, last, loss_limit, goal_win),
                    speech[:seat],
                    self.tried,
                )
                if faults[seat]:
                    prompt += (
                        "\nThat value is not allowed. "
                        + faults[seat]
                        + " Propose a different input or a different value inside one step.\nLast answer:\n"
                        + visible_text(replies[seat])[:1200]
                    )
                elif replies[seat] and not _ready(proposals[seat]):
                    prompt += (
                        "\nYour last answer is still open. It needs "
                        + _gap(proposals[seat])
                        + ". Answer the reply above again with the variable, the new value, and both reasons.\nLast answer:\n"
                        + visible_text(replies[seat])[:1200]
                    )
                text = self._ask(prompt, who)
                replies[seat] = text
                proposals[seat] = parse_proposal(text, found)
                faults[seat] = proposal_fault(
                    proposals[seat],
                    found,
                    last,
                    self.tried,
                    goal_win,
                    profit_goal_for(days, daily),
                    loss_limit,
                )
                speech[seat] = as_speech(text, found)
                heard[seat] = key
                for later in range(seat + 1, 3):
                    heard[later] = None
                    speech[later] = ""
                    faults[later] = ""
                waiting = _waiting(proposals) or next((item for item in faults if item), "")
                self._show(proposals, replies, waiting or note)
                if "could not answer" in text:
                    unanswered += 1
                    if unanswered >= 2:
                        chosen = _use_open_step("The reply took longer than 10 minutes.")
                        if chosen is None:
                            return None, proposals
                        return chosen, proposals
                    self._worker_stop.wait(2)
            if any(not _ready(item) or fault for item, fault in zip(proposals, faults)):
                continue
            if self._worker_stop.is_set():
                return None, proposals
            self._show(proposals, replies, "The manager is choosing one of the three.")
            text = self._ask(manager_prompt(proposals, note, speech, self.tried), "Manager")
            choice = parse_choice(text)
            result = hold_meeting(proposals, closed, choice)
            if result.order is not None:
                fault = proposal_fault(
                    result.order, found, last, self.tried, goal_win, profit_goal_for(days, daily), loss_limit
                )
                if fault and isinstance(choice, int) and 0 <= choice < 3:
                    replies[choice] = ""
                    proposals[choice] = parse_proposal("", found)
                    speech[choice] = ""
                    heard[choice] = None
                    faults[choice] = fault
                    self._show(proposals, replies, fault)
                    continue
            if result.order is not None and raises_delta_while_a_session_is_empty(result.order, found, last):
                if isinstance(choice, int) and 0 <= choice < 3:
                    replies[choice] = ""
                    proposals[choice] = parse_proposal("", found)
                    speech[choice] = ""
                    heard[choice] = None
                self._show(
                    proposals,
                    replies,
                    "A session had no trade. Lower the volume delta for more trades. Raising it takes fewer trades.",
                )
                continue
            if result.order is not None:
                self._show(proposals, replies, f"The manager picked {result.order.variable} {result.order.value}.")
                return result.order, proposals
            if result.refused == "closed" and isinstance(choice, int) and 0 <= choice < 3:
                replies[choice] = ""
                proposals[choice] = parse_proposal("", found)
                speech[choice] = ""
                heard[choice] = None
                self._show(proposals, replies, "That direction is already closed. That researcher is proposing a different value.")
                continue
            self._show(proposals, replies, "The manager has to pick one of the three. The meeting stays open.")
        return None, proposals

    def _run_engines(self, job_id, source, instrument, title, book, order, rate, prior=None, closed=None):
        """Backtest the chosen script on the 2-minute, 5-minute, and 1-minute charts for ES, NQ, and MNQ."""
        window = book.days
        attempt_no = book.attempt
        change = f"{order.variable} {order.value}"
        finished = []
        records = [dict(item) for item in (prior or [])]
        done = {(int(item["engine_id"]), item["instrument"]): item for item in records}
        for engine in list(self.engines.engines):
            if not engine.original:
                continue
            for market in MARKETS:
                if self._worker_stop.is_set():
                    self._note(job_id, "Stopped.")
                    self._release_engine()
                    self._stop(job_id, book)
                    return "stopped-now"
                saved = done.get((engine.id, market))
                if saved is not None:
                    days = _days_from(saved.get("days") or [])
                    summary = dict(saved.get("summary") or {})
                    summary["instrument"] = market
                    summary["profit"] = _profit_of(days, summary.get("profit"))
                    run = dict(saved.get("run") or {})
                    run["profit"] = summary["profit"]
                    self._store_run(engine.id, run)
                    if _accepted_run(run, window, rate, float(self.settings["daily_target"])):
                        self._store_success(window, run)
                    with self._lock:
                        self.engines.complete(engine.id, summary)
                    self._note(job_id, f"Kept the finished {market} {engine.timeframe} backtest.")
                    finished.append((days, summary, engine, market))
                    continue
                claimed = self._claim_engine(engine.id, title, market)
                if claimed is None:
                    self._note(job_id, f"The {engine.timeframe} engine is not free.")
                    continue
                self._working(
                    f"{claimed.timeframe} engine {claimed.id}",
                    f"Working on {title} for {market} on the {claimed.timeframe} chart.",
                )
                try:
                    days = self.backtest(priced_source(source, market), market, claimed.timeframe, window)
                except PineTSError as exc:
                    with self._lock:
                        self.engines.did_not_finish(claimed.id)
                    self._note(job_id, f"{title} on {market} is set aside on the {claimed.timeframe} engine. {exc}")
                    if "10 minutes" not in str(exc):
                        self._stop(job_id, book)
                        return "stopped-now"
                    continue
                summary = summarize(days)
                summary["instrument"] = market
                run = {
                    "engine_id": claimed.id,
                    "timeframe": claimed.timeframe,
                    "script": title,
                    "instrument": market,
                    "change": change,
                    "days": window,
                    "success_rate": summary["success_rate"],
                    "win_percentage": summary["win_percentage"],
                    "worst_day": summary["worst_day"],
                    "trade_count": summary["trade_count"],
                    "quiet_sessions": summary["quiet_sessions"],
                    "profit": summary["profit"],
                    "attempt": attempt_no,
                }
                with self._lock:
                    self.engines.complete(claimed.id, summary)
                    self.jobs[job_id].feed["rows"] = _rows(book)
                self._store_run(claimed.id, run)
                if _accepted_run(summary, window, rate, float(self.settings["daily_target"])):
                    self._store_success(window, run)
                self._working(
                    f"{claimed.timeframe} engine {claimed.id}",
                    (
                        f"Finished {title} on {market} {claimed.timeframe}. Success {summary['success_rate']:.0%}. "
                        f"Win {summary['win_percentage']:.0%}. Profit {summary['profit']:.0f}. "
                        f"Worst day {summary['worst_day']}. Trades {summary['trade_count']}. "
                        f"Sessions with no trade {summary['quiet_sessions']}."
                    ),
                    commit=True,
                )
                records.append({
                    "engine_id": claimed.id,
                    "instrument": market,
                    "days": _day_rows(days),
                    "summary": summary,
                    "run": run,
                })
                self._save_attempt(source, title, book, closed or set(), order, records)
                finished.append((days, summary, claimed, market))
        if self._worker_stop.is_set():
            self._note(job_id, "Stopped.")
            self._release_engine()
            self._stop(job_id, book)
            return "stopped-now"
        if not finished:
            if book.attempt >= book.attempts:
                book.stopped = True
                self._stop(job_id, book)
                return "stopped-now"
            book.attempt += 1
            self._note(job_id, f"Attempt {attempt_no} did not finish on ES, NQ, and MNQ.")
            return "retry"
        self.board = [_board_row(summary, engine, market) for _days, summary, engine, market in finished]
        self.marks = board_marks(self.board)
        best_days, best_summary, best_engine, best_market = choose_result(
            finished, window, rate, float(self.settings["daily_target"])
        )
        self._latest[job_id] = {
            "success_rate": best_summary["success_rate"],
            "win_percentage": best_summary["win_percentage"],
            "worst_day": best_summary["worst_day"],
            "trade_count": best_summary["trade_count"],
            "quiet_sessions": best_summary.get("quiet_sessions", 0),
            "profit": best_summary.get("profit", 0),
            "days": window,
        }
        self._record_reward(best_summary, window)
        self._note(job_id, reward_note(self.reward))
        if best_summary.get("quiet_sessions"):
            self._note(
                job_id,
                f"{best_summary['quiet_sessions']} of {window} sessions had no trade. Lower the volume delta for more trades.",
            )
        return book.submit(
            best_days,
            script=title,
            instrument=best_market,
            timeframe=best_engine.timeframe,
            change=change,
            justification=order.justification,
        )

    def _store_run(self, engine_id: int, run: dict) -> None:
        with self._lock:
            rows = self.engine_runs.setdefault(engine_id, [])
            rows.insert(0, dict(run))
            del rows[20:]
            self._remember_result(run)

    def _store_success(self, days: int, run: dict) -> None:
        with self._lock:
            rows = self.successes.setdefault(int(days), [])
            rows.insert(0, dict(run))
            del rows[40:]

    def _claim_engine(self, engine_id: int, script: str, instrument: str = ""):
        with self._lock:
            for engine in self.engines.engines:
                if engine.id == engine_id and engine.free:
                    self.engines.start(engine.id, script, instrument)
                    return engine
        return None

    def _claim(self, script: str):
        with self._lock:
            for engine in self.engines.engines:
                if engine.free:
                    self.engines.start(engine.id, script)
                    return engine
        return None

    def _mark(self, job_id: str, state: str) -> None:
        with self._lock:
            job = self.jobs[job_id]
            if not job.states or job.states[-1] != state:
                job.states.append(state)

    def _note(self, job_id: str, text: str) -> None:
        with self._lock:
            self.jobs[job_id].feed.setdefault("lines", []).append(text)

    def _publish(self, job_id: str, proposals, manager: str) -> None:
        rows = []
        for seat, item in enumerate(proposals):
            row = _proposal(item)
            row["who"] = f"Researcher {seat + 1}"
            rows.append(row)
        with self._lock:
            feed = self.jobs[job_id].feed
            feed["proposals"] = rows
            feed["manager"] = manager
            feed["line"] = manager

    def _publish_pending(self, proposals, reason: str) -> None:
        if self.shown is None:
            return
        with self._lock:
            feed = self.jobs[self.shown].feed
            feed["proposals"] = [_proposal(item) for item in proposals]
            feed["manager"] = reason

    def _ask(self, prompt: str, who: str = "Researcher") -> str:
        if self._worker_stop.is_set():
            return ""
        self._working(who, "Thinking.")
        last = {"at": 0.0, "n": 0}

        def listener(kind: str, text: str) -> None:
            now = time.monotonic()
            if now - last["at"] < 0.4 and len(text) - last["n"] < 80:
                return
            last["at"] = now
            last["n"] = len(text)
            shown = text[-8000:] if kind == "thinking" and len(text) > 8000 else text
            self._working(who, shown, stream_kind="thinking" if kind == "thinking" else "content")

        try:
            text = self.ask(prompt, listener)
        except TypeError:
            text = self.ask(prompt)
        self._working(who, formatted_text(text) or "No reply.", commit=True)
        return text

    def _working(self, who: str, text: str, *, commit: bool = False, stream_kind: str = "thinking") -> None:
        if self.shown is None:
            return
        body = formatted_text(str(text)) or "No reply."
        line = " ".join(body.split())
        with self._lock:
            feed = self.jobs[self.shown].feed
            thread = feed.setdefault("chats", {}).setdefault(who, [])
            if commit:
                if thread and thread[-1].get("draft") and thread[-1].get("kind") != "thinking":
                    thread[-1] = {"text": body, "draft": False, "kind": "reply"}
                else:
                    if thread and thread[-1].get("draft"):
                        draft = " ".join(str(thread[-1].get("text") or "").split())
                        if draft in {"Thinking.", "Thinking"}:
                            thread.pop()
                        else:
                            thread[-1]["draft"] = False
                            thread[-1]["kind"] = "thinking"
                    thread.append({"text": body, "draft": False, "kind": "reply"})
            else:
                kind = "thinking" if stream_kind == "thinking" else "content"
                message = {"text": body, "draft": True, "kind": kind}
                if thread and thread[-1].get("draft") and thread[-1].get("kind") == kind:
                    thread[-1] = message
                else:
                    if thread and thread[-1].get("draft"):
                        thread[-1]["draft"] = False
                    thread.append(message)
            del thread[:-200]
            thought = ""
            for message in reversed(thread):
                if message.get("kind") != "thinking":
                    continue
                thought = " ".join(str(message.get("text") or "").split())
                if thought and thought not in {"Thinking.", "Thinking"}:
                    break
                thought = ""
            entry = {"who": who, "text": thought or line}
            activity = feed.setdefault("activity", [])
            for index, item in enumerate(activity):
                if item.get("who") == who:
                    activity[index] = entry
                    break
            else:
                activity.append(entry)
            feed["line"] = entry["text"]

    def _show(self, proposals, replies, manager: str) -> None:
        if self.shown is None:
            return
        rows = []
        for seat, proposal in enumerate(proposals):
            spoken = visible_text(replies[seat]) if seat < len(replies) else ""
            if len(spoken) > 700:
                spoken = spoken[:700].rstrip() + "..."
            row = _proposal(proposal)
            row["who"] = f"Researcher {seat + 1}"
            row["spoken"] = spoken or proposal.justification
            rows.append(row)
        with self._lock:
            feed = self.jobs[self.shown].feed
            feed["proposals"] = rows
            feed["manager"] = manager
            feed["line"] = manager

    def deliver(self, strategy: dict, job_id: str | None = None) -> dict | None:
        """The research manager reports a strategy that was added to the table."""
        mail = self.mail
        if mail is None or not strategy:
            return None
        try:
            result = mail.send_success(strategy)
        except Exception as exc:
            result = {"sent": False, "to": "", "reason": str(exc)}
        if job_id:
            self._note(job_id, f"Research manager email: {result.get('reason') or 'Not sent.'}")
        return result

    def _release_engine(self) -> None:
        with self._lock:
            for engine in self.engines.engines:
                if not engine.free:
                    engine.free = True
                    engine.script = ""
                    engine.instrument = ""
                    engine.result = None
                    engine.status = ""

    def _keep(self, job, source: str, book: StageBook) -> dict:
        rows = _rows(book)
        final = dict(rows[-1]) if rows else {}
        final["id"] = f"strategy-{self._next_strategy}"
        self._next_strategy += 1
        final["source"] = source
        final["file"] = self.current_file
        final["instrument"] = ", ".join(MARKETS)
        final["table"] = "scripts"
        self.strategies.append(final)
        self._save_library()
        return final

    def _load_forever(self) -> None:
        worker = Worker(self.url)
        self.loader_calls = worker.calls
        try:
            while not self._worker_stop.is_set():
                path = self._next_file()
                if path is None:
                    self._worker_stop.wait(0.3)
                    continue
                market = instrument_of(path.name)
                if not _tradable(market):
                    continue
                with self._lock:
                    self.current_file = path.name
                accepted = worker.submit(str(path), market)
                if self._worker_stop.is_set():
                    break
                if accepted.get("refused") or worker.finished:
                    continue
                seen = None
                while not worker.finished and not self._worker_stop.is_set():
                    try:
                        body = worker.poll()
                    except Exception:
                        self._worker_stop.set()
                        break
                    state = body.get("state")
                    if state == seen:
                        self._worker_stop.wait(0.05)
                    seen = state
                if self._worker_stop.is_set() and not worker.finished:
                    self._drain(worker)
                if worker.finished:
                    passed = bool(worker.accepted_script)
                    reason = worker.best.get("reason") if isinstance(worker.best, dict) else ""
                    if path.is_file() and (passed or reason == "attempts"):
                        place_file(path, passed)
                    self.accepted.append({
                        "file": path.name,
                        "state": "passed_all" if passed else "stopped",
                        "script": worker.accepted_script,
                    })
                if self._worker_stop.is_set():
                    break
        finally:
            with self._lock:
                self.worker_running = False
                self.current_file = ""

    def _drain(self, worker: Worker) -> None:
        deadline = time.time() + 5
        while worker.job_id and not worker.finished and time.time() < deadline:
            try:
                worker.poll()
            except Exception:
                return
            if not worker.finished:
                time.sleep(0.05)

    def _next_file(self) -> Path | None:
        files = learning_files(self.folder)
        if not files:
            return None
        path = files[self._file_index % len(files)]
        self._file_index += 1
        return path

    def _progress_path(self) -> Path | None:
        if self.library_path is None:
            return None
        return Path(self.library_path).parent / "progress.json"

    def _settings_path(self) -> Path | None:
        if self.library_path is None:
            return None
        return Path(self.library_path).parent / "settings.json"

    def _read_progress(self) -> dict | None:
        path = self._progress_path()
        if path is None or not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def _saved_attempt(self) -> dict | None:
        return self._progress

    def _load_search(self, saved) -> None:
        self.tried = set()
        self.board = []
        self.marks = None
        self.measured = False
        self.before_source = None
        if not isinstance(saved, dict):
            return
        self.measured = bool(saved.get("measured"))
        self.before_source = saved.get("before") or None
        self.marks = saved.get("marks") if isinstance(saved.get("marks"), dict) else None
        board = saved.get("board")
        self.board = list(board) if isinstance(board, list) else []
        for pair in saved.get("tried") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    self.tried.add(tried_key(pair[0], pair[1]))
                except (TypeError, ValueError):
                    continue

    def _reward_from(self, saved) -> dict:
        reading = saved.get("reward") if isinstance(saved, dict) else None
        days = self._stage_days(saved)
        daily = float(self.settings["daily_target"])
        if not isinstance(reading, dict):
            return reward_reading(None, None, self.settings["success_rate"], days=days, daily=daily)
        return reward_reading(
            reading.get("previous"),
            reading.get("win_percentage"),
            self.settings["success_rate"],
            reading.get("profit"),
            reading.get("profit_previous"),
            days=days,
            daily=daily,
        )

    def _stage_days(self, saved) -> int:
        if isinstance(saved, dict) and saved.get("stage_index") is not None:
            index = min(max(int(saved.get("stage_index") or 0), 0), len(STAGE_DAYS) - 1)
            return STAGE_DAYS[index]
        if isinstance(saved, dict) and isinstance(saved.get("reward"), dict) and saved["reward"].get("days"):
            return int(saved["reward"]["days"])
        return STAGE_DAYS[0]

    def _record_reward(self, summary: dict, days: int = 7) -> None:
        self.reward = reward_reading(
            self.reward.get("win_percentage"),
            summary.get("win_percentage"),
            self.settings["success_rate"],
            summary.get("profit"),
            self.reward.get("profit"),
            days=int(summary.get("days") or days),
            daily=float(self.settings["daily_target"]),
        )

    def _align_file_index(self) -> None:
        name = str((self._progress or {}).get("file") or "")
        if not name:
            return
        files = learning_files(self.folder)
        for index, path in enumerate(files):
            if path.name == name:
                self._file_index = index
                return
        self._progress = None

    def _save_attempt(self, source: str, title: str, book: StageBook, closed: set, order, finished: list) -> None:
        path = self._progress_path()
        if path is None:
            return
        payload = {
            "file": self.current_file,
            "source": source,
            "title": title,
            "stage_index": book.stage_index,
            "attempt": book.attempt,
            "closed": [list(pair) for pair in sorted(closed)],
            "order": None if order is None else asdict(order),
            "finished": finished,
            "reward": dict(self.reward),
            "tried": [list(pair) for pair in sorted(self.tried, key=lambda item: (str(item[0]), float(item[1])))],
            "measured": self.measured,
            "marks": self.marks,
            "board": self.board,
            "before": self.before_source,
        }
        self._progress = payload
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _clear_progress(self) -> None:
        self._progress = None
        path = self._progress_path()
        if path is not None and path.is_file():
            path.unlink()

    def _load_settings(self) -> None:
        path = self._settings_path()
        if path is None or not path.is_file():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(loaded, dict):
            return
        for key in SETTING_KEYS:
            if key in loaded and loaded[key] is not None:
                self.settings[key] = loaded[key]

    def _save_settings(self, values: dict) -> None:
        path = self._settings_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, indent=2), encoding="utf-8")

    def _load_library(self) -> None:
        if self.library_path is None or not self.library_path.is_file():
            return
        try:
            loaded = json.loads(self.library_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(loaded, list):
            return
        self.strategies = [item for item in loaded if isinstance(item, dict)]
        self._merge_met()
        numbers = []
        for item in self.strategies:
            text = str(item.get("id") or "")
            if text.startswith("strategy-") and text.removeprefix("strategy-").isdigit():
                numbers.append(int(text.removeprefix("strategy-")))
        self._next_strategy = max(numbers, default=0) + 1

    def _merge_met(self) -> None:
        path = Path(__file__).resolve().parent / "static" / "met-win-rate.json"
        if not path.is_file():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(loaded, list):
            return
        known = {str(item.get("id")) for item in self.strategies}
        for item in loaded:
            if isinstance(item, dict) and str(item.get("id")) not in known:
                self.strategies.append(item)
                known.add(str(item.get("id")))

    def _save_library(self) -> None:
        if self.library_path is None:
            return
        self.library_path.parent.mkdir(parents=True, exist_ok=True)
        kept = [item for item in self.strategies if str(item.get("id") or "").startswith("strategy-")]
        self.library_path.write_text(json.dumps(kept, indent=2), encoding="utf-8")

    def _catalog_path(self) -> Path | None:
        if self.library_path is None:
            return None
        return Path(self.library_path).parent / "catalog.json"

    def _load_catalog(self) -> None:
        path = self._catalog_path()
        if path is None or not path.is_file():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, list):
            self.results = [item for item in loaded if isinstance(item, dict) and item.get("id")]

    def _save_catalog(self) -> None:
        path = self._catalog_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.results), encoding="utf-8")

    def _remember_result(self, run: dict) -> None:
        """Keep a finished backtest in the library, including one that missed the goal."""
        row = _result_row(run, self.current_file)
        if row is None:
            return
        for index, item in enumerate(self.results):
            if item.get("id") == row["id"]:
                self.results[index] = row
                self._save_catalog()
                return
        self.results.append(row)
        if len(self.results) > 400:
            self.results = _ranked(self.results)[:400]
        self._save_catalog()

    def _import_saved_runs(self) -> None:
        saved = self._progress or {}
        if not isinstance(saved, dict):
            return
        for item in saved.get("finished") or []:
            if not isinstance(item, dict):
                continue
            run = dict(item.get("run") or {})
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
            for key in ("profit", "win_percentage", "success_rate", "worst_day", "trade_count", "quiet_sessions"):
                if run.get(key) is None and summary.get(key) is not None:
                    run[key] = summary[key]
            run.setdefault("instrument", item.get("instrument") or "")
            run.setdefault("file", saved.get("file") or "")
            run.setdefault("script", saved.get("title") or run.get("file") or "")
            self._remember_result(run)

    def _stop(self, job_id: str, book: StageBook | None = None) -> None:
        with self._lock:
            job = self.jobs[job_id]
            job.best = {
                "rows": _rows(book) if book is not None else list(job.feed.get("rows") or []),
                "reason": "attempts" if book is not None and book.stopped else "stopped",
            }
            job.feed["rows"] = job.best["rows"]
            if not job.states or job.states[-1] != "stopped":
                job.states.append("stopped")
            if self._worker_stop.is_set():
                self.open_job = None


def _day_rows(days: list[Day]) -> list[dict]:
    return [
        {
            "profit": day.profit,
            "worst_loss": day.worst_loss,
            "wins": day.wins,
            "trades": day.trades,
            "losses": day.losses,
        }
        for day in days
    ]


_RESULT_KEYS = (
    "script",
    "file",
    "instrument",
    "timeframe",
    "change",
    "days",
    "success_rate",
    "win_percentage",
    "profit",
    "worst_day",
    "trade_count",
    "quiet_sessions",
    "attempt",
)


def _script_file(folder: Path, file_name: str) -> Path | None:
    raw = str(file_name or "").strip()
    name = Path(raw).name
    if not raw or name != raw or name in {".", ".."}:
        return None
    roots = [
        folder,
        folder.parent / "completed strategies",
        folder.parent / "did not produce profits",
        folder.parent / "skipped",
    ]
    for root in roots:
        try:
            base = root.resolve()
            candidate = (base / name).resolve()
            candidate.relative_to(base)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            return candidate
    return None


def _with_change(source: str, change: str) -> str:
    text = str(change or "").strip()
    if not text or text == "script unchanged":
        return source
    name, _, raw = text.rpartition(" ")
    if not name:
        return source
    try:
        value = float(raw)
    except ValueError:
        return source
    if abs(value - round(value)) < 1e-9:
        value = int(round(value))
    try:
        return apply_change(source, name, value)
    except ValueError:
        return source


def _result_row(run: dict, file_name: str = "") -> dict | None:
    row = {}
    for key in _RESULT_KEYS:
        value = run.get(key)
        if value is not None:
            row[key] = value
    if not row.get("file") and file_name:
        row["file"] = file_name
    if isinstance(row.get("days"), list):
        row["days"] = len(row["days"])
    if not row.get("script"):
        row["script"] = row.get("file") or ""
    if not row.get("script"):
        return None
    parts = [
        row.get("file") or "",
        row.get("instrument") or "",
        row.get("timeframe") or "",
        row.get("days") or "",
        row.get("change") or "",
    ]
    digest = hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]
    row["id"] = f"result-{digest}"
    return row


def _profit_rank(row: dict) -> tuple:
    try:
        return (1, float(row.get("profit")))
    except (TypeError, ValueError):
        return (0, 0.0)


def _ranked(rows: list[dict]) -> list[dict]:
    return sorted((dict(row) for row in rows), key=_profit_rank, reverse=True)


def _accepted_run(summary: dict, window: int, rate: float, daily: float = 500) -> bool:
    """A table row has to reach the profit goal and the win percentage, with enough trades."""
    return (
        int(summary.get("trade_count") or 0) >= required_trades(window)
        and float(summary.get("win_percentage") or 0) + 1e-12 >= float(rate)
        and float(summary.get("profit") or 0) + 1e-9 >= profit_goal_for(window, daily)
    )


def _profit_of(days: list[Day], saved) -> float:
    if saved is not None:
        try:
            return round(float(saved), 2)
        except (TypeError, ValueError):
            pass
    return round(sum(day.profit for day in days), 2)


def _days_from(rows: list[dict]) -> list[Day]:
    return [
        Day(
            float(row.get("profit") or 0),
            float(row.get("worst_loss") or 0),
            int(row.get("wins") or 0),
            int(row.get("trades") or 0),
            int(row.get("losses") or 0),
        )
        for row in rows
    ]


def _tradable(instrument: str) -> bool:
    return (instrument or "").upper().strip() in MARKETS


def _preferred_input(inputs, last, daily_loss: float = 400, goal_win: float = 0.5) -> str:
    usable = [item for item in inputs if not scales_price(item)]
    if not usable:
        return ""
    if not last:
        return usable[0].name
    if int(last.get("quiet_sessions") or 0) >= 1:
        delta = next((item for item in usable if "delta" in item.name.lower()), None)
        if delta:
            return delta.name
    if float(last.get("worst_day") or 0) < -float(daily_loss):
        stops = [item for item in usable if "stop" in item.name.lower() or "stop" in (item.title or "").lower()]
        if stops:
            return max(stops, key=lambda item: item.default).name
    if float(last.get("win_percentage") or 0) + 1e-12 < float(goal_win):
        delta = next((item for item in usable if "delta" in item.name.lower()), None)
        if delta:
            return delta.name
    return usable[0].name


def _board_row(summary: dict, engine, market: str) -> dict:
    return {
        "instrument": market,
        "timeframe": engine.timeframe,
        "profit": summary.get("profit"),
        "win_percentage": summary.get("win_percentage"),
        "trade_count": summary.get("trade_count"),
        "quiet_sessions": summary.get("quiet_sessions"),
        "worst_day": summary.get("worst_day"),
    }


def choose_result(finished, window: int, rate: float, daily: float):
    """Use a result that already clears both goals. Otherwise use the highest win percentage."""
    passing = [item for item in finished if _accepted_run(item[1], window, rate, daily)]
    enough = [item for item in finished if int(item[1].get("trade_count") or 0) >= required_trades(window)]
    pool = passing or enough or list(finished)
    return max(pool, key=lambda item: float(item[1].get("win_percentage") or 0))


def trade_note(inputs, last) -> str:
    """Tell the meeting how the last test traded and which way the volume delta moves."""
    delta = next((item for item in inputs if "delta" in item.name.lower()), None)
    name = delta.name if delta else "the volume delta"
    current = f" from {delta.default:g}" if delta else ""
    if not last:
        return (
            "Every session needs at least one trade. "
            "Once a session is up $500, later trades do not count. "
            "A session counts at most two losing trades. "
            f"Raise {name} for fewer trades and lower {name}{current} for more trades."
        )
    quiet = int(last.get("quiet_sessions") or 0)
    days = int(last.get("days") or 0)
    base = (
        f"Last test succeeded {float(last.get('success_rate') or 0):.0%} "
        f"with win {float(last.get('win_percentage') or 0):.0%}, "
        f"profit {float(last.get('profit') or 0):,.0f}, "
        f"worst day {last.get('worst_day')}, trades {last.get('trade_count')}, "
        f"and {quiet} of {days} sessions had no trade. "
        "Once a session is up $500, later trades do not count. "
        "A session counts at most two losing trades."
    )
    if quiet:
        return base + f" Lower {name}{current} so every session gets a trade. Raising {name} takes fewer trades."
    return (
        base
        + f" Every session traded. Raise {name} for fewer trades or lower it for more trades, "
        + "and keep a trade in every session."
    )


def raises_delta_while_a_session_is_empty(order, inputs, last) -> bool:
    if not last or int(last.get("quiet_sessions") or 0) < 1 or getattr(order, "direction", "") != "up":
        return False
    return any(item.name == order.variable and "delta" in item.name.lower() for item in inputs)


def _readable_source(source: str) -> str:
    text = source.lstrip()
    if text.startswith("@version"):
        return "//" + text
    return source


def _can_run(source: str) -> bool:
    return 'strategy("' in source or "strategy('" in source


def _ready(proposal) -> bool:
    return bool(
        proposal.variable
        and str(proposal.justification).strip()
        and proposal.consistent_day
        and proposal.higher_win_rate
    )


def _gap(proposal) -> str:
    missing = []
    if not proposal.variable:
        missing.append("one input and a new value")
    if not proposal.justification or not proposal.consistent_day:
        missing.append("a reason this makes the $500 day more consistent")
    if not proposal.higher_win_rate:
        missing.append("a reason this raises the win percentage")
    return ", ".join(missing) or "both reasons"


def _waiting(proposals) -> str:
    gaps = []
    for seat, item in enumerate(proposals):
        if not _ready(item):
            gaps.append(f"Researcher {seat + 1} still owes {_gap(item)}.")
    if not gaps:
        return "The manager is choosing."
    return " ".join(gaps) + " The meeting stays open until every justification is in and the manager picks one."


def _proposal(item) -> dict:
    return {
        "who": "",
        "variable": item.variable,
        "value": item.value,
        "direction": item.direction,
        "justification": item.justification,
        "spoken": item.justification,
        "consistent_day": item.consistent_day,
        "higher_win_rate": item.higher_win_rate,
    }


def _rows(book: StageBook | None) -> list[dict]:
    if book is None:
        return []
    stored = []
    for count in STAGE_DAYS:
        stored.extend(asdict(row) for row in book.rows[count])
    return stored


def _open_inputs(found, closed: set[tuple[str, str]]) -> bool:
    for item in found:
        if (item.name, "up") not in closed or (item.name, "down") not in closed:
            return True
    return False
