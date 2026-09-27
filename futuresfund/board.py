"""In-memory floor shared by the API and a running analysis."""

from __future__ import annotations

import threading
from datetime import datetime

from futuresfund.roster import ROSTER, TASKS


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Board:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self.messages: list[dict] = []
        self.agent_status: dict[str, str] = {agent["name"]: "idle" for agent in ROSTER}
        self.reports: dict[str, str] = {}
        self.run: dict | None = None
        self.activity: dict[str, str | None] = {"agent": None, "task": None}
        self.cancel = threading.Event()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "messages": list(self.messages),
                "agent_status": dict(self.agent_status),
                "reports": dict(self.reports),
                "run": dict(self.run) if self.run else None,
                "activity": dict(self.activity),
            }

    def post(self, author: str, text: str, kind: str = "speech", channel: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            return {}
        with self._lock:
            self._seq += 1
            message = {
                "id": self._seq,
                "time": _now(),
                "author": author,
                "kind": kind,
                "text": text,
                "channel": channel or author,
            }
            self.messages.append(message)
            if len(self.messages) > 400:
                self.messages = self.messages[-400:]
            return message

    def set_status(self, name: str, status: str) -> None:
        with self._lock:
            if name not in self.agent_status or self.agent_status[name] == status:
                return
            self.agent_status[name] = status
            if status == "working":
                self.activity = {"agent": name, "task": TASKS.get(name, "Working")}
            elif self.activity.get("agent") == name:
                self.activity = {"agent": None, "task": None}

    def set_activity(self, agent: str | None, task: str | None) -> None:
        with self._lock:
            self.activity = {"agent": agent, "task": task}

    def set_report(self, key: str, text: str) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        with self._lock:
            previous = self.reports.get(key, "")
            if text == previous:
                return None
            self.reports[key] = text
            if text.startswith(previous) and previous:
                return text[len(previous):].strip()
            return text

    def begin_run(self, contract: str, trade_date: str, desks: list[str]) -> None:
        with self._lock:
            self.cancel.clear()
            self.reports.clear()
            self.agent_status = {agent["name"]: "idle" for agent in ROSTER}
            self.activity = {"agent": None, "task": "Loading the local model"}
            self.run = {
                "contract": contract,
                "date": trade_date,
                "desks": desks,
                "status": "running",
                "error": None,
                "started_at": _now(),
            }

    def mark_stopped(self) -> None:
        with self._lock:
            if self.run and self.run.get("status") == "running":
                self.run["status"] = "stopped"
            for name, status in self.agent_status.items():
                if status in {"working", "pending"}:
                    self.agent_status[name] = "idle"
            self.activity = {"agent": None, "task": None}
            self.cancel.set()

    def finish_run(self, status: str, error: str | None = None) -> None:
        with self._lock:
            if self.run and self.run.get("status") == "stopped":
                return
            if self.run:
                self.run["status"] = status
                self.run["error"] = error

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self.run and self.run.get("status") == "running")
