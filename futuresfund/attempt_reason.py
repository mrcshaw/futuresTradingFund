"""After a measured attempt, the chart researcher decides the next change.

The mechanical search is the fallback when the model is down or the reply is
not a usable change. One existing input moves, or one library indicator is
added, or the script's entry changes. The numbers in the prompt are the
engine's measurements, including whether the last change moved closer or farther.
"""

from __future__ import annotations

import re
from pathlib import Path

_BECAUSE = re.compile(r"^BECAUSE:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SETTING = re.compile(r"^SETTING:\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_INDICATOR = re.compile(r"^INDICATOR:\s*((?:ta\.)?[A-Za-z_][\w.]*)(?:\s+(\d+))?\s*$", re.IGNORECASE | re.MULTILINE)
_SCRIPT = re.compile(r"^SCRIPT:[ \t]*\n(.*?)(?:\nEND[ \t]*$|\Z)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
_FENCE = re.compile(r"^```[a-z]*\n|\n```$", re.IGNORECASE)
_INPUT = re.compile(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*input\.(?:int|float|bool|string)\b")
_DESK_BLOCK = re.compile(
    r"(?m)^// desk indicator [^\n]*\n(?:_desk_gate = [^\n]*\n)?(?:_desk_long = [^\n]*\n)?(?:_desk_short = [^\n]*\n)?"
)
_LIBRARY = {
    "sma": "ta.sma", "ema": "ta.ema", "rma": "ta.rma", "wma": "ta.wma", "vwma": "ta.vwma",
    "rsi": "ta.rsi", "mom": "ta.mom", "roc": "ta.roc", "change": "ta.change", "rising": "ta.rising",
}


def reasoned_change(script: str, trials: list[dict], bounds: dict | None = None, choices: dict | None = None) -> dict | None:
    """One change chosen from the last attempt. None leaves the mechanical search in charge."""
    if not trials:
        return None
    last = trials[-1]
    if not isinstance(last, dict) or last.get("error") or last.get("fatal") or last.get("stopped"):
        return None
    current = dict(last.get("params") or {})
    if not current:
        return None
    reply = _ask(_brief(script, trials, current))
    if not reply or reply.startswith("The meeting model could not answer"):
        return None
    return parse_reasoned_reply(reply, current, bounds or {}, choices or {}, script)


def parse_reasoned_reply(reply: str, current: dict, bounds: dict | None = None, choices: dict | None = None, script: str = "") -> dict | None:
    """A BECAUSE line plus one SETTING, one INDICATOR, or a SCRIPT block. Anything else is refused."""
    text = reply or ""
    because = _BECAUSE.search(text)
    if because is None:
        return None
    reason = " ".join(because.group(1).split())
    if not reason:
        return None
    revised = _script_block(text)
    setting = _SETTING.search(text)
    indicator = _INDICATOR.search(text)
    chosen = sum(item is not None for item in (setting, indicator, revised))
    if chosen != 1:
        return None
    params = None
    if setting is not None:
        params = _apply_setting(current, setting.group(1), setting.group(2), bounds or {}, choices or {})
        if params is None:
            return None
    elif indicator is not None:
        revised = add_library_indicator(script, indicator.group(1), indicator.group(2))
        if revised is None:
            return None
    elif script and not _keeps_inputs(script, revised):
        return None
    if revised is None and params is None:
        return None
    return {"because": reason[:500], "params": params, "script": revised}


def _keeps_inputs(original: str, revised: str) -> bool:
    """A rewrite may call a library indicator. It may not declare an input the script did not have."""
    return set(_INPUT.findall(revised or "")) <= set(_INPUT.findall(original or ""))


def _indicator_call(name: str) -> str | None:
    text = (name or "").strip().lower()
    if text.startswith("ta."):
        text = text[3:]
    return _LIBRARY.get(text)


def add_library_indicator(source: str, name: str, length: str | None = None) -> str | None:
    """Gate entries with one library indicator. The length is a literal, not a new input."""
    call = _indicator_call(name)
    if not source or not call or "strategy.entry" not in source:
        return None
    try:
        span = int(length) if length else 20
    except (TypeError, ValueError):
        return None
    span = max(2, min(200, span))
    kind = call.split(".", 1)[1]
    text = _DESK_BLOCK.sub("", source).replace(" and _desk_long", "").replace(" and _desk_short", "")
    sides = set()
    if re.search(r"strategy\.long\b", text):
        sides.add("long")
    if re.search(r"strategy\.short\b", text):
        sides.add("short")
    if not sides:
        return None
    if kind == "rising":
        gate = None
        long_expr = f"ta.rising(close, {span})"
        short_expr = f"ta.falling(close, {span})"
    elif kind == "rsi":
        gate = f"ta.rsi(close, {span})"
        long_expr = "_desk_gate > 50"
        short_expr = "_desk_gate < 50"
    elif kind in {"mom", "roc", "change"}:
        gate = f"{call}(close, {span})"
        long_expr = "_desk_gate > 0"
        short_expr = "_desk_gate < 0"
    else:
        gate = f"{call}(close, {span})"
        long_expr = "close > _desk_gate"
        short_expr = "close < _desk_gate"
    block = [f"// desk indicator {call}"]
    if gate:
        block.append(f"_desk_gate = {gate}")
    if "long" in sides:
        block.append(f"_desk_long = {long_expr}")
    if "short" in sides:
        block.append(f"_desk_short = {short_expr}")
    lines = text.splitlines()
    if not _attach_gates(lines, sides):
        return None
    revised = "\n".join(_insert_block(lines, block))
    if revised.strip() == (source or "").strip():
        return None
    return accept_script(revised)


def _insert_block(lines: list[str], block: list[str]) -> list[str]:
    entry_at = next(index for index, line in enumerate(lines) if "strategy.entry" in line)
    insert_at = entry_at
    for index in range(entry_at - 1, max(-1, entry_at - 8), -1):
        prev = lines[index].strip()
        if not prev or prev.startswith("//"):
            continue
        if prev.startswith("if ") or prev.startswith("if("):
            insert_at = index
        break
    return lines[:insert_at] + block + lines[insert_at:]


def _attach_gates(lines: list[str], sides: set[str]) -> bool:
    attached = False
    for index, line in enumerate(lines):
        if "strategy.entry" not in line:
            continue
        if re.search(r"strategy\.long\b", line) and "long" in sides:
            token = "_desk_long"
        elif re.search(r"strategy\.short\b", line) and "short" in sides:
            token = "_desk_short"
        else:
            continue
        if _and_token(lines, index, token):
            attached = True
    return attached


def _and_token(lines: list[str], index: int, token: str) -> bool:
    stripped = lines[index].strip()
    if stripped.startswith("if ") or stripped.startswith("if("):
        if token not in lines[index]:
            lines[index] = lines[index].rstrip() + f" and {token}"
        return True
    for earlier in range(index - 1, max(-1, index - 8), -1):
        prev = lines[earlier].strip()
        if not prev or prev.startswith("//"):
            continue
        if prev.startswith("if ") or prev.startswith("if("):
            if token not in lines[earlier]:
                lines[earlier] = lines[earlier].rstrip() + f" and {token}"
            return True
        return False
    return False


def accept_script(source: str) -> str | None:
    """The rewritten script, after the shared risk block, when the engine can run it."""
    from futuresfund.strategy_team import pine_problems, repair_pine

    revised = repair_pine(_strip_fence(source or ""))
    if "strategy(" not in revised or pine_problems(revised):
        return None
    return revised


def _script_block(text: str) -> str | None:
    found = _SCRIPT.search(text or "")
    if found is None:
        return None
    body = _strip_fence(found.group(1)).strip()
    if "strategy(" not in body:
        return None
    return body


def _strip_fence(text: str) -> str:
    return _FENCE.sub("", text or "").strip()


def _apply_setting(current: dict, name: str, raw: str, bounds: dict, choices: dict) -> dict | None:
    from futuresfund.learn import FROZEN

    key = name.strip()
    if key not in current or key in FROZEN or key.startswith("_"):
        return None
    value = _typed(current.get(key), raw.strip().strip("\"'"), choices.get(key) or [])
    if value is None or value == current.get(key):
        return None
    low, high = bounds.get(key, (None, None))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if low is not None and value < low:
            return None
        if high is not None and value > high:
            return None
    plan = dict(current)
    plan[key] = value
    return plan


def _typed(current, raw: str, options: list):
    if isinstance(current, bool):
        lowered = raw.lower()
        if lowered in {"true", "yes", "on"}:
            return True
        if lowered in {"false", "no", "off"}:
            return False
        return None
    if isinstance(current, str):
        if options and raw not in options:
            return None
        return raw
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            number = float(raw)
        except ValueError:
            return None
        if number != int(number):
            return None
        return int(number)
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _ask(prompt: str) -> str:
    from futuresfund.llm import CONTEXT_CAP, _post, model_name

    messages = [
        {
            "role": "system",
            "content": (
                "You are the chart researcher. A backtest just finished. "
                "Decide the one next change that moves the script toward the acceptance line. "
                "Use only the measured numbers in the user message. Do not invent a profit, a win rate, or a trade count. "
                "The scale in the user message says whether the last change moved closer to the goal or farther from it. Do not repeat a change that moved farther. "
                "Reply with a BECAUSE line, then exactly one of a SETTING line, an INDICATOR line, or a SCRIPT block closed by a line that says END. "
                "A SETTING changes one existing input. The stop starts at $50 and the take profit starts at $2,000. "
                "If that run is profitable, do not change the stop or the take profit. "
                "If it loses, the stop rises by $100 until its cap, then the take profit drops by $100. "
                "After that, change an indicator. Do not move the stop or the take profit away from the most profitable pair. "
                "A win rate under the line can shorten a length. "
                "Read the comment that starts How this script works and follow that description of this script. "
                "A small stop needs a short trade only when that description says the entry is an average cross. "
                "Then a smaller length tracks price more closely and crosses sooner, so the trade can finish before that stop. "
                "An INDICATOR adds one indicator from the library, for example INDICATOR: ta.ema 20. That is not a new input. "
                "A SCRIPT is the whole Pine strategy when the entry itself is why the result missed. It cannot add an input the script does not already have. "
                "Do not change the point value. The instrument roots are ES, GC, NQ, and MNQ. MNQ is $2 a point. Gold is GC. Never write GOLD."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    return _post(model_name("Chart researcher"), messages, think=True, num_ctx=12288, timeout=240)


def _brief(script: str, trials: list[dict], current: dict) -> str:
    from futuresfund.objective import acceptance_line

    last = trials[-1]
    lines = [
        acceptance_line(),
        "A trade down more than $1,500 stays off. The dollar stop stays at or under $1,500 and the take profit at or under $2,000, and the take profit stays larger than the stop.",
        "Flatten at 16:45 New York. Do not move that close to add trades.",
        "130 to 600 trades a year is the pace. More than 600 stays off the list.",
        "Change one thing. If the last run took no trades, or the win rate and the profit factor cannot move by another length, replace the entry. Otherwise change the one input that the last result says is the miss.",
        "The stop starts at $50 and the take profit at $2,000. A profitable opening is kept. A losing opening raises the stop by $100, then lowers the take profit by $100. Indicator changes start from the most profitable pair.",
        "A win rate under the line can shorten a length.",
        "Read the comment that starts How this script works and follow that description of this script. A small stop needs a short trade only when that description says the entry is an average cross. Then a smaller length tracks price more closely and crosses sooner, so the trade can finish before that stop.",
        "Do not invent an input. Do not change the point value. The instrument roots are ES, GC, NQ, and MNQ. MNQ is $2 a point. Gold is GC. Never write GOLD.",
        _library_line(),
        _scale_line(trials),
        "",
        "Measured attempts, oldest first:",
    ]
    for row in trials[-6:]:
        lines.append(_attempt_line(row))
    lines.append("")
    lines.append("Current settings: " + ", ".join(f"{key}={current[key]}" for key in list(current)[:24]))
    lines.append(_miss(last))
    lines.append("")
    lines.append("Script:")
    lines.append(_excerpt(script))
    lines.append("")
    lines.append("Reply in this shape:")
    lines.append("BECAUSE: one or two sentences that name the measured miss and the change.")
    lines.append("SETTING: length = 21")
    lines.append("or, to add one library indicator:")
    lines.append("INDICATOR: ta.ema 20")
    lines.append("or, when the entry is the problem:")
    lines.append("SCRIPT:")
    lines.append("//@version=5")
    lines.append("strategy(...)")
    lines.append("END")
    return "\n".join(lines)


def _library_line() -> str:
    names = ", ".join(sorted(set(_LIBRARY.values())))
    return (
        "You may add one of these indicators from the library when it would move the result toward the goal: "
        f"{names}. That indicator is not a new input."
    )


def _scale_line(trials: list[dict]) -> str:
    """Whether the last measured change moved toward the acceptance goal."""
    if len(trials) < 2:
        return "The scale has not moved yet. This result is the first measurement."
    from futuresfund.learn import ACCOUNT_SIZE
    from futuresfund.objective import scale_move

    move = scale_move(trials[-2], trials[-1], ACCOUNT_SIZE)
    sentence = {
        "closer": "The last change moved closer to the goal.",
        "farther": "The last change moved farther from the goal. Do not repeat it.",
        "flat": "The last change left the scale flat. It did not move closer to the goal.",
    }
    return sentence.get(move, "The last change left the scale flat.")


def _attempt_line(row: dict) -> str:
    change = row.get("change") or "starting settings"
    return (
        f"- {change}: profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, "
        f"trades {row.get('trades')}, win rate {row.get('win_rate')}, profit factor {row.get('profit_factor')}, "
        f"largest loss {row.get('biggest_loser')}"
    )


def _miss(row: dict) -> str:
    trades = row.get("trades")
    try:
        count = int(trades)
    except (TypeError, ValueError):
        count = None
    if count == 0:
        return "The last run took no trades. The entry did not pass. Change the entry, not another length."
    parts = []
    if row.get("win_rate") is not None:
        parts.append(f"win rate {row.get('win_rate')}")
    if row.get("profit_factor") is not None:
        parts.append(f"profit factor {row.get('profit_factor')}")
    if row.get("net_profit") is not None:
        parts.append(f"profit {row.get('net_profit')}")
    if not parts:
        return "The last run is the result to learn from."
    return "The last run measured " + ", ".join(parts) + "."


def _excerpt(script: str) -> str:
    text = script or ""
    if len(text) <= 6000:
        return text or "(no script text)"
    return text[:3000] + "\n...\n" + text[-2500:]


def install_script(path: Path, source: str, execute) -> str | None:
    """Write a checked script and point the running backtest at it."""
    revised = accept_script(source)
    if revised is None or path is None:
        return None
    try:
        path.write_text(revised, encoding="utf-8")
    except OSError:
        return None
    holder = getattr(execute, "source", None)
    if isinstance(holder, dict):
        holder["text"] = revised
        holder["stamp"] = int(holder.get("stamp") or 0) + 1
    return revised
