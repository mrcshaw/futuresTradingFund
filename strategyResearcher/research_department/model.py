"""The three researchers and the manager. Each call is local Qwen. Think stays max."""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

from research_department.inputs import ScriptInput
from research_department.meeting import Proposal, step_limit


THINK_LEVEL = "max"
REPLY_SECONDS = 600
_URL = os.environ.get("RESEARCH_LLM_URL", "http://127.0.0.1:11434/api/chat")
_MODEL = os.environ.get("RESEARCH_LLM", "futures-qwen38")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

_CHANGE_RULES = (
    "Change one input by one step. The current value is the default at the end of the input line. "
    "Do not change the point value. The backtest sets it to $50 on ES, $20 on NQ, and $2 on MNQ, "
    "because those markets do not share one point value. "
    "Do not raise a stop. A larger stop makes the losing trade bigger. "
    "About $1,000 of profit for every 7 days is the first profit to clear. "
    "A change is kept only when the best profit does not fall and the best win percentage does not fall. "
    "If either one falls, that change is reverted. "
)


def _input_line(item: ScriptInput) -> str:
    title = f' "{item.title}"' if item.title else ""
    return f"{item.name}{title} {item.kind} step {step_limit(item):g} default {item.default:g}"


def _tried_line(tried) -> str:
    shown = []
    for name, value in tried or ():
        number = float(value)
        text = str(int(number)) if abs(number - round(number)) < 1e-9 else f"{number:g}"
        shown.append(f"{name} {text}")
    if not shown:
        return ""
    return "Already tried and reverted: " + ", ".join(shown) + ". Do not propose those values.\n"


_SYSTEM = (
    "You are in the research department. "
    "You are answering the previous person in the user message. "
    "A change has to give a reason for a more consistent $500 day and a separate reason for a higher win percentage. "
    "A change that only adds trades, or only widens the stop, is not a complete answer. "
    "Reply with one JSON object and no other text."
)


def formatted_text(text: str) -> str:
    """The reply with think tags removed and paragraph breaks kept."""
    cleaned = _THINK.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for block in re.split(r"\n\s*\n", cleaned):
        line = " ".join(block.split())
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def visible_text(text: str) -> str:
    """The reply with the think tags removed and the whitespace collapsed."""
    return " ".join(formatted_text(text).split())


def ask(prompt: str, listener=None) -> str:
    """One fresh reply. Earlier meetings are not kept on this call.

    A listener receives the growing thought and the growing answer so the desk can show them.
    """
    body = json.dumps({
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": listener is not None,
        "think": THINK_LEVEL,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }).encode("utf-8")
    request = urllib.request.Request(_URL, data=body, headers={"Content-Type": "application/json"})
    deadline = time.monotonic() + REPLY_SECONDS
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if listener is None:
                _arm(response, min(120.0, max(1.0, deadline - time.monotonic())))
                payload = json.loads(response.read().decode("utf-8"))
                message = payload.get("message") or {}
                text = str(message.get("content") or "").strip() or str(message.get("thinking") or "").strip()
                return visible_text(text)
            return _stream(response, listener, deadline)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return f"The meeting model could not answer: HTTP {exc.code}. {detail}"
    except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        return f"The meeting model could not answer: {exc}"


def _stream(response, listener, deadline: float) -> str:
    thinking: list[str] = []
    content: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _arm(response, min(20.0, remaining))
        try:
            raw = response.readline()
        except (TimeoutError, socket.timeout):
            continue
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = data.get("message") or {}
        piece_t = str(message.get("thinking") or "")
        piece_c = str(message.get("content") or "")
        if piece_t:
            thinking.append(piece_t)
            _hear(listener, "thinking", "".join(thinking))
        if piece_c:
            content.append(piece_c)
            _hear(listener, "content", "".join(content))
        if data.get("done"):
            text = "".join(content).strip() or "".join(thinking).strip()
            return visible_text(text)
    text = "".join(content).strip() or "".join(thinking).strip()
    if text:
        return visible_text(text)
    return "The meeting model could not answer: the reply took longer than 10 minutes."


