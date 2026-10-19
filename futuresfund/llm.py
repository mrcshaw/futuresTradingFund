"""One client per desk, on one loaded Qwen3.8 27B dense model.

Histories and logs stay on that desk. A desk does not take a lock that makes
another desk wait. Each engine has its own process.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

_URL = "http://127.0.0.1:11434/api/chat"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("futures_agent", default=None)

# The desk window on the RTX 5090. The served model can accept 262,144.
# 40,960 leaves room for the 20GB Q5_K_M weights and a max thinking pass.
CONTEXT_CAP = 40960
# Leave the rest of the window for the reply.
COMPACT_AT = 20000

# Qwen3.8-27B dense. The served build is Q5_K_M, about 20GB, on a 32GB RTX 5090.
# FUTURES_LLM_BASE replaces the served name. FUTURES_LLM_<ID> replaces one desk's alias.
MOE_BASE = "smtek/Qwen3.8-27B:Q5_K_M"
SERVED_MODEL = "futures-qwen38"
_MODELFILE = Path(__file__).resolve().parent / "models" / "Qwen38.Modelfile"
_DESK_ALIASES = (
    "desk-quant-trader",
    "desk-portfolio",
    "desk-ingestion",
    "desk-floor",
    "desk-researcher",
    "desk-card-keeper",
    "desk-strategy-developer",
    "desk-strategy-developer-2",
    "desk-strategy-developer-5",
    "desk-strategy-developer-15",
    "desk-script-checker",
    "desk-creation-tester",
    "desk-lesson-writer",
    "desk-risk",
    "desk-analyst",
    "desk-developer",
    "desk-developer-2",
    "desk-developer-15",
    "desk-systems",
    "desk-compliance",
)
_BASES = {alias: MOE_BASE for alias in _DESK_ALIASES}

_clients: dict[str, "AgentModel"] = {}
_clients_lock = threading.Lock()
_AMOUNT = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?")
_TOOL_LINE = re.compile(r"^\s*TOOL\s+([a-z]+)(?:\s*:\s*(.*))?$", re.IGNORECASE)
_READ_TOOLS = ("objective", "library", "research", "execution", "notes")


def reasoning_model() -> str:
    """The one loaded dense model every desk is served by."""
    return os.environ.get("FUTURES_LLM_BASE", "").strip() or SERVED_MODEL


def runner_model(name: str) -> str:
    """A desk alias runs on the shared Qwen3.8 27B. Any other name is used as given."""
    if name in _BASES:
        return reasoning_model()
    return name or reasoning_model()


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


@contextmanager
def use_agent(agent: str):
    """Model calls on this thread belong to this desk."""
    token = _current.set(agent)
    try:
        yield
    finally:
        _current.reset(token)


def complete(prompt: str, agent: str | None = None, *, fresh: bool = False, num_ctx: int | None = None, result: dict | None = None) -> str:
    """Ask that desk's model. A failure returns a short sentence instead of taking the desk down.

    `fresh` leaves earlier replies out of the call, for a prompt that already carries everything.
    Research context uses the 28,672 token window. A larger request is cut to that cap.
    `result` is the engine measurement this desk just ran, used when the window is compacted.
    """
    name = agent or _current.get() or "Portfolio Manager"
    return client_for(name).ask(prompt, fresh=fresh, num_ctx=num_ctx, result=result)


def estimate_tokens(text: str) -> int:
    """About four characters to a token, enough to compact before the reply is crowded out."""
    return (len(text or "") + 3) // 4


def message_tokens(messages: list[dict]) -> int:
    return estimate_tokens("\n".join(str(item.get("content") or "") for item in messages))


def note_result(agent: str, result: dict) -> None:
    """Remember the engine measurement for this desk. Compaction copies it into the lessons."""
    if not agent or not isinstance(result, dict):
        return
    client_for(agent).latest_result = dict(result)


def maintain_lessons() -> bool:
    """Summarize the lessons file when it is large enough to slow the research window."""
    from futuresfund.learn import maintain_lessons as _maintain

    return _maintain()


_chat_history: dict[str, list[dict]] = {}


def answer_chat(agent: str, prompt: str, board=None) -> str:
    """Answer a person on this desk's own model. A TOOL line runs the harness, then the model answers."""
    name = agent or "Portfolio Manager"
    model = model_name(name)
    prior = _chat_history.get(name, [])[-4:]
    messages = [
        {"role": "system", "content": _clip(_chat_system(name), 2500)},
        *prior,
        {"role": "user", "content": _clip(prompt, 3500)},
    ]
    sources = prompt or ""
    reply = ""
    for _ in range(2):
        reply = _strip_handoff(_post(model, messages, num_ctx=4096, timeout=180 if _reasons(name) else 90, think=_reasons(name)))
        if reply.startswith("The meeting model could not answer"):
            return reply
        tool, argument = _tool_request(reply)
        if tool is None:
            break
        result = _run_tool(name, tool, argument, board)
        sources = f"{sources}\n{result}"
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"Tool result for {tool}:\n{_clip(result, 2000)}"})
    else:
        if _tool_request(reply)[0] is not None:
            reply = "I could not finish that from the desk records."
    reply = _ground_reply(reply, sources)
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
        self.latest_result: dict | None = None
        self._lock = threading.Lock()

    def ask(self, prompt: str, *, fresh: bool = False, num_ctx: int | None = None, result: dict | None = None) -> str:
        if result is not None:
            self.latest_result = dict(result)
        window = _research_window(num_ctx)
        limit = max(6000, window * 3)
        maintain_lessons()
        messages = self._messages(prompt, fresh=fresh, limit=limit)
        if message_tokens(messages) > COMPACT_AT:
            self.compact()
            messages = self._messages(prompt, fresh=fresh, limit=limit)
            messages = _fit_messages(messages, COMPACT_AT)
        # This desk's lock is not held across the call, so another desk is not waiting on it.
        reply = _post(self.model, messages, num_ctx=window, think=_reasons(self.agent))
        if fresh:
            return reply
        with self._lock:
            self.history.append({"role": "user", "content": prompt})
            self.history.append({"role": "assistant", "content": reply})
            if len(self.history) > 8:
                self.history = self.history[-8:]
        return reply

    def compact(self) -> str | None:
        """Append one new measured lesson, then drop this desk's in-memory turns."""
        from futuresfund.learn import append_window_lesson

        written = append_window_lesson(self.latest_result)
        with self._lock:
            self.history.clear()
        _chat_history.pop(self.agent, None)
        return written

    def _messages(self, prompt: str, *, fresh: bool = False, limit: int = 6000) -> list[dict]:
        prior: list[dict] = []
        if not fresh:
            with self._lock:
                prior = [{"role": item["role"], "content": _clip(item.get("content") or "")} for item in self.history[-4:]]
        return [{"role": "system", "content": _clip(_system(self.agent), 6000)}, *prior, {"role": "user", "content": _clip(prompt, limit)}]


