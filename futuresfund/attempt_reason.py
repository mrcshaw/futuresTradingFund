"""The chart researcher reads a script and decides what to change, in the background.

It is asked when a script is first opened, when settings stop moving the result, and
when a proving stage fails. No engine waits for the answer: the mechanical search keeps
backtesting, and the answer is used the next time a chart opens that script. One existing input moves, or one library indicator is added, or the
script is repaired. After a measured failure it may also redesign the script, which writes a new script
and leaves this one as it was. The numbers in the prompt are the engine's measurements.
"""

from __future__ import annotations

import re
from pathlib import Path

from futuresfund.pine_check import placeholder_problem, rewrite_problem

_BECAUSE = re.compile(r"^BECAUSE:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SETTING = re.compile(r"^SETTING:\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_INDICATOR = re.compile(r"^INDICATOR:\s*((?:ta\.)?[A-Za-z_][\w.]*)(?:\s+(\d+))?\s*$", re.IGNORECASE | re.MULTILINE)
_SCRIPT = re.compile(r"^SCRIPT:[ \t]*\n(.*?)(?:\nEND[ \t]*$|\Z)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
_FENCE = re.compile(r"^```[a-z]*\n|\n```$", re.IGNORECASE)
_INPUT = re.compile(r"(?m)^\s*([A-Za-z_]\w*)\s*=\s*input\.(?:int|float|bool|string)\b")
_REWRITE = re.compile(r"^REWRITE:[ \t]*(.*?)(?:\nEND[ \t]*$|\Z)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
_REDESIGN = re.compile(r"^REDESIGN:[ \t]*(.*?)(?:\nEND[ \t]*$|\Z)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
_KEEP = re.compile(r"^KEEP\b", re.IGNORECASE | re.MULTILINE)
_DESK_BLOCK = re.compile(
    r"(?m)^// desk indicator [^\n]*\n(?:_desk_gate = [^\n]*\n)?(?:_desk_long = [^\n]*\n)?(?:_desk_short = [^\n]*\n)?"
)
# A decision gets the model's full thinking pass. A reply that does not arrive inside
# DECISION_TIMEOUT is dropped. The time covers its turn on the one model.
DECISION_TIMEOUT = 1200
# A script up to this long reaches the model whole. It rewrote scripts it had seen with the
# middle cut out, and wrote a placeholder where the volume profile had been.
SCRIPT_SHOWN = 30000
_LIBRARY = {
    "sma": "ta.sma", "ema": "ta.ema", "rma": "ta.rma", "wma": "ta.wma", "vwma": "ta.vwma",
    "rsi": "ta.rsi", "mom": "ta.mom", "roc": "ta.roc", "change": "ta.change", "rising": "ta.rising",
}


def reasoned_change(script: str, trials: list[dict], bounds: dict | None = None, choices: dict | None = None, why: str = "", response: dict | None = None, redesign: bool = False) -> dict | None:
    """One decision from the measurements so far. None leaves the mechanical search in charge.

    redesign opens a REDESIGN to the researcher. Its answer carries the whole new script under "redesigned".
    """
    if not trials:
        return None
    last = trials[-1]
    if not isinstance(last, dict) or last.get("error") or last.get("fatal") or last.get("stopped"):
        return None
    current = dict(last.get("params") or {})
    if not current:
        return None
    reply = _ask(_brief(script, trials, current, why, response, redesign))
    if not reply or reply.startswith("The meeting model could not answer"):
        return None
    from futuresfund.script_diagnosis import note_decision

    note_decision(script, why, reply)
    found = parse_reasoned_reply(reply, current, bounds or {}, choices or {}, script, redesign)
    if found and found.get("redesign"):
        found["redesigned"] = _write_redesign(script, found["redesign"], found["because"])
        if not found["redesigned"]:
            found["because"] += " The redesign could not be written, so the script is unchanged."
    if found and found.get("rewrite"):
        found["script"] = _write_script(script, found["rewrite"], found["because"])
        if not found["script"]:
            found["because"] += " The rewrite could not be written, so the script is unchanged."
    return found


def _write_script(script: str, change: str, because: str) -> str | None:
    """The second pass: the whole script with the researcher's change in it. The thinking is already done."""
    from futuresfund.llm import CONTEXT_CAP, _post, model_name

    messages = [
        {
            "role": "system",
            "content": (
                "You edit Pine Script strategies. Apply exactly the change you are given and nothing else. "
                "Reply with the whole script, every line kept except what the change needs, after a line that says SCRIPT:, closed by a line that says END. "
                "TradingView has to compile it: plot, plotshape, bgcolor, fill, hline, and input calls stay at the left margin, never inside an if, a for, or a function. "
                "Declare a name above its first use and only once in a block. Change it with :=. Do not name anything range. "
                'time(timeframe.period, session) takes a session such as "0930-1600", not an hour or a timezone alone. '
                "Do not add an input the script does not already have."
            ),
        },
        {"role": "user", "content": f"Why: {because}\n\nThe change:\n{change}\n\nThe script:\n{script}"},
    ]
    refused = ""
    for _ in range(2):
        asked = messages if not refused else [*messages, {"role": "user", "content": f"That script was refused: {refused} Write the whole script again with that fixed."}]
        reply = _post(model_name("Chart researcher"), asked, think=False, num_ctx=CONTEXT_CAP, timeout=DECISION_TIMEOUT, urgent=True)
        revised = _script_block(reply)
        if not revised:
            refused = "It was not a whole script between SCRIPT: and END."
        elif not _keeps_inputs(script, revised):
            refused = "It added an input the script did not have."
        else:
            refused = rewrite_problem(script, revised) or script_refusal(revised, script)
        if not refused:
            return revised
    return None


def _write_redesign(script: str, plan: str, because: str) -> str | None:
    """The second pass: a whole new script from the researcher's plan. Anything in the old script may change except the risk rules."""
    from futuresfund.llm import CONTEXT_CAP, _post, model_name
    from futuresfund.strategy_team import engine_problem

    messages = [
        {
            "role": "system",
            "content": (
                "You write Pine Script strategies. Write a complete new strategy from the plan you are given. "
                "The old script is there to read. Change anything the plan changes: the entries, the filters, the sessions, the exits, and the inputs. Leave out whatever the plan drops. "
                "Reply with the whole script after a line that says SCRIPT:, closed by a line that says END. Never leave part of it out or write a placeholder. "
                "Keep these risk rules. "
                'Stop and take profit are dollar inputs: stop_dollars = input.int(500, "Stop ($)", minval=50, maxval=700) and tp_dollars = input.int(1000, "Take Profit ($)", minval=50), with the take profit larger than the stop. '
                "The stop holds at the entry price for the whole trade: long_stop = strategy.position_size > 0 ? strategy.position_avg_price - stop_dollars / pv : close - stop_dollars / pv, and the same for the short side. "
                "The take profit reads the last close: long_limit = close + tp_dollars / pv and short_limit = close - tp_dollars / pv. Every entry has a strategy.exit with stop= and limit=. "
                "Set the stop and the target in dollars, not with ATR. An ATR stop or target is converted to the dollar stop before the backtest, so its multiplier is removed. "
                "Every trade is closed with strategy.close_all when the session ends at 16:00 New York. "
                "Give each new input a default, a minval, and a maxval, so the search can move it. "
                "TradingView has to compile it: plot, plotshape, bgcolor, fill, hline, and input calls stay at the left margin, never inside an if, a for, or a function. "
                "Declare a name above its first use and only once in a block. Change it with :=. Do not name anything range. "
                'time(timeframe.period, session) takes a session such as "0930-1600", not an hour or a timezone alone.'
            ),
        },
        {"role": "user", "content": f"Why: {because}\n\nThe plan:\n{plan}\n\nThe old script:\n{_excerpt(script)}"},
    ]
    refused = ""
    for _ in range(2):
        asked = messages if not refused else [*messages, {"role": "user", "content": f"That script was refused: {refused} Write the whole script again with that fixed."}]
        reply = _post(model_name("Chart researcher"), asked, think=False, num_ctx=CONTEXT_CAP, timeout=DECISION_TIMEOUT, urgent=True)
        revised = _script_block(reply)
        if not revised:
            refused = "It was not a whole script between SCRIPT: and END."
            continue
        refused = placeholder_problem(script, revised) or script_refusal(revised)
        if not refused:
            accepted = accept_script(revised) or ""
            refused = engine_problem(accepted)
            if accepted and not refused:
                return accepted
    return None


def parse_reasoned_reply(reply: str, current: dict, bounds: dict | None = None, choices: dict | None = None, script: str = "", redesign: bool = False) -> dict | None:
    """A BECAUSE line plus one SETTING, one INDICATOR, a REWRITE, REDESIGN or SCRIPT block, or KEEP. Anything else is refused."""
    text = reply or ""
    because = _BECAUSE.search(text)
    if because is None:
        return None
    reason = " ".join(because.group(1).split())
    if not re.search(r"[A-Za-z]", reason):
        return None
    # The plan comes first: it may name a setting, and that is not a second answer.
    plan = _REDESIGN.search(text)
    if plan and plan.group(1).strip():
        if not redesign:
            return None
        return {"because": reason[:500], "params": None, "script": None, "redesign": plan.group(1).strip()}
    rewrite = _REWRITE.search(text)
    if rewrite and rewrite.group(1).strip():
        if len(script or "") > SCRIPT_SHOWN or _SETTING.search(text) or _INDICATOR.search(text):
            return None
        return {"because": reason[:500], "params": None, "script": None, "rewrite": rewrite.group(1).strip()}
    revised = _script_block(text)
    setting = _SETTING.search(text)
    indicator = _INDICATOR.search(text)
    chosen = sum(item is not None for item in (setting, indicator, revised))
    if chosen == 0 and _KEEP.search(text):
        return {"because": reason[:500], "params": None, "script": None, "keep": True}
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
    elif len(script or "") > SCRIPT_SHOWN:
        # The model saw only part of this script, so it cannot write the whole of it.
        return None
    elif script and (not _keeps_inputs(script, revised) or rewrite_problem(script, revised)):
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
    return accept_script(revised, source)


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


def script_refusal(source: str, original: str = "") -> str:
    """Why a rewrite cannot be used, or blank. A rewrite answers only for problems it brought in, not ones the script already had."""
    from futuresfund.strategy_team import pine_problems, repair_pine

    revised = repair_pine(_strip_fence(source or ""))
    if "strategy(" not in revised:
        return "It has no strategy() call."
    known = set(pine_problems(original)) if original else set()
    return " ".join(problem for problem in pine_problems(revised) if problem not in known)


def accept_script(source: str, original: str = "") -> str | None:
    """The rewritten script, after the shared risk block, when the engine can run it."""
    from futuresfund.strategy_team import repair_pine

    if script_refusal(source, original):
        return None
    return repair_pine(_strip_fence(source or ""))


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
    from futuresfund.llm import decision_reply, model_name

    messages = [
        {
            "role": "system",
            "content": (
                "You are the chart researcher. You find out why a script behaves the way it does, and you fix it. You do not tune blindly. "
                "Read the script and the measurements before you decide. Look for a broken script first: an input nothing reads, "
                "an exit that uses a different input from the one being tested, a trailing stop that runs while its switch is off, "
                "a filter that can never pass, or code that does not do what its description says. Repair broken logic before you change a setting. "
                "The desk tests the stop with stop_dollars and the take profit with tp_dollars. When the script's own exit uses another input for its stop or target, make that exit read stop_dollars and tp_dollars. "
                "Use only the measured numbers in the user message. Do not invent a profit, a win rate, or a trade count. "
                "The scale in the user message says whether the last change moved closer to the goal or farther from it. Do not repeat a change that moved farther. "
                "Reply with a BECAUSE line, then exactly one of a SETTING line, an INDICATOR line, a REWRITE block closed by a line that says END, or a line that says KEEP. "
                "KEEP means the script is sound and the search should carry on. "
                "A SETTING changes one existing input. The search starts at a $250 stop and a $500 take profit. "
                "The stop rises through $250, $500, and $700, then the take profit rises with no maximum. "
                "On a micro contract (MES, MNQ, M2K, MYM, MGC, MCL) the search starts at a stop of $50, a take profit of $100, and the stop rises through $50, $100, and $150. "
                "Indicator changes start from the most profitable pair. Do not move the stop or the take profit away from that pair. "
                "A win rate under the line can shorten a length. "
                "Read the comment that starts How this script works and follow that description of this script. "
                "A small stop needs a short trade only when that description says the entry is an average cross. "
                "Then a smaller length tracks price more closely and crosses sooner, so the trade can finish before that stop. "
                "An INDICATOR adds one indicator from the library, for example INDICATOR: ta.ema 20. That is not a new input. "
                "A REWRITE is for when the script's logic is why the result missed. It says exactly which lines change and how. Another pass writes the script from it, so do not write out the script, in your reply or in your thinking. It cannot add an input the script does not already have. "
                "When the user message says a REDESIGN is open, you may reply with a REDESIGN block closed by a line that says END instead. Another pass writes that script too, so do not write it out. "
                "Do not change the point value. The instrument roots are ES, GC, NQ, MNQ, MES, M2K, and YM. MNQ is $2 a point. MES, M2K, and YM are $5 a point. Gold is GC. Never write GOLD."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    return decision_reply(model_name("Chart researcher"), messages, timeout=DECISION_TIMEOUT)


def _guide() -> str:
    try:
        from futuresfund.learn import research_guidelines

        return research_guidelines()
    except (OSError, ValueError, ImportError):
        return ""


def _redesign_lines() -> list[str]:
    return [
        "A REDESIGN is open on this script. Use it when the idea of the script is why it loses, so a setting or a few lines will not fix it. The goal is a script that makes money consistently without losing more than the drawdown rule allows.",
        "A REDESIGN can change the entries, the filters, the sessions, and the exits, add inputs, or replace the logic completely. The one-change rule above is for a SETTING or a REWRITE.",
        "It replaces this script in place. It is measured at these settings, and it stays only when it does better than this script did. Otherwise this script is put back as it was, so nothing is lost and no extra script is filed.",
        "It keeps the risk rules: the dollar stop held at the entry price, the take profit larger than the stop, and flat when the session ends.",
    ]


def _brief(script: str, trials: list[dict], current: dict, why: str = "", response: dict | None = None, redesign: bool = False) -> str:
    from futuresfund.objective import acceptance_line
    from futuresfund.script_diagnosis import response_lines

    last = trials[-1]
    lines = [
        why,
        acceptance_line(),
        "A trade down more than $750 stays off. The dollar stop stays at or under $700, and the take profit stays larger than the stop with no maximum.",
        "The stop holds at the entry price for the whole trade. Never move it with the close, and never let it move away from a losing trade. If a loss runs past the stop, repair that.",
        "The take profit moving with the last close is on purpose: it lets winners run past the take profit input. A win larger than the take profit is not a fault. Do not lock the take profit to the entry price, and do not call this broken.",
        "Flatten when the session ends at 16:00 New York. Do not move that close to add trades.",
        "More than 600 trades a year stays off the list.",
        "Change one thing. If an input or an exit is not connected, repair that first. If the last run took no trades, or the win rate and the profit factor cannot move by another length, replace the entry. Otherwise change the one input that the last result says is the miss.",
        "The stop starts at $250 and the take profit at $500. A profitable opening is kept. A losing opening raises the stop through $500 and $700, then raises the take profit. Indicator changes start from the most profitable pair.",
        "On a micro contract (MES, MNQ, M2K, MYM, MGC, MCL) the stop starts at $50 and the take profit at $100, and a losing opening raises the stop through $100 and $150.",
        "A win rate under the line can shorten a length.",
        "Read the comment that starts How this script works and follow that description of this script. A small stop needs a short trade only when that description says the entry is an average cross. Then a smaller length tracks price more closely and crosses sooner, so the trade can finish before that stop.",
        "Do not invent an input, unless this is a REDESIGN. Do not change the point value. The instrument roots are ES, GC, NQ, MNQ, MES, M2K, and YM. MNQ is $2 a point. MES, M2K, and YM are $5 a point. Gold is GC. Never write GOLD.",
        *(_redesign_lines() if redesign else []),
        _library_line(),
        _golden_line(),
        _scale_line(trials),
        _guide(),
        "",
        *response_lines(response),
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
    lines.append("or, when the script is sound and the search should carry on:")
    lines.append("KEEP")
    lines.append("or, when the script's logic is the problem:")
    lines.append("REWRITE:")
    lines.append("Which lines change and how. Name each variable and each input.")
    lines.append("END")
    if redesign:
        lines.append("or, when the idea of the script is the problem:")
        lines.append("REDESIGN:")
        lines.append("The whole plan for the redesigned script: its entries, its filters, its sessions, its exits, and each new input. Do not write the script.")
        lines.append("END")
    return "\n".join(lines)


def _golden_line() -> str:
    from futuresfund.golden import standard_line

    return standard_line()


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
    from futuresfund.script_diagnosis import exits_line

    change = row.get("change") or "starting settings"
    closed = exits_line(row.get("exits"))
    held = row.get("held_loser")
    jumped = f" ({held} without the price jump past the stop)" if held is not None and held != row.get("biggest_loser") else ""
    return (
        f"- {change}: profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, "
        f"trades {row.get('trades')}, win rate {row.get('win_rate')}, profit factor {row.get('profit_factor')}, "
        f"largest loss {row.get('biggest_loser')}{jumped}" + (f". Closed by: {closed}" if closed else "")
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
    if len(text) <= SCRIPT_SHOWN:
        return text or "(no script text)"
    return text[:15000] + "\n... The middle of this script is not shown. Do not reply with SCRIPT. ...\n" + text[-12000:]


def keep_original(path: Path) -> None:
    """Copy a script aside the first time the desk rewrites it, so what was written is never lost."""
    home = Path(path).parent.parent / "researchNotes" / "script_originals"
    target = home / Path(path).name
    if Path(path).is_file() and not target.exists():
        home.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(path).read_bytes())


def keep_version(path: Path, text: str) -> str:
    """Save the exact script a profitable result ran, under its hash. The file itself can be rewritten later."""
    import hashlib

    if not text:
        return ""
    name = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    target = Path(path).parent.parent / "researchNotes" / "script_versions" / f"{name}.pine"
    try:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    except OSError:
        return ""
    return name


def undo_rewrite(path: Path, execute) -> str | None:
    """Put back the script from before a rewrite that stopped in the engine. None when no rewrite is waiting."""
    holder = getattr(execute, "source", None)
    if not isinstance(holder, dict) or not holder.get("previous"):
        return None
    previous = holder.pop("previous")
    try:
        path.write_text(previous, encoding="utf-8")
    except OSError:
        return None
    holder["text"] = previous
    holder["stamp"] = int(holder.get("stamp") or 0) + 1
    return previous


def install_script(path: Path, source: str, execute) -> str | None:
    """Write a checked script and point the running backtest at it."""
    if path is None:
        return None
    try:
        previous = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        return None
    revised = accept_script(source, previous)
    if revised is None:
        return None
    from futuresfund.strategy_team import engine_problem

    if engine_problem(revised):
        return None
    holder = getattr(execute, "source", None)
    try:
        keep_original(path)
        path.write_text(revised, encoding="utf-8")
    except OSError:
        return None
    if isinstance(holder, dict):
        holder["previous"] = previous
        holder["text"] = revised
        holder["stamp"] = int(holder.get("stamp") or 0) + 1
    return revised