def _arm(response, seconds: float) -> None:
    sock = response
    for name in ("fp", "raw", "_sock"):
        sock = getattr(sock, name, None)
        if sock is None:
            return
    try:
        sock.settimeout(max(0.2, float(seconds)))
    except OSError:
        return


def _hear(listener, kind: str, text: str) -> None:
    try:
        listener(kind, text)
    except Exception:
        return


def as_speech(text: str, inputs: list[ScriptInput]) -> str:
    """The sentence the next agent answers."""
    proposal = parse_proposal(text or "", inputs)
    if proposal.variable and str(proposal.justification).strip():
        move = "Lower" if proposal.direction == "down" else "Raise"
        return f"{move} {proposal.variable} to {proposal.value}. {proposal.justification}"
    data = _object(text or "") or {}
    if "choice" in data:
        try:
            index = int(data["choice"])
        except (TypeError, ValueError):
            index = -1
        names = ["Researcher 1", "Researcher 2", "Researcher 3"]
        name = names[index] if 0 <= index < 3 else "one of the three"
        return f"I'll take {name}'s proposal."
    return visible_text(text or "")[:1500]


def researcher_prompt(
    title: str,
    inputs: list[ScriptInput],
    closed: set[tuple[str, str]],
    seat: int,
    note: str = "",
    preferred: str = "",
    prior: list[str] | None = None,
    tried=None,
) -> str:
    lines = [_input_line(item) for item in inputs]
    shut = [f"{name} {direction}" for name, direction in sorted(closed)] or ["none"]
    chosen = preferred or (inputs[seat % len(inputs)].name if inputs else "")
    heard = [item.strip() for item in (prior or []) if str(item).strip()]
    body = (
        f"Strategy: {title}. You are Researcher {seat + 1}. "
        "The goal is a consistent $500 day and a higher win percentage. "
        "Every session needs at least one trade. A session with no trade cannot make $500. "
        "Once a session is up $500, later trades do not count. A session counts at most two losing trades. "
        "The test moves on when the profit reaches 50% of $500 times the days tested, the win percentage meets the setting, and the 7-day window has at least 3 trades. "
        "Six trades in seven days is enough. One or two trades cannot move on to the 30-day test. "
        "The reward scale's goal is that win percentage. "
        "A change that raises the win percentage moves the scale toward the goal. "
        "A change that lowers the win percentage moves the scale away from the goal. "
        "The profit scale starts at $0. The total is $500 times the number of days tested. The goal is 50% of that total. "
        "A change that raises the profit moves that scale toward the goal. "
        "A change that lowers the profit moves that scale away from the goal. "
        "On a delta-volume breakout, raise the volume-delta input for fewer trades and lower it for more trades. "
        + _CHANGE_RULES
    )
    if not heard:
        body += "You speak first. Researcher 2 will answer your reply. "
    elif seat == 1:
        body += "Researcher 1's reply is your prompt. Answer that reply. Researcher 3 will answer you next. "
    else:
        body += "The replies before you are your prompt. Answer them. The manager will answer you next. "
    body += (
        "Propose one variable and one new numeric value. "
        "The justification has to answer what was just said and still cover a more consistent $500 day and a higher win percentage. "
        "The chosen change is run on the 2-minute, 5-minute, and 1-minute engines for ES, NQ, and MNQ. "
        f"Prefer {chosen} when that direction is still open. "
        "Set consistent_day true only when the justification explains a more consistent $500 day. "
        "Set higher_win_rate true only when it explains a higher win percentage. "
        "direction is up when the new value is higher than the default, otherwise down.\n"
    )
    if heard:
        body += "Answer this:\n" + "\n".join(
            f"Researcher {index + 1}: {speech}" for index, speech in enumerate(heard)
        ) + "\n"
    body += f"Inputs:\n" + "\n".join(lines) + "\n"
    body += _tried_line(tried)
    body += f"Closed for this stage: {', '.join(shut)}.\n"
    if note:
        body += note + "\n"
    return body + '{"variable":"", "value":0, "direction":"up", "consistent_day":false, "higher_win_rate":false, "justification":""}'


