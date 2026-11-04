"""What a script does when its inputs move. The chart researcher reads this before it changes anything.

An input is unread when nothing that reaches a trade uses it: a take profit that only feeds a
price no exit looks at is unread. A flat input is read, but moving it to either end changed no
trade on the last month of the chart. The search does not spend attempts on either kind, and the
researcher is told about both, because a script that ignores its own settings needs repair, not tuning.

Each script is tested once. The record is researchNotes/script_diagnosis/<key>.json, and the key
ignores comments and the contract's point value, so every chart and instrument shares it. A
rewrite is a new script and gets its own record.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

HOME = Path(__file__).resolve().parent / "researchNotes" / "script_diagnosis"
# The last month shows whether an input matters. A week often has too few trades to tell.
RESPONSE_SESSIONS = 21
RESPONSE_RUNS = 40
# An earlier test may have measured under a bug that has since been fixed. It is run again.
RECORD_VERSION = 3
# A loss this far past the stop means the stop does not hold. The rest is room for slippage and a gap.
# A win past the take profit is not a fault: the target moves with the last close so winners can run.
EXIT_SLACK = 1.5

_NOISE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|//[^\n]*')
_TARGET = re.compile(r"^\s*(?:(?:var|varip)\s+)?(?:(?:int|float|bool|string|color)\s+)?([A-Za-z_]\w*)\s*(?::=|\+=|-=|\*=|/=|=(?!=|>))")
_WORD = re.compile(r"(?<![\w.])[A-Za-z_]\w*")
_POINT = re.compile(r"(?m)^\s*(?:float\s+)?pv\s*=.*$")
_turns: dict[str, threading.Lock] = {}
_gate = threading.Lock()


def _code(source: str) -> str:
    return _NOISE.sub("", source or "")


def unread_inputs(source: str) -> list[str]:
    """Inputs no trade can depend on. A line this cannot follow counts as a read, so it never calls a used input unread."""
    from futuresfund.pine_report import input_calls

    assigned: list[tuple[str, set[str]]] = []
    live: set[str] = set()
    depth = 0
    for line in _code(source).splitlines():
        words = set(_WORD.findall(line))
        target = _TARGET.match(line) if depth == 0 else None
        if target:
            assigned.append((target.group(1), words - {target.group(1)}))
        else:
            live |= words
        depth = max(0, depth + line.count("(") + line.count("[") - line.count(")") - line.count("]"))
    grew = True
    while grew:
        grew = False
        for name, reads in assigned:
            if name in live and not reads <= live:
                live |= reads
                grew = True
    return [call["name"] for call in input_calls(source) if call["name"] not in live]


def script_key(source: str) -> str:
    """The same for every copy of a script. Comments and the contract's point value do not count."""
    lines = [line.strip() for line in _POINT.sub("", _code(source)).splitlines() if line.strip()]
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def load(source: str) -> dict:
    try:
        found = json.loads((HOME / f"{script_key(source)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) and found.get("version") == RECORD_VERSION else {}


def save(source: str, record: dict) -> None:
    """A record that cannot be written never stops research."""
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / f"{script_key(source)}.json").write_text(json.dumps({**record, "version": RECORD_VERSION}, indent=1), encoding="utf-8")
    except OSError:
        pass


def turn(source: str) -> threading.Lock:
    """Held by the chart reading this script. Another chart waits, then uses what it found."""
    key = script_key(source)
    with _gate:
        return _turns.setdefault(key, threading.Lock())


def probes(name: str, value, bounds: tuple | None = None, options: list | None = None) -> list:
    """The values that show whether an input matters: both ends for a number, the other side for a switch."""
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, str):
        return [option for option in options or [] if option != value][:2]
    if not isinstance(value, (int, float)):
        return []
    low, high = bounds or (None, None)
    # A quarter and four times. A $1,000 target only changed a trade at $250 on one script.
    raw = [value * 0.25, value * 4] if value else [high if high is not None else 10]
    if "_hr" in name or "hour" in name.lower():
        high = 23 if high is None else min(high, 23)
    found = []
    for item in raw:
        if low is not None:
            item = max(low, item)
        if high is not None:
            item = min(high, item)
        item = int(round(item)) if isinstance(value, int) else round(float(item), 4)
        if item != value and item not in found:
            found.append(item)
    return found


def _result(row: dict) -> tuple:
    return (row.get("net_profit"), row.get("max_drawdown"), row.get("trades"))


def _baseline(row: dict, params: dict) -> dict:
    keep = ("net_profit", "max_drawdown", "trades", "win_rate", "profit_factor", "biggest_winner", "biggest_loser", "held_loser", "exits")
    return {"params": dict(params), **{key: row.get(key) for key in keep}}


