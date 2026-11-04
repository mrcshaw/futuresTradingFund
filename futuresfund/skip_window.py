"""Skip the time of day that keeps losing.

A backtest keeps its closed trades by the half hour they entered, in New York
time. When a stage fails, the researcher looks for the stretch of the day, from
half an hour to two hours long, where the trades lost the most together. The
script gets a Skip Window section: no trade opens inside that stretch, and an
open trade is closed when it starts. The window has hour and minute inputs, so
the search can move it like any other setting, and the user can change it.

A script that already has the section only has the window moved.
"""

from __future__ import annotations

import re

SLOT_MINUTES = 30
SLOTS = 24 * 60 // SLOT_MINUTES
MAX_SLOTS = 4            # the longest window skipped is two hours
MIN_TRADES = 12          # a run needs this many closed trades before its clock is read
MIN_WINDOW_TRADES = 4    # a window must hold this many trades to be called a losing time
MIN_SHARE = 0.25         # and lose at least this share of the run's gross loss

INPUT_NAMES = ("use_skip", "skip_start_hr", "skip_start_mn", "skip_end_hr", "skip_end_mn")
_GUARD = "skip_window_open"


def clock(trades: list[dict]) -> dict[str, list]:
    """Closed trades by the half hour they entered, as {"HH:MM": [trades, profit]}."""
    found: dict[str, list] = {}
    for trade in trades or []:
        when = str(trade.get("entry_time") or "")
        pnl = trade.get("pnl")
        if len(when) < 16 or pnl is None or not trade.get("exit_time"):
            continue
        try:
            hour, minute = int(when[11:13]), int(when[14:16])
            value = float(pnl)
        except ValueError:
            continue
        slot = (hour * 60 + minute) // SLOT_MINUTES
        key = _label(slot)
        count, total = found.get(key, [0, 0.0])
        found[key] = [count + 1, round(total + value, 2)]
    return dict(sorted(found.items()))


def _label(slot: int) -> str:
    minutes = (slot % SLOTS) * SLOT_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _slot(label: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(label or ""))
    if match is None:
        return None
    return (int(match.group(1)) * 60 + int(match.group(2))) // SLOT_MINUTES


def worst_window(found: dict | None, max_slots: int = MAX_SLOTS) -> dict | None:
    """The stretch of the day whose trades lost the most together, or None when no stretch stands out.

    Returns {"start": "HH:MM", "end": "HH:MM", "trades": n, "profit": p}. The end is
    the first minute trading opens again. A window can run past midnight.
    """
    if not isinstance(found, dict) or not found:
        return None
    slots: dict[int, tuple[int, float]] = {}
    for key, value in found.items():
        slot = _slot(key)
        if slot is None or not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        try:
            slots[slot] = (int(value[0]), float(value[1]))
        except (TypeError, ValueError):
            continue
    total_trades = sum(count for count, _ in slots.values())
    gross_loss = sum(-pnl for _, pnl in slots.values() if pnl < 0)
    if total_trades < MIN_TRADES or gross_loss <= 0:
        return None
    best = None
    for start in range(SLOTS):
        # A window starts and ends on a half hour that lost, so no time is skipped that cost nothing.
        if slots.get(start, (0, 0.0))[1] >= 0:
            continue
        count = 0
        profit = 0.0
        for length in range(1, max(1, max_slots) + 1):
            count_add, pnl_add = slots.get((start + length - 1) % SLOTS, (0, 0.0))
            count += count_add
            profit += pnl_add
            if pnl_add >= 0:
                continue
            if count < MIN_WINDOW_TRADES or profit >= 0 or -profit < gross_loss * MIN_SHARE:
                continue
            # The deepest loss wins. A shorter window wins a tie, so less of the day is given up.
            if best is None or profit < best[0] - 1e-9:
                best = (profit, start, length, count)
    if best is None:
        return None
    profit, start, length, count = best
    return {"start": _label(start), "end": _label(start + length), "trades": count, "profit": round(profit, 2)}


def merge(clocks: list[dict | None]) -> dict[str, list]:
    """Several runs' clocks added together."""
    merged: dict[str, list] = {}
    for found in clocks or []:
        for key, value in (found or {}).items():
            try:
                count, total = int(value[0]), float(value[1])
            except (TypeError, ValueError, IndexError):
                continue
            have = merged.get(key, [0, 0.0])
            merged[key] = [have[0] + count, round(have[1] + total, 2)]
    return dict(sorted(merged.items()))


def window_result(found: dict | None, window: dict) -> tuple[int, float]:
    """Trades and profit inside the window on one run's clock."""
    start, end = _slot(window["start"]), _slot(window["end"])
    if start is None or end is None or not isinstance(found, dict):
        return 0, 0.0
    length = (end - start) % SLOTS or SLOTS
    inside = {_label(start + step) for step in range(length)}
    count, profit = 0, 0.0
    for key, value in found.items():
        if key in inside:
            try:
                count += int(value[0])
                profit += float(value[1])
            except (TypeError, ValueError, IndexError):
                continue
    return count, round(profit, 2)


