"""One model per desk. Histories and logs stay on that desk. The backtest engine stays shared."""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

_URL = "http://127.0.0.1:11434/api/chat"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("futures_agent", default=None)

# The name each desk calls, and the installed model that name is built from.
# An environment value FUTURES_LLM_<ID> replaces the name without editing this file.
_BASES = {
    "desk-quant-trader": "qwen3-trading",
    "desk-portfolio": "deepseek-r1-desk",
    "desk-ingestion": "gemma2",
    "desk-floor": "qwen3",
    "desk-researcher": "deepseek-r1:32b",
    "desk-card-keeper": "qwen3",
    "desk-strategy-developer": "qwen3-trading",
    "desk-script-checker": "qwen3",
    "desk-creation-tester": "qwen3-trading",
    "desk-lesson-writer": "qwen3",
    "desk-risk": "gemma2",
    "desk-analyst": "gemma",
    "desk-developer": "qwen3-trading",
    "desk-developer-2": "qwen3-trading",
    "desk-developer-15": "qwen3-trading",
    "desk-systems": "gemma2",
    "desk-compliance": "gemma",
}

_clients: dict[str, "AgentModel"] = {}
_clients_lock = threading.Lock()
_model_locks: dict[str, threading.Lock] = {}


def model_name(agent: str) -> str:
    """The model this desk calls. A FUTURES_LLM_<ID> setting overrides the default."""
    from futuresfund.roster import ROSTER

    found = next((item for item in ROSTER if item["name"] == agent), None)
    if found is None:
        return "desk-portfolio"
    override = os.environ.get(f"FUTURES_LLM_{found['id'].upper()}", "").strip()
    return override or str(found.get("model") or "desk-portfolio")


def client_for(agent: str) -> "AgentModel":
    """The desk's own model client. A second call returns the same client."""
    with _clients_lock:
        client = _clients.get(agent)
        if client is None or client.model != model_name(agent):
            client = AgentModel(agent, model_name(agent))
            _clients[agent] = client
        return client


def _lock_for(model: str) -> threading.Lock:
    with _clients_lock:
        lock = _model_locks.get(model)
        if lock is None:
            lock = threading.Lock()
            _model_locks[model] = lock
        return lock


@contextmanager
def use_agent(agent: str):
    """Model calls on this thread belong to this desk."""
    token = _current.set(agent)
    try:
        yield
    finally:
        _current.reset(token)


def complete(prompt: str, agent: str | None = None) -> str:
    """Ask that desk's model. A failure returns a short sentence instead of taking the desk down."""
    name = agent or _current.get() or "Portfolio Manager"
    return client_for(name).ask(prompt)


_CHAT_MODELS = {
    "desk-portfolio": "qwen3",
    "desk-researcher": "qwen3",
}
_chat_history: dict[str, list[dict]] = {}


def answer_chat(agent: str, prompt: str) -> str:
    """Answer a person at the desk. The large meeting models are not used here."""
    name = agent or "Portfolio Manager"
    model = _CHAT_MODELS.get(model_name(name), model_name(name))
    prior = _chat_history.get(name, [])[-4:]
    messages = [
        {"role": "system", "content": _clip(_chat_system(name), 2500)},
        *prior,
        {"role": "user", "content": _clip(prompt, 3500)},
    ]
    reply = _strip_handoff(_post(model, messages, num_ctx=4096, timeout=90))
    if reply.startswith("The meeting model could not answer"):
        return reply
    history = _chat_history.setdefault(name, [])
    history.append({"role": "user", "content": _clip(prompt, 800)})
    history.append({"role": "assistant", "content": _clip(reply, 800)})
    del history[:-6]
    return reply


class AgentModel:
    """One desk, one model, one history. Nothing here is written onto another desk."""

    def __init__(self, agent: str, model: str) -> None:
        self.agent = agent
        self.model = model
        self.history: list[dict] = []
        self._lock = threading.Lock()

    def ask(self, prompt: str) -> str:
        messages = self._messages(prompt)
        with _lock_for(self.model):
            reply = _post(self.model, messages)
        with self._lock:
            self.history.append({"role": "user", "content": prompt})
            self.history.append({"role": "assistant", "content": reply})
            if len(self.history) > 8:
                self.history = self.history[-8:]
        return reply

    def _messages(self, prompt: str) -> list[dict]:
        with self._lock:
            prior = [{"role": item["role"], "content": _clip(item.get("content") or "")} for item in self.history[-4:]]
        return [{"role": "system", "content": _clip(_system(self.agent), 6000)}, *prior, {"role": "user", "content": _clip(prompt, 6000)}]


