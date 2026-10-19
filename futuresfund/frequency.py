"""When a result is far under 4 trades a week, read this script and change its entry.

The guideline the user edits is researchNotes/how_to_research.md. This module
applies that method to the script that is open: a short session is widened only
if this script gates entries on the clock, and an average length is cut only if
this script enters on a cross. Anything else waits for the condition the entry
actually requires.
"""

from __future__ import annotations

import json
import re

FULL_SESSION = "1800-1600"
FAR_UNDER_WEEK = 2.0

_TIME_SESSION = re.compile(
    r'(time\s*\(\s*timeframe\.period\s*,\s*")(\d{4}-\d{4})(")',
    re.IGNORECASE,
)


def cash_window(token: str) -> bool:
    """A same-day cash window, such as 0930-1600. An overnight window wraps past midnight."""
    if not re.fullmatch(r"\d{4}-\d{4}", token or ""):
        return False
    start, end = token.split("-")
    return start < end and start >= "0700" and end <= "1700"


def found_cash_session(source: str) -> str | None:
    """The first cash-hours session written inside time()."""
    for match in _TIME_SESSION.finditer(source or ""):
        if cash_window(match.group(2)):
            return match.group(2)
    return None


def apply_session(source: str, window: str | None) -> str:
    """Rewrite cash-hours time() sessions. Leave a window that already covers the overnight."""
    if not window:
        return source or ""

    def repl(match):
        if cash_window(match.group(2)):
            return match.group(1) + window + match.group(3)
        return match.group(0)

    return _TIME_SESSION.sub(repl, source or "")


def far_under(row: dict | None) -> bool:
    """True when the measured week is under 2 trades.

    A finished run with no trades is under that floor even when the day count is missing.
    Unknown days on a run that did trade are not treated as far under.
    """
    from futuresfund.objective import numbers, trades_per_week

    found = numbers(row)
    if found is not None and found[2] < 1:
        return True
    weekly = trades_per_week(row)
    return weekly is not None and weekly < FAR_UNDER_WEEK


def is_average_length(name: str, source: str = "") -> bool:
    """An EMA or SMA length, or a fast/slow length on a script that uses those averages."""
    lowered = (name or "").lower().replace("_", "")
    if any(word in lowered for word in ("stop", "target", "profit", "dollar", "tick", "qty", "cooldown", "session", "lookback", "profile")):
        return False
    if "ema" in lowered or "sma" in lowered:
        return True
    if "fast" in lowered or "slow" in lowered:
        text = (source or "").lower()
        return "ema" in text or "sma" in text
    return False