def manager_prompt(proposals: list[Proposal], note: str = "", spoken: list[str] | None = None, tried=None) -> str:
    rows = []
    for index, proposal in enumerate(proposals):
        rows.append(
            f"{index}: {proposal.variable} {proposal.value} {proposal.direction}. {proposal.justification}"
        )
    body = (
        "Pick the one proposal that best supports both a more consistent $500 day and a higher win percentage. "
        "You are answering the three researchers. Their replies are your prompt. "
        "Every session needs at least one trade. "
        "Once a session is up $500, later trades do not count. A session counts at most two losing trades. "
        "Move on only when the profit reaches 50% of $500 times the days tested, the win percentage meets the setting, and the 7-day window has at least 3 trades. "
        "The reward scale's goal is that win percentage. "
        "A change that raises the win percentage moves the scale toward the goal. "
        "A change that lowers the win percentage moves the scale away from the goal. "
        "The profit scale starts at $0. The total is $500 times the number of days tested. The goal is 50% of that total. "
        "A change that raises the profit moves that scale toward the goal. "
        "A change that lowers the profit moves that scale away from the goal. "
        "On a delta-volume breakout, raise the volume delta for fewer trades and lower it for more trades. "
        + _CHANGE_RULES
        + "That one change is how the script will be run on the 2-minute, 5-minute, and 1-minute engines for ES, NQ, and MNQ. "
        "Choose only 0, 1, or 2. Do not invent a variable that is not listed.\n"
    )
    heard = [item.strip() for item in (spoken or []) if str(item).strip()]
    if heard:
        body += "Answer this:\n" + "\n".join(
            f"Researcher {index + 1}: {speech}" for index, speech in enumerate(heard)
        ) + "\n"
    if note:
        body += note + "\n"
    body += _tried_line(tried)
    return body + "\n".join(rows) + '\n{"choice":0}'


def parse_proposal(text: str, inputs: list[ScriptInput]) -> Proposal:
    data = _object(text) or {}
    known = {item.name: item for item in inputs}
    spoken = visible_text(text)
    name = str(data.get("variable") or "").strip()
    item = known.get(name)
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        value = None
    if item is None or value is None or value == item.default:
        named, named_value = _named_value(spoken, known)
        if named:
            name = named
            item = known[name]
            value = named_value
    if item is None or value is None or value == item.default:
        return Proposal(name, data.get("value") if value is None else value, "", False, False, "")
    if item.kind == "int":
        value = int(value)
    direction = "up" if float(value) > item.default else "down"
    justification = " ".join(str(data.get("justification") or "").split()) or spoken[:500]
    day_in_text, win_in_text = _covers(justification)
    return Proposal(
        name,
        value,
        direction,
        _flag(data.get("consistent_day")) or day_in_text,
        _flag(data.get("higher_win_rate")) or win_in_text,
        justification,
    )


def parse_choice(text: str) -> int | Proposal:
    data = _object(text) or {}
    if "choice" in data:
        try:
            return int(data["choice"])
        except (TypeError, ValueError):
            return -1
    return Proposal(
        str(data.get("variable") or ""),
        data.get("value"),
        str(data.get("direction") or ""),
        True,
        True,
        str(data.get("justification") or "outside the three"),
    )


def _object(text: str) -> dict | None:
    raw = _THINK.sub("", text or "")
    decoder = json.JSONDecoder()
    found = None
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found = value
    return found


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _covers(text: str) -> tuple[bool, bool]:
    low = " ".join((text or "").lower().split())
    consistent = any(piece in low for piece in ("consistent", "$500", "500 day", "five hundred", "daily target"))
    win = any(piece in low for piece in ("win percentage", "win rate", "higher win", "more wins"))
    return consistent, win


def _named_value(text: str, known: dict[str, ScriptInput]) -> tuple[str, float | None]:
    low = text.lower()
    for name, item in known.items():
        index = low.find(name.lower())
        if index < 0:
            continue
        window = text[index:index + 80]
        for match in re.finditer(r"-?\d+(?:\.\d+)?", window):
            value = float(match.group())
            if value != item.default:
                return name, value
    return "", None