def _library_catalog() -> str:
    from futuresfund.library import agent_catalog

    return agent_catalog()


def _system(agent: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from futuresfund.learn import notes_digest, research_guidelines
    from futuresfund.prop_rules import describe
    from futuresfund.roster import ROSTER
    from futuresfund.session import meeting_note
    from futuresfund.strategy import get_strategy, lead_script

    found = next((item for item in ROSTER if item["name"] == agent), None)
    role = found["role"] if found else "A desk on this futures fund."
    clock = datetime.now(ZoneInfo("America/New_York")).strftime("%A %I:%M %p ET")
    size = float((get_strategy() or {}).get("account_size") or 50000)
    lead = lead_script()
    if lead:
        script = (
            f"The lead strategy on headquarters is {lead['title']}: "
            f"{lead['trades']} trades, profit {lead['net_profit']}, drawdown {lead['max_drawdown']}. "
            f"{lead.get('formula') or ''}\n"
        )
    else:
        script = "No lead strategy is on headquarters yet.\n"
    guidance = ""
    if agent in {"Chart researcher", "Strategy developer", "Card keeper", "Lesson writer"}:
        guidance = research_guidelines() + "\n"
    return (
        f"You are the {agent}. {role} "
        "You can talk about this desk: the charts, research, the strategy library, meetings, orders, and the open trade. "
        "You speak only for this desk. Another desk has its own model and its own log. "
        f"Desk clock: {clock}. {meeting_note()} Do not hold a meeting outside those times. "
        "Strategy notes already learned, use these before any other source:\n"
        f"{notes_digest()}\n"
        f"{_library_catalog()}\n"
        f"{describe(size)} "
        f"{guidance}"
        "Reason from these limits and from the numbers in the prompt. "
        "Do not invent a profit, a drawdown, or a trade count. "
        f"The engine's backtest is the fact.\n{script}"
    )


def _chat_system(agent: str) -> str:
    from futuresfund.learn import research_guidelines
    from futuresfund.roster import ROSTER

    found = next((item for item in ROSTER if item["name"] == agent), None)
    role = found["role"] if found else "A desk on this futures fund."
    guidance = ""
    if agent in {"Chart researcher", "Strategy developer", "Card keeper", "Lesson writer"}:
        guidance = research_guidelines() + " "
    return (
        f"You are the {agent}. {role} {guidance}"
        "A person is talking with you about this application. "
        "Explain the desk, the charts, research, the strategy library, meetings, and the account from the firm record. "
        "If a number is not in the message, say you do not have that number. "
        "Do not invent a profit, a drawdown, a trade count, or an open position. "
        "Do not place an order. Do not write a To: line. Answer in a few sentences."
    )


def _strip_handoff(text: str) -> str:
    kept = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("to:"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _clip(text: str, limit: int = 1500) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"


def _post(model: str, messages: list[dict], *, think: bool = False, num_ctx: int = 4096, timeout: int = 120) -> str:
    payload_body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.4, "num_ctx": num_ctx},
    }
    if think:
        payload_body["think"] = True
    body = json.dumps(payload_body).encode()
    request = urllib.request.Request(_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 400 and len(messages) > 2:
            return _post(model, [messages[0], messages[-1]], think=False, num_ctx=num_ctx, timeout=timeout)
        return f"The meeting model could not answer: HTTP {exc.code}. {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return f"The meeting model could not answer: {exc}"
    message = payload.get("message") or {}
    text = str(message.get("content") or "").strip()
    if not text:
        text = str(message.get("thinking") or "").strip()
    return _THINK.sub("", text).strip()


def ensure_agent_models() -> None:
    """Give each desk a model name of its own, built from a model already installed."""
    import shutil
    import subprocess

    if shutil.which("ollama") is None:
        return
    try:
        listed = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return
    have = listed.stdout or ""
    for alias, base in _BASES.items():
        if alias in have:
            continue
        if base.split(":")[0] not in have:
            continue
        try:
            subprocess.run(["ollama", "cp", base, alias], capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
