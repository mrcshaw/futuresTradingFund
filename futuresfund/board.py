"""In-memory floor shared by the API and a running analysis."""

from __future__ import annotations

import threading
from datetime import datetime

from futuresfund.roster import ROSTER, TASKS


def _brief(text: str) -> str:
    line = (text or "").strip().split("\n", 1)[0].strip()
    if len(line) > 110:
        return line[:107].rstrip() + "..."
    return line


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Board:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self.messages: list[dict] = []
        self.agent_status: dict[str, str] = {agent["name"]: "idle" for agent in ROSTER}
        self.agent_tasks: dict[str, str] = {}
        self.agent_logs: dict[str, list[str]] = {}
        self.reports: dict[str, str] = {}
        self.run: dict | None = None
        self.activity: dict[str, str | None] = {"agent": None, "task": None}
        self.research = "idle"
        self.cancel = threading.Event()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "messages": list(self.messages),
                "agent_status": dict(self.agent_status),
                "agent_tasks": dict(self.agent_tasks),
                "agent_logs": {name: list(lines) for name, lines in self.agent_logs.items()},
                "reports": dict(self.reports),
                "run": dict(self.run) if self.run else None,
                "activity": dict(self.activity),
                "research": self.research,
            }

    def post(self, author: str, text: str, kind: str = "speech", channel: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            return {}
        with self._lock:
            message = self._append_locked(author, text, kind, channel or author)
            brief = _brief(text)
            if author in self.agent_status and brief:
                self._remember(author, brief)
            return message

    def set_research(self, state: str) -> None:
        with self._lock:
            self.research = state

    def set_status(self, name: str, status: str) -> None:
        with self._lock:
            if self.cancel.is_set():
                return
            if name not in self.agent_status or self.agent_status[name] == status:
                return
            self.agent_status[name] = status
            if status == "working" and not self.agent_tasks.get(name):
                task = TASKS.get(name, "Working")
                self._remember(name, task)
                self.activity = {"agent": name, "task": task}
            elif self.activity.get("agent") == name and status in {"done", "idle"}:
                self.activity = {"agent": None, "task": None}

    def set_activity(self, agent: str | None, task: str | None) -> None:
        with self._lock:
            if self.cancel.is_set():
                return
            self.activity = {"agent": agent, "task": task}
            if agent and task:
                self._remember(agent, task)
                if agent in self.agent_status:
                    self.agent_status[agent] = "working"

    def log(self, agent: str, message: str) -> None:
        """Record one line on that agent's own log and on that agent's channel."""
        text = (message or "").strip()
        if not agent or not text:
            return
        with self._lock:
            self._remember(agent, text)
            if agent in self.agent_status and not self.cancel.is_set():
                self.agent_status[agent] = "working"
            self.activity = {"agent": agent, "task": text}
            self._append_locked(agent, text, "log", agent)

    def _append_locked(self, author: str, text: str, kind: str, channel: str) -> dict:
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
        self._trim_messages()
        return message

    def _trim_messages(self) -> None:
        """Keep each channel's recent lines. One busy desk must not erase the others."""
        kept = []
        counts: dict[str, int] = {}
        for message in reversed(self.messages):
            channel = str(message.get("channel") or "")
            counts[channel] = counts.get(channel, 0) + 1
            if counts[channel] <= 60:
                kept.append(message)
        kept.reverse()
        self.messages = kept

    def _remember(self, agent: str, text: str) -> None:
        lines = self.agent_logs.setdefault(agent, [])
        lines.append(text)
        if len(lines) > 40:
            del lines[:-40]
        self.agent_tasks[agent] = text

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
            self.agent_tasks = {}
            self.agent_logs = {}
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
                    self.agent_tasks[name] = "Stopped"
                    self.agent_logs.setdefault(name, []).append("Stopped")
            self.cancel.set()
            if self.research == "running":
                self.research = "stopping"
                self.activity = {"agent": "System", "task": "Stopping the researchers."}
            else:
                self.research = "stopped"
                self.activity = {"agent": None, "task": None}

    def acknowledge_stop(self) -> None:
        with self._lock:
            self.research = "stopped"
            self.activity = {"agent": None, "task": None}

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
