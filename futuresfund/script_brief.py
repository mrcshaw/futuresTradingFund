"""The Trading Analyst describes the script that is about to be backtested.

The description follows that script. An average cross is one case, and the
short-trade reading belongs there only when the code enters on a cross. The
script checker compares the description with the code and sends it back when
the two do not match.
"""

from __future__ import annotations

import re

from futuresfund.frequency import (
    MECHANISM,
    SMALL_STOP,
    _quoted_mechanism,
    _strip_mechanism,
    _uses_cross,
    install_mechanism,
    mechanism_lines,
    mechanism_note,
    stop_dollars,
)

ANALYST = "Trading Analyst"
CHECKER = "Script checker"


def prepare_description(source: str, board=None) -> str:
    """Write an accepted description above the script. A description that does not match is left off."""
    text = source or ""
    if _quoted_mechanism(text) and not _is_template(text):
        _say(board, ANALYST, mechanism_note(text))
        return text
    if _is_template(text):
        text = _strip_mechanism(text)
    _working(board, ANALYST, "Reading the script before the backtest", True)
    lines = _analyst_lines(text)
    _working(board, ANALYST, "Reading the script before the backtest", False)
    if not lines:
        _say(board, ANALYST, "This script was not described. A description that was not read from the file is not written above it.")
        return text
    _working(board, CHECKER, "Checking the description against the script", True)
    problem = description_problem(text, lines)
    _working(board, CHECKER, "Checking the description against the script", False)
    if problem:
        _say(board, CHECKER, f"The description was sent back. {problem}")
        return text
    _say(board, ANALYST, " ".join(lines))
    _say(board, CHECKER, "The description matches this script.")
    return install_mechanism(text, lines)


def description_problem(source: str, lines: list[str]) -> str:
    """Why the description does not match, or empty when the checker accepts it."""
    found = _code_problem(source, lines)
    if found:
        return found
    return _checker_problem(source, lines)


def _is_template(source: str) -> bool:
    quoted = _quoted_mechanism(source)
    return bool(quoted) and quoted == " ".join(mechanism_lines(source))


def _analyst_lines(source: str) -> list[str]:
    reply = _ask(ANALYST, _analyst_system(), _analyst_prompt(source))
    return _sentences(reply)


def _checker_problem(source: str, lines: list[str]) -> str:
    reply = _ask(CHECKER, _checker_system(), _checker_prompt(source, lines))
    text = " ".join((reply or "").split())
    if not text or text.startswith("The meeting model could not answer"):
        return "The script checker could not read the description."
    if text.upper().startswith("ACCURATE"):
        return ""
    if text.upper().startswith("ERROR"):
        detail = text.split(":", 1)[-1].strip()
        return detail or "The description does not match the script."
    return "The script checker did not accept the description."


def _code_problem(source: str, lines: list[str]) -> str:
    blob = " ".join(lines).strip()
    if not blob:
        return "The description is empty."
    lowered = blob.lower()
    cross = _uses_cross(source)
    if _claims_cross(lowered) and not cross:
        return "The description says the entry is a cross. This script does not enter on a cross."
    if cross and not re.search(r"\bcross", lowered):
        return "The entry is a moving-average cross. The description does not say that."
    stop = stop_dollars(source)
    if cross and stop is not None and stop <= SMALL_STOP and "tracks price" not in lowered:
        shown = int(stop) if float(stop).is_integer() else stop
        return (
            f"The stop is ${shown} and the entry is a cross. "
            "The description has to say a smaller length tracks price more closely so the trade can finish before that stop."
        )
    for name, pattern in _CLAIMS:
        if re.search(pattern, lowered) and not re.search(pattern, source or "", re.I):
            return f"The description mentions {name}. This script does not."
    wrong_stop = _wrong_amount(source, blob)
    if wrong_stop:
        return wrong_stop
    return ""


_CLAIMS = (
    ("RSI", r"\brsi\b"),
    ("MACD", r"\bmacd\b"),
    ("VWAP", r"\bvwap\b"),
    ("Bollinger", r"bollinger"),
    ("Supertrend", r"supertrend"),
    ("stochastic", r"\bstoch"),
)


def _claims_cross(lowered: str) -> bool:
    return any(
        phrase in lowered
        for phrase in (
            "moving-average cross",
            "moving average cross",
            "average cross",
            "ema cross",
            "sma cross",
            "crossover",
            "crosses sooner",
            "crosses more often",
        )
    )


def _wrong_amount(source: str, blob: str) -> str:
    stop = stop_dollars(source)
    allowed = set()
    if stop is not None:
        allowed.add(int(stop))
    target = re.search(r"tp_dollars\s*=\s*input\.(?:int|float)\s*\(\s*(\d+)", source or "", re.I)
    if target:
        allowed.add(int(target.group(1)))
    for match in re.finditer(r"\$(\d+)", blob):
        amount = int(match.group(1))
        if allowed and amount not in allowed:
            return f"The description says ${amount}. That amount is not the stop or the take profit in this script."
        if not allowed:
            return f"The description says ${amount}. This script does not set a dollar stop."
    return ""


def _sentences(reply: str) -> list[str]:
    text = (reply or "").strip()
    if not text or text.startswith("The meeting model could not answer"):
        return []
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text, flags=re.I).strip()
    rows = []
    for row in text.splitlines():
        cleaned = re.sub(r"^//\s*", "", row.strip())
        if cleaned.lower().startswith(MECHANISM.lower()):
            continue
        if cleaned:
            rows.append(cleaned)
    text = " ".join(rows)
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    return parts[:8]


def _analyst_system() -> str:
    return (
        "You are the Trading Analyst. Read the script and describe how that script works. "
        "Write three to six sentences. Name the entry the code uses and the stop when the code has one. "
        "An EMA crossover is an example, not the default. "
        "Describe a moving-average cross only when the script enters on ta.crossover or ta.crossunder of an average. "
        "When that is the entry and the stop is small, say the trade has to be short, and a smaller length tracks price more closely and crosses sooner, so the trade can finish before that stop. "
        "When the entry is not a cross, describe the conditions that open the trade. Do not mention a crossover. "
        "Do not invent an indicator, a session, or a dollar amount the script does not have. "
        "Reply with the sentences only."
    )


def _analyst_prompt(source: str) -> str:
    return "Script:\n" + _excerpt(source)


def _checker_system() -> str:
    return (
        "You are the Script checker. Compare the description with the script. "
        "Accept it only when every claim is in the code and the sentences are free of errors. "
        "An EMA crossover description is accurate only when the script enters on a cross of an average. "
        "Reply with one line: ACCURATE or ERROR: one sentence naming what is wrong."
    )


def _checker_prompt(source: str, lines: list[str]) -> str:
    return "Description:\n" + " ".join(lines) + "\n\nScript:\n" + _excerpt(source)


def _excerpt(source: str) -> str:
    text = source or ""
    if len(text) <= 8000:
        return text or "(no script text)"
    return text[:4000] + "\n...\n" + text[-3000:]


def _ask(agent: str, system: str, prompt: str) -> str:
    from futuresfund.llm import CONTEXT_CAP, _post, _reasons, model_name

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    return _post(
        model_name(agent),
        messages,
        num_ctx=CONTEXT_CAP,
        timeout=180 if _reasons(agent) else 90,
        think=_reasons(agent),
    )


def _say(board, agent: str, text: str) -> None:
    if board is not None and text:
        board.post(agent, text, kind="report", channel=agent)


def _working(board, agent: str, task: str, working: bool) -> None:
    if board is not None:
        board.set_task(agent, task, working=working)