def shorter_length(value, low) -> int | None:
    """About half the current length, and never below the input minimum."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    floor = int(low) if isinstance(low, (int, float)) and not isinstance(low, bool) else 1
    floor = max(1, floor)
    nxt = max(floor, int(value) // 2)
    if nxt >= int(value):
        nxt = max(floor, int(value) - 1)
    if nxt == int(value) or nxt <= 0:
        return None
    return nxt


MECHANISM = "How this script works"
SMALL_STOP = 750


def stop_dollars(source: str) -> float | None:
    """The dollar stop written on this script."""
    match = re.search(
        r"stop_dollars\s*=\s*input\.(?:int|float)\s*\(\s*(\d+(?:\.\d+)?)",
        source or "",
        re.I,
    )
    if match is None:
        return None
    return float(match.group(1))


def mechanism_lines(source: str) -> list[str]:
    """How this script trades, including what a small stop requires of the entry."""
    lines = ["The Trading Analyst read this script before the backtest."]
    if _uses_cross(source):
        lines.append("The entry is a moving-average cross. A shorter length follows price more closely and crosses more often.")
    elif re.search(r"ta\.ema\s*\(|ta\.sma\s*\(", source or "", re.I):
        lines.append("An average in this script filters the entry. It is not a cross, so its length is not the first change.")
    else:
        lines.append("The entry is the conditions written in this script.")
    stop = stop_dollars(source)
    if stop is None:
        return lines
    shown = int(stop) if float(stop).is_integer() else stop
    if stop <= SMALL_STOP and _uses_cross(source):
        lines.append(
            f"The stop is ${shown}. A small stop is hit unless the trade is quick. "
            "A smaller average tracks price more closely and crosses sooner, so the trade can finish before that stop."
        )
    elif stop <= SMALL_STOP:
        lines.append(
            f"The stop is ${shown}. A small stop is hit unless the trade works quickly. "
            "Change the condition that opens the trade. A longer filter does not make the trade faster."
        )
    else:
        lines.append(
            f"The stop is ${shown}. That loss can last longer, so the entry does not have to cross on every small turn."
        )
    return lines


def mechanism_note(source: str) -> str:
    """The description already written above the script."""
    return _quoted_mechanism(source)


def install_mechanism(source: str, lines: list[str] | None = None) -> str:
    """Put the accepted description above the script. No lines leaves the file unchanged."""
    text = source or ""
    if not lines:
        return text
    text = _strip_mechanism(text)
    comment = "\n".join(f"// {line}" for line in [MECHANISM, *lines])
    rows = text.splitlines()
    index = 1 if rows and rows[0].startswith("//@version") else 0
    if index < len(rows) and rows[index].startswith("// Built from:"):
        index += 1
    rows[index:index] = comment.splitlines() + [""]
    body = "\n".join(rows)
    if text.endswith("\n") or not text:
        body += "\n"
    return body


def _strip_mechanism(source: str) -> str:
    rows = []
    skipping = False
    for row in (source or "").splitlines():
        if row.strip() == f"// {MECHANISM}":
            skipping = True
            continue
        if skipping:
            if row.strip().startswith("//"):
                continue
            skipping = False
            if not row.strip():
                continue
        rows.append(row)
    body = "\n".join(rows)
    if (source or "").endswith("\n"):
        body += "\n"
    return body


def _quoted_mechanism(source: str) -> str:
    lines = []
    capture = False
    for line in (source or "").splitlines():
        stripped = line.strip()
        if stripped == f"// {MECHANISM}":
            capture = True
            continue
        if not capture:
            continue
        if not stripped.startswith("//"):
            break
        lines.append(stripped[2:].strip())
    return " ".join(line for line in lines if line)


def script_reading(source: str, params: dict | None = None) -> str:
    """What this script is trying to do, taken from the file itself."""
    params = params or {}
    title = _title(source)
    parts = []
    if title:
        parts.append(f"{title}.")
    purpose = _purpose(source)
    if purpose:
        parts.append(purpose)
    window = _window_sentence(source, params)
    if window:
        parts.append(window)
    quoted = _quoted_mechanism(source)
    if quoted:
        parts.append(quoted)
    elif _uses_cross(source):
        parts.append("The entry is a moving-average cross. A shorter length follows price more closely and crosses more often.")
    elif re.search(r"ta\.ema\s*\(|ta\.sma\s*\(", source or "", re.I):
        parts.append("An average in this script filters the entry. It is not a cross, so its length is not the first change.")
    if not parts:
        parts.append("The script did not state an entry. The next change stays on one input.")
    return " ".join(parts)


def frequency_plan(origin: dict, trials: list, source: str, bounds: dict | None = None, seen: set | None = None, carried: dict | None = None, spent: set | None = None) -> dict | None:
    """One change that follows from this script, or None when the week is not far under the floor."""
    if not trials or not far_under(trials[-1]):
        return None
    bounds = bounds or {}
    current = dict(carried or (trials[-1].get("params") or origin) or {})
    used = set(seen or ())
    moved = _moved_gate(trials)
    if moved and _pace_improved(trials):
        plan = _apply(current, moved, bounds, source)
        if plan is not None and _key(plan) not in used:
            return plan
    skip = {moved} if moved and not _pace_improved(trials) else set()
    skip.update(spent or ())
    for name in _gates(source, current):
        if name in skip:
            continue
        plan = _apply(current, name, bounds, source)
        if plan is not None and _key(plan) not in used:
            return plan
    return None


def frequency_reason(before: dict | None, plan: dict, source: str) -> str:
    """Why this attempt follows from the script that is open."""
    before = before or {}
    changed = [key for key in plan if plan.get(key) != before.get(key)]
    name = changed[0] if len(changed) == 1 else ""
    if name == "session_window":
        found = found_cash_session(source) or "0930-1600"
        start, end = _clock_pair(found)
        return (
            f"This result is far under 4 trades a week. This script enters only from {start} to {end} New York. "
            f"The futures session opens at 18:00, so the next run uses {FULL_SESSION}, 18:00 to 16:00."
        )
    if name == "sess_start_hr":
        return (
            f"This result is far under 4 trades a week. This script enters only from {_hour_clock(before, 'start')} to {_hour_clock(before, 'end')} New York. "
            "The futures session opens at 18:00, so the next run starts that window at 18:00 and keeps the afternoon end."
        )
    if name and is_average_length(name, source) and _uses_cross(source):
        reason = (
            f"This script enters on a moving-average cross. {name} {before.get(name)} to {plan.get(name)} "
            "follows price more closely, so the cross can print more often."
        )
        stop = stop_dollars(source)
        if stop is not None and stop <= SMALL_STOP:
            shown = int(stop) if float(stop).is_integer() else stop
            reason += (
                f" The stop is ${shown}, so the trade has to be short enough to finish before that stop."
            )
        return reason
    if name:
        return (
            f"This result is far under 4 trades a week. This script requires {name} before an entry. "
            f"The next run moves it from {before.get(name)} to {plan.get(name)}."
        )
    return "This result is far under 4 trades a week. The next change is the condition in this script that keeps bars from entering."


_CROSS = re.compile(r"ta\.crossover\s*\(|ta\.crossunder\s*\(|ta\.cross\s*\(", re.I)
_TITLE = re.compile(r'strategy\s*\(\s*"([^"]+)"', re.I)


def _title(source: str) -> str:
    match = _TITLE.search(source or "")
    return match.group(1).strip() if match else ""


def _purpose(source: str) -> str:
    """The comment lines where the script says what the entry is."""
    kept = []
    for line in (source or "").splitlines():
        text = line.strip().lstrip("/").strip()
        if not text or text.startswith("="):
            continue
        lowered = text.lower()
        if lowered.startswith("long:") or lowered.startswith("short:") or "entry:" in lowered:
            kept.append(text.rstrip("."))
        if len(kept) == 2:
            break
    return " ".join(kept)


def _uses_cross(source: str) -> bool:
    """True when the entry is a cross. A negated cross, such as not ta.crossover, is a filter."""
    text = source or ""
    lowered = text.lower()
    if "ema" not in lowered and "sma" not in lowered:
        return False
    for match in _CROSS.finditer(text):
        prefix = text[max(0, match.start() - 12):match.start()]
        if re.search(r"\bnot\s*$", prefix, re.I):
            continue
        return True
    return False


def _day_session(source: str, params: dict) -> bool:
    """True when this script allows entries only inside a daytime hour window."""
    if "sess_start_hr" not in params or "sess_end_hr" not in params:
        return False
    if params.get("use_session") is False:
        return False
    if not re.search(r"\bin_session\b|\binSession\b", source or ""):
        return False
    try:
        start = int(params["sess_start_hr"])
        end = int(params["sess_end_hr"])
    except (TypeError, ValueError):
        return False
    return start < end and start < 18


def _window_sentence(source: str, params: dict) -> str:
    found = found_cash_session(source)
    if found and params.get("session_window") != FULL_SESSION:
        start, end = _clock_pair(found)
        return f"Entries are allowed from {start} to {end} New York. The futures session opens at 18:00."
    if _day_session(source, params):
        return (
            f"Entries are allowed from {_hour_clock(params, 'start')} to {_hour_clock(params, 'end')} New York. "
            "The futures session opens at 18:00."
        )
    return ""


def _clock_pair(token: str) -> tuple[str, str]:
    start, end = (token or "0930-1600").split("-")
    return _hhmm(start), _hhmm(end)


def _hhmm(digits: str) -> str:
    hour = int(digits[:2])
    minute = int(digits[2:])
    return f"{hour}:{minute:02d}"


def _hour_clock(params: dict, side: str) -> str:
    hour = params.get(f"sess_{side}_hr")
    minute = params.get(f"sess_{side}_mn") or 0
    try:
        return f"{int(hour)}:{int(minute):02d}"
    except (TypeError, ValueError):
        return "the session hours"


def _gates(source: str, params: dict) -> list[str]:
    """Entry conditions this script actually has, session first, then a cross, then the other filters."""
    names = []
    if found_cash_session(source):
        names.append("session_window")
    if _day_session(source, params):
        names.append("sess_start_hr")
    if _uses_cross(source):
        names.extend(_average_lengths(params, source))
    filters = [
        name for name in params
        if name in (source or "") and _loosen_rank(name) < 9 and not is_average_length(name, source)
    ]
    names.extend(sorted(filters, key=lambda name: (_loosen_rank(name), name.lower())))
    seen = set()
    ordered = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _average_lengths(params: dict, source: str) -> list[str]:
    names = [
        name for name, value in params.items()
        if isinstance(value, int) and not isinstance(value, bool) and is_average_length(name, source) and "slope" not in name.lower()
    ]
    return sorted(names, key=lambda name: (-int(params[name]), name))


def _loosen_rank(name: str) -> int:
    lowered = name.lower()
    if any(word in lowered for word in ("stop", "target", "profit", "dollar", "tick", "qty", "point", "lookback", "profile", "row")):
        return 9
    if "cooldown" in lowered:
        return 0
    if "max" in lowered and "trade" in lowered:
        return 1
    if lowered.startswith("min"):
        return 2
    if "oversold" in lowered or "_os" in lowered:
        return 3
    if "overbought" in lowered or "_ob" in lowered:
        return 4
    if "slope" in lowered and "max" in lowered:
        return 5
    return 9


def _apply(current: dict, name: str, bounds: dict, source: str) -> dict | None:
    nxt = _next_value(name, current.get(name), bounds, source)
    if nxt is None or nxt == current.get(name):
        return None
    plan = dict(current)
    plan[name] = nxt
    return plan


def _next_value(name: str, value, bounds: dict, source: str):
    low, high = bounds.get(name, (None, None))
    if name == "session_window":
        return None if value == FULL_SESSION else FULL_SESSION
    if name == "sess_start_hr":
        try:
            return None if int(value) >= 18 else 18
        except (TypeError, ValueError):
            return None
    if is_average_length(name, source) and _uses_cross(source):
        return shorter_length(value, low)
    return _loosen(name, value, low, high)


def _loosen(name: str, value, low, high):
    if _loosen_rank(name) >= 9 or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    lowered = name.lower()
    if "cooldown" in lowered or lowered.startswith("min"):
        goal = 0 if low is None else low
    elif "overbought" in lowered or "_ob" in lowered:
        goal = 50
    elif "oversold" in lowered or "_os" in lowered:
        goal = 50
    else:
        goal = high if high is not None else (value * 2 if value else 1)
    return _step_toward(value, goal, low, high)


def _step_toward(value, goal, low, high):
    """Halfway from the current value toward the side that lets more bars through."""
    try:
        goal = float(goal)
        current = float(value)
    except (TypeError, ValueError):
        return None
    if low is not None:
        goal = max(float(low), goal)
    if high is not None:
        goal = min(float(high), goal)
    if isinstance(value, int) and not isinstance(value, bool):
        nxt = int(round(current + (goal - current) / 2))
        if nxt == int(value):
            nxt = int(value) + (1 if goal > current else -1)
        if low is not None:
            nxt = max(int(low), nxt)
        if high is not None:
            nxt = min(int(high), nxt)
        return None if nxt == int(value) else nxt
    nxt = round(current + (goal - current) / 2, 4)
    return None if nxt == current else nxt


def _moved_gate(trials: list) -> str | None:
    if len(trials) < 2:
        return None
    before = trials[-2].get("params") or {}
    after = trials[-1].get("params") or {}
    changed = [name for name in set(before) | set(after) if before.get(name) != after.get(name)]
    if len(changed) != 1:
        return None
    return changed[0]


def _pace_improved(trials: list) -> bool:
    from futuresfund.objective import trades_per_week

    if len(trials) < 2:
        return False
    before = trades_per_week(trials[-2])
    after = trades_per_week(trials[-1])
    if before is None or after is None:
        return False
    return after > before + 0.05


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, default=str)