def _library_catalog() -> str:
    from futuresfund.indicators import agent_catalog as indicator_catalog
    from futuresfund.library import agent_catalog

    return agent_catalog() + "\n" + indicator_catalog(900)


def _chart_candle_vision(agent: str) -> str:
    """The chart developer and the chart researcher adjust from the replay notes. Their window stays one year."""
    if agent != "Chart researcher" and not str(agent).endswith("chart developer"):
        return ""
    from futuresfund.strategy_team import candle_lines

    return candle_lines(audience="chart") + "\n"


def _system(agent: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from futuresfund.learn import notes_digest, research_guidelines
    from futuresfund.objective import acceptance_line, account_line, goal_line
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
    if agent in {"Chart researcher", "Card keeper", "Lesson writer"} or agent.endswith("strategy developer"):
        guidance = research_guidelines()
    # The clipped system prompt is 6,000 characters. The ranking rule has to
    # stay ahead of the library catalog, or a long lesson digest pushes it out.
    guide_lead = ""
    guide_rest = ""
    if guidance:
        cut = guidance.find("\n", 900)
        if cut == -1:
            cut = len(guidance)
        guide_lead = guidance[:cut].rstrip() + "\n"
        guide_rest = guidance[cut:].lstrip() + "\n"
    return (
        f"You are the {agent}. {role} {_instance_line()}"
        "You can talk about this desk: the charts, research, the strategy library, meetings, orders, and the open trade. "
        "You speak only for this desk. Another desk has its own instance and its own log. "
        "Guideline 1: flatten every open trade at 16:45 New York with strategy.close_all. A trade left open after 16:45 can liquidate the account. "
        + f"{acceptance_line()} {account_line()} {goal_line()} The take profit is larger than the stop. A strategy with one trade down more than $1,500 is not included. "
        + "Do not invent a profit, a drawdown, or a trade count. "
        f"{_chart_candle_vision(agent)}"
        f"Desk clock: {clock}. {meeting_note()} Do not hold a meeting outside those times. "
        "Strategy notes already learned, use these before any other source:\n"
        f"{notes_digest()}\n"
        f"{guide_lead}"
        f"{_library_catalog()}\n"
        f"{describe(size)} "
        f"{guide_rest}"
        "Reason from these limits and from the numbers in the prompt. "
        f"The engine's backtest is the fact.\n{script}"
    )


def _chat_system(agent: str) -> str:
    from futuresfund.learn import research_guidelines
    from futuresfund.objective import acceptance_line, account_line, goal_line
    from futuresfund.roster import ROSTER

    from futuresfund.indicators import agent_catalog as indicator_catalog

    found = next((item for item in ROSTER if item["name"] == agent), None)
    role = found["role"] if found else "A desk on this futures fund."
    return (
        f"You are the {agent}. {role} {_instance_line()}"
        "If a fact you need is not already in the message, reply with one line and nothing else: "
        "TOOL objective, TOOL library, TOOL research, TOOL execution, or TOOL notes. "
        "The Portfolio Manager may also reply TOOL task: and the task in plain words. "
        "No desk places an order from chat. "
        "Guideline 1: flatten every open trade at 16:45 New York with strategy.close_all. A trade left open after 16:45 can liquidate the account. "
        + f"{acceptance_line()} {account_line()} {goal_line()} The take profit is larger than the stop. A strategy with one trade down more than $1,500 is not included. "
        + "Do not invent a profit, a drawdown, or a trade count. The engine's backtest is the fact. "
        f"{_chart_candle_vision(agent)}"
        f"{indicator_catalog(600)} "
        "A person is talking with you about this application. "
        "Answer the question they asked first, in plain words, from the facts and the firm record in the message. "
        "You can talk about the desk, the charts, research, the strategy library, report email, what is executing, meetings, and the account. "
        "Do not give a general summary of the desk unless they ask for one. "
        "If a number is not in the message, say you do not have that number. "
        "Do not place an order. Do not write a To: line. Answer in a few sentences. "
        + (research_guidelines() if agent.endswith("strategy developer") or agent == "Chart researcher" else "")
    )


def _strip_handoff(text: str) -> str:
    kept = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("to:"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _research_window(num_ctx: int | None) -> int:
    """Research context uses the 40,960 window. A larger request stays on that cap."""
    if isinstance(num_ctx, int) and num_ctx > CONTEXT_CAP:
        return CONTEXT_CAP
    return CONTEXT_CAP


def _fit_messages(messages: list[dict], limit: int) -> list[dict]:
    """After compaction, keep the prompt inside the room left for the reply."""
    rows = [dict(item) for item in messages]
    if message_tokens(rows) <= limit:
        return rows
    head = message_tokens(rows[:-1])
    room_chars = max(1, limit - head) * 4
    rows[-1]["content"] = _clip(rows[-1].get("content") or "", room_chars)
    while rows and message_tokens(rows) > limit and rows[-1].get("content"):
        shorter = _clip(rows[-1]["content"], max(1, len(rows[-1]["content"]) // 2))
        if shorter == rows[-1]["content"]:
            rows[-1]["content"] = ""
            break
        rows[-1]["content"] = shorter
    return rows


def _clip(text: str, limit: int = 1500) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"


def _instance_line() -> str:
    return (
        "You are this desk's own instance of Qwen3.8 27B, a dense model. "
        "The loaded weights are shared so the desk stays responsive, and this instance does not wait on another desk. "
    )


# Extra-high thinking. This Qwen build rejects "xhigh". Ollama's highest
# accepted level is max (above high, medium, and low).
THINK_LEVEL = "max"


def _reasons(agent: str) -> bool:
    """Strategy design and the chart researcher use the model's thinking pass."""
    return agent in {
        "Chart researcher", "Card keeper", "Lesson writer", "Script checker", "Creation tester",
        "Systems Administrator", "Trading Analyst",
    } or agent.endswith(
        "strategy developer"
    )


def _think_value(think: bool):
    """Off, or the extra-high level when a desk is asked to think."""
    return THINK_LEVEL if think else False


def _tool_request(text: str) -> tuple[str | None, str]:
    line = ((text or "").strip().splitlines() or [""])[0].strip()
    match = _TOOL_LINE.match(line)
    if match is None:
        return None, ""
    name = match.group(1).lower()
    if name not in _READ_TOOLS and name != "task":
        return None, ""
    return name, (match.group(2) or "").strip()


def _run_tool(agent: str, name: str, argument: str, board) -> str:
    """Facts and the tasks a desk is already allowed to do. Nothing here places an order."""
    if name == "task":
        if agent != "Portfolio Manager":
            return "That task is not available from this desk. This desk does not place orders."
        if board is None:
            return "No desk is attached to this chat."
        from futuresfund.desk_chat import _task

        return _task(board, argument.lower()) or "That task is not available from this desk."
    if name == "objective":
        from futuresfund.learn import research_guidelines
        from futuresfund.objective import describe

        return describe() + "\n" + research_guidelines()
    if name == "library":
        return _library_catalog()
    if name == "research":
        if board is None:
            return "Research state was not passed with this chat."
        from futuresfund.desk_chat import research_lines

        return "\n".join(research_lines(board))
    if name == "execution":
        from futuresfund.desk_chat import executing_lines

        return "\n".join(executing_lines())
    if name == "notes":
        from futuresfund.learn import notes_digest

        return notes_digest()
    return "That tool is not on this desk."


def _ground_reply(reply: str, sources: str) -> str:
    """A dollar amount that is not in the desk records is not an answer."""
    compact = (sources or "").replace(" ", "").replace(",", "")
    invented = []
    for match in _AMOUNT.finditer(reply or ""):
        token = match.group(0)
        key = token.replace(" ", "").replace(",", "")
        if key not in compact:
            invented.append(token.strip())
    if not invented:
        return reply
    return "I will not use a profit that is not in the desk records."


def _post(model: str, messages: list[dict], *, think: bool = False, num_ctx: int = 4096, timeout: int = 120) -> str:
    payload_body = {
        "model": runner_model(model),
        "messages": messages,
        "stream": False,
        "think": _think_value(think),
        "options": {"temperature": 0.4, "num_ctx": num_ctx},
    }
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
    """Build the one Qwen3.8 27B the desks share, once that model is installed."""
    import shutil
    import subprocess

    if shutil.which("ollama") is None or not _MODELFILE.is_file():
        return
    if reasoning_model() != SERVED_MODEL:
        return
    try:
        listed = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return
    have = listed.stdout or ""
    if MOE_BASE not in have or SERVED_MODEL in have:
        return
    try:
        subprocess.run(
            ["ollama", "create", SERVED_MODEL, "-f", str(_MODELFILE)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