def consistent_window(clocks: list[dict | None]) -> dict | None:
    """The time of day that lost on every passed run that traded there, such as the open.

    The window comes from all the runs added together. It counts only when each run
    with trades inside it lost money there, and at least two runs did, so one bad
    day is not called a habit.
    """
    runs = [found for found in clocks or [] if found]
    if len(runs) < 2:
        return None
    window = worst_window(merge(runs))
    if window is None:
        return None
    results = [window_result(found, window) for found in runs]
    traded = [profit for count, profit in results if count]
    if len(traded) < 2 or any(profit >= 0 for profit in traded):
        return None
    return {**window, "runs": len(traded)}


def has_skip(source: str) -> bool:
    return bool(re.search(r"\bskip_start_hr\s*=\s*input\.int", source or ""))


def window_params(window: dict) -> dict:
    """The input values that turn the skip window on at this window."""
    start_h, start_m = (int(part) for part in window["start"].split(":"))
    end_h, end_m = (int(part) for part in window["end"].split(":"))
    return {"use_skip": True, "skip_start_hr": start_h, "skip_start_mn": start_m, "skip_end_hr": end_h, "skip_end_mn": end_m}


def skip_block(window: dict) -> str:
    """The Skip Window inputs and the test, set to this window."""
    values = window_params(window)
    return "\n".join([
        "// ===== SKIP WINDOW (a time of day that keeps losing) =====",
        'use_skip      = input.bool(true, "Skip This Time Of Day", group="Skip Window", '
        'tooltip="No trade opens inside this window, and an open trade is closed when it starts.")',
        f'skip_start_hr = input.int({values["skip_start_hr"]}, "Skip Start Hour (ET)", minval=0, maxval=23, group="Skip Window")',
        f'skip_start_mn = input.int({values["skip_start_mn"]}, "Skip Start Minute", minval=0, maxval=59, group="Skip Window")',
        f'skip_end_hr   = input.int({values["skip_end_hr"]}, "Skip End Hour (ET)", minval=0, maxval=23, group="Skip Window")',
        f'skip_end_mn   = input.int({values["skip_end_mn"]}, "Skip End Minute", minval=0, maxval=59, group="Skip Window")',
        'skip_bar_t = hour(time, "America/New_York") * 100 + minute(time, "America/New_York")',
        "skip_from = skip_start_hr * 100 + skip_start_mn",
        "skip_to = skip_end_hr * 100 + skip_end_mn",
        "in_skip_window = use_skip and (skip_from > skip_to ? (skip_bar_t >= skip_from or skip_bar_t < skip_to) : (skip_bar_t >= skip_from and skip_bar_t < skip_to))",
        f"{_GUARD} = not in_skip_window",
        "if in_skip_window and strategy.position_size != 0",
        '    strategy.close_all(comment="Skip Window")',
    ])


def _set_input(source: str, name: str, value) -> str:
    shown = "true" if value is True else "false" if value is False else str(value)
    pattern = re.compile(rf"(\b{re.escape(name)}\s*=\s*input\.(?:int|bool|float)\s*\(\s*)([^,)\s]+)")
    return pattern.sub(lambda match: match.group(1) + shown, source, count=1)


def _guard_entries(source: str) -> str:
    """Every strategy.entry call opens only outside the skip window. A call over several lines moves as one."""
    rows = source.splitlines()
    out: list[str] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        stripped = row.lstrip(" ")
        guarded = out and out[-1].strip() == f"if {_GUARD}"
        if stripped.startswith("strategy.entry(") and not guarded:
            indent = row[: len(row) - len(stripped)]
            out.append(f"{indent}if {_GUARD}")
            depth = 0
            while index < len(rows):
                part = rows[index]
                out.append("    " + part)
                depth += _paren_depth(part)
                index += 1
                if depth <= 0:
                    break
            continue
        out.append(row)
        index += 1
    return "\n".join(out) + ("\n" if source.endswith("\n") else "")


def _paren_depth(line: str) -> int:
    """Open minus closed brackets on a line, leaving out text inside quotes and after //."""
    depth = 0
    quote = ""
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "/" and line[i:i + 2] == "//":
            break
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        i += 1
    return depth


def _insert_point(source: str) -> int:
    """After the last session input when there is one, else after the strategy() call."""
    found = None
    for match in re.finditer(r"(?m)^[ \t]*sess_end_mn\s*=\s*input\.int\b.*$", source):
        found = match
    if found is not None:
        return found.end() + 1 if found.end() < len(source) else found.end()
    start = source.find("strategy(")
    if start < 0:
        return 0
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                newline = source.find("\n", index)
                return len(source) if newline < 0 else newline + 1
    return len(source)


def add_skip_window(source: str, window: dict) -> str:
    """The script with the skip window set to `window`. A script that has one only has it moved."""
    text = source or ""
    if has_skip(text):
        for name, value in window_params(window).items():
            text = _set_input(text, name, value)
        return text
    if "strategy.entry(" not in text:
        return text
    at = _insert_point(text)
    block = skip_block(window)
    text = text[:at] + ("" if at == 0 or text[at - 1] == "\n" else "\n") + "\n" + block + "\n\n" + text[at:]
    return _guard_entries(text)


def describe(window: dict) -> str:
    return (
        f"Trades entered from {window['start']} to {window['end']} New York time lost "
        f"${-float(window['profit']):,.0f} over {int(window['trades'])} trades, so that time is skipped."
    )
