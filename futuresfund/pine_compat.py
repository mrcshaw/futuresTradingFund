"""Small PineForge compatibility edits. The original version 5 script is left unchanged."""

from __future__ import annotations

import re

_INPUT = re.compile(r"([A-Za-z_]\w*)\s*=\s*input\.(?:int|float)\b")
_MIN_BAR = re.compile(
    r"(?:int\s+)?([A-Za-z_]\w*)\s*=\s*math\.min\(\s*([A-Za-z_]\w*)\s*,\s*bar_index\s*\)"
)
_FROM_PRIOR = re.compile(
    r"(?:int\s+)?([A-Za-z_]\w*)\s*=\s*math\.max\(\s*0\s*,\s*([A-Za-z_]\w*)\s*-\s*1\s*\)"
)
_CALL = re.compile(r"ta\.(highest|lowest)\(\s*([^,\n]+?)\s*,\s*([^)\n]+)\)")
_GUARD = re.compile(r"([A-Za-z_]\w*)\s*>\s*0\s*\?\s*\1\s*:\s*1")
_VERSION = re.compile(r"//@version\s*=\s*5")
_METHOD = re.compile(r"\.([A-Za-z_]\w*)\(")
_NAMESPACES = {
    "ta", "math", "strategy", "array", "request", "str", "color", "input",
    "barstate", "syminfo", "timeframe", "session", "line", "label", "box", "table",
}
_ARRAY_METHODS = {"size", "clear", "push", "pop", "get", "set", "shift", "unshift"}


_PROFILE_RESET = re.compile(
    r"for a = 0 to 2\s+"
    r"arr = a == 0 \? vol_total : a == 1 \? vol_up : vol_dn\s+"
    r"if arr\.size\(\) != rows\s+"
    r"arr\.clear\(\)\s+"
    r"for i = 0 to rows - 1\s+"
    r"arr\.push\(0\.0\)\s+"
    r"else\s+"
    r"for i = 0 to rows - 1\s+"
    r"arr\.set\(i, 0\.0\)"
)
_PROFILE_RANGE = re.compile(
    r"float prof_hi\s*=\s*ta\.highest\(high,\s*lb > 0 \? lb : 1\)\s*"
    r"float prof_lo\s*=\s*ta\.lowest\(low,\s*lb > 0 \? lb : 1\)"
)
_DIRECT_RESET = (
    "for i = 0 to rows - 1\n"
    "        array.set(vol_total, i, 0.0)\n"
    "        array.set(vol_up, i, 0.0)\n"
    "        array.set(vol_dn, i, 0.0)"
)
_DIRECT_RANGE = (
    "float prof_hi = high\n"
    "float prof_lo = low\n"
    "if lb > 1\n"
    "    for jHi = 0 to lb - 1\n"
    "        prof_hi := math.max(prof_hi, high[jHi])\n"
    "        prof_lo := math.min(prof_lo, low[jHi])"
)


def _tradingview_profile(text: str) -> tuple[str, list[str]]:
    """Keep the volume profile on this bar's window. A bin that is never cleared becomes the whole history."""
    notes: list[str] = []
    revised, count = _PROFILE_RESET.subn(_DIRECT_RESET, text, count=1)
    if count:
        notes.append("Profile bins are cleared on every bar, the same way TradingView rebuilds them.")
    revised, count = _PROFILE_RANGE.subn(_DIRECT_RANGE, revised, count=1)
    if count:
        notes.append("The profile high and low use the same bars TradingView uses.")
    return revised, notes


def prepare_for_pineforge(source: str) -> tuple[str, list[str]]:
    """Return the copy PineForge compiles, and the notes for that copy."""
    text, notes = _tradingview_profile(source or "")
    inputs = set(_INPUT.findall(text))
    lengths: dict[str, str] = {}
    for name, base in _MIN_BAR.findall(text):
        if base in inputs:
            lengths[name] = base
    for name, base in _FROM_PRIOR.findall(text):
        if base in lengths:
            lengths[name] = lengths[base]

    def replace(match: re.Match) -> str:
        kind, series, length = match.group(1), match.group(2).strip(), match.group(3).strip()
        target = _length_input(length, lengths, inputs)
        if not target or target == length:
            return match.group(0)
        notes.append(
            f"ta.{kind} now uses the {target} input so PineForge can size it."
        )
        return f"ta.{kind}({series}, {target})"

    revised = _CALL.sub(replace, text)
    revised, array_notes = _array_methods(revised)
    notes.extend(array_notes)
    if _VERSION.search(revised):
        revised = _VERSION.sub("//@version=6", revised, count=1)
        notes.append("PineForge compiled a version 6 copy. Headquarters keeps the version 5 script.")
    # The same length can be named twice, once for highest and once for lowest.
    unique = list(dict.fromkeys(notes))
    return revised, unique


def _length_input(length: str, lengths: dict[str, str], inputs: set[str]) -> str | None:
    if length in inputs:
        return None
    if length in lengths:
        return lengths[length]
    guard = _GUARD.fullmatch(length)
    if guard and guard.group(1) in lengths:
        return lengths[guard.group(1)]
    if guard and guard.group(1) in inputs:
        return guard.group(1)
    return None


def _array_methods(text: str) -> tuple[str, list[str]]:
    """Pine method calls such as arr.size() become array.size(arr)."""
    changed = False
    while True:
        found = _next_method(text)
        if found is None:
            break
        start, end, name, method, args = found
        if args.strip():
            replacement = f"array.{method}({name}, {args})"
        else:
            replacement = f"array.{method}({name})"
        text = text[:start] + replacement + text[end:]
        changed = True
    if not changed:
        return text, []
    return text, ["Array calls such as arr.size() were rewritten as array.size(arr)."]


def _next_method(text: str) -> tuple[int, int, str, str, str] | None:
    for match in _METHOD.finditer(text):
        method = match.group(1)
        if method not in _ARRAY_METHODS:
            continue
        dot = match.start()
        name_start = dot - 1
        while name_start >= 0 and (text[name_start].isalnum() or text[name_start] == "_"):
            name_start -= 1
        name_start += 1
        name = text[name_start:dot]
        if not name or name in _NAMESPACES:
            continue
        open_paren = match.end() - 1
        close = _close_paren(text, open_paren)
        if close < 0:
            continue
        return name_start, close + 1, name, method, text[open_paren + 1:close]
    return None


def _close_paren(text: str, open_paren: int) -> int:
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1