def measure_response(source: str, params: dict, execute, names: list[str], bounds: dict | None = None, choices: dict | None = None, *, limit: int = RESPONSE_RUNS) -> dict:
    """Run the script with each named input moved. partial means the test stopped early, so it is not kept."""
    holder = execute.source
    window = holder.get("sessions")
    holder["sessions"] = RESPONSE_SESSIONS
    unread = unread_inputs(source)
    record: dict = {"sessions": RESPONSE_SESSIONS, "unread": unread, "moved": {}, "flat": [], "untested": []}
    try:
        base = execute(dict(params)) or {}
        if base.get("error") or base.get("stopped") or base.get("skipped") or base.get("fatal"):
            record.update(partial=True, error=str(base.get("error") or ""))
            return record
        record["baseline"] = _baseline(base, params)
        if not base.get("trades"):
            return record
        runs = 0
        for name in names:
            if name in unread or name not in params:
                continue
            values = probes(name, params[name], (bounds or {}).get(name), (choices or {}).get(name))
            if not values:
                continue
            if runs >= limit:
                record["untested"].append(name)
                continue
            ran = 0
            for value in values:
                runs += 1
                row = execute({**params, name: value}) or {}
                if row.get("stopped") or row.get("skipped"):
                    record["partial"] = True
                    return record
                if row.get("error"):
                    continue
                ran += 1
                if _result(row) != _result(base):
                    record["moved"][name] = {"value": value, **{key: row.get(key) for key in ("net_profit", "max_drawdown", "trades")}}
                    break
            if ran and name not in record["moved"]:
                record["flat"].append(name)
        return record
    finally:
        holder["sessions"] = window


def note_decision(source: str, why: str, reply: str) -> None:
    """What the researcher answered, in researchNotes/script_diagnosis/decisions.jsonl."""
    row = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "script": script_key(source), "why": why, "reply": reply}
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with (HOME / "decisions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError:
        pass


def ignored_inputs(record: dict | None) -> set[str]:
    """The inputs the search does not spend attempts on."""
    record = record or {}
    return set(record.get("unread") or []) | set(record.get("flat") or [])


def exit_faults(source: str, record: dict | None) -> list[str]:
    """The stops and take profits that do not work on this script, in words for the researcher."""
    from futuresfund.learn import _is_stop_name, _is_target_name
    from futuresfund.objective import held_loss

    record = record or {}
    base = record.get("baseline") or {}
    params = base.get("params") or {}
    dead = set(unread_inputs(source)) | set(record.get("flat") or [])
    found = []
    for name, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            continue
        stop, target = _is_stop_name(name), _is_target_name(name)
        if not stop and not target:
            continue
        kind = "stop" if stop else "take profit"
        if name in dead:
            found.append(f"The {kind} {name} changes no trade. Nothing closes a trade at it.")
            continue
        worst = held_loss(base)
        if stop and isinstance(worst, (int, float)) and worst < -EXIT_SLACK * value:
            found.append(f"The stop {name} is {value}, but one trade lost {worst}. The stop has to hold at the entry price from the first bar of the trade.")
    return found


def input_faults(source: str) -> list[str]:
    """Inputs no trade reads. A trailing input is left out, because the desk runs with the trail off."""
    from futuresfund.learn import _is_exit_name

    names = [name for name in unread_inputs(source) if "trail" not in name.lower() and not _is_exit_name(name)]
    return [f"Nothing that reaches a trade reads {', '.join(names)}. Connect each one to the rule it names."] if names else []


def response_lines(record: dict | None) -> list[str]:
    """The test, written for the researcher."""
    record = record or {}
    if "baseline" not in record:
        return []
    base = record["baseline"]
    lines = [
        f"How this script responds, tested on the last {record.get('sessions')} sessions. "
        f"At these settings: profit {base.get('net_profit')}, drawdown {base.get('max_drawdown')}, trades {base.get('trades')}.",
    ]
    if not base.get("trades"):
        lines.append("It took no trades in that month, so the entry is what needs reading.")
    if base.get("exits"):
        lines.append(f"How those trades closed: {exits_line(base['exits'])}")
    if record.get("unread"):
        lines.append("Nothing that reaches a trade reads these inputs: " + ", ".join(record["unread"]) + ".")
    if record.get("flat"):
        lines.append("These inputs are read, but moving them to either end changed no trade: " + ", ".join(record["flat"]) + ".")
    for name, seen in (record.get("moved") or {}).items():
        lines.append(f"{name} at {seen.get('value')}: profit {seen.get('net_profit')}, drawdown {seen.get('max_drawdown')}, trades {seen.get('trades')}.")
    if record.get("untested"):
        lines.append("Not tested: " + ", ".join(record["untested"]) + ".")
    return lines


def exit_summary(trades: list[dict] | None) -> list[dict]:
    """How the trades closed: each exit's count, its profit, and how far those trades ran first."""
    groups: dict[str, dict] = {}
    for trade in trades or []:
        name = str(trade.get("exit_signal") or "unnamed")
        group = groups.setdefault(name, {"exit": name, "trades": 0, "pnl": 0.0, "favorable": [], "adverse": []})
        group["trades"] += 1
        group["pnl"] += float(trade.get("pnl") or 0)
        for key in ("favorable", "adverse"):
            if trade.get(key) is not None:
                group[key].append(float(trade[key]))
    rows = []
    for group in sorted(groups.values(), key=lambda item: -item["trades"]):
        rows.append({
            "exit": group["exit"],
            "trades": group["trades"],
            "pnl": round(group["pnl"], 2),
            "favorable": round(sum(group["favorable"]) / len(group["favorable"]), 2) if group["favorable"] else None,
            "adverse": round(sum(group["adverse"]) / len(group["adverse"]), 2) if group["adverse"] else None,
        })
    return rows


def exits_line(exits: list[dict] | None) -> str:
    parts = []
    for row in exits or []:
        text = f"{row.get('exit')} {row.get('trades')} trades, profit {row.get('pnl')}"
        if row.get("favorable") is not None:
            text += f", typically up {row['favorable']} and down {row.get('adverse')} while open"
        parts.append(text)
    return "; ".join(parts)
