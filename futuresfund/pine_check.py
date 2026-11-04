"""The TradingView compile rules PineTS does not enforce.

PineTS runs JavaScript, so it accepts a plot inside an if block and a name used
above its declaration on some paths. TradingView refuses both, and a script that
does not compile on TradingView cannot be traded, whatever its backtest made.
Each problem names the line, in the words the Pine editor uses.
"""

from __future__ import annotations

import re

_GLOBAL_ONLY = (
    "plot", "plotshape", "plotchar", "plotarrow", "plotcandle", "plotbar", "bgcolor", "barcolor",
    "fill", "hline", "alertcondition", "strategy", "indicator", "library",
)
_GLOBAL_CALL = re.compile(r"(?<![\w.])(" + "|".join(_GLOBAL_ONLY) + r")\s*\(")
_INPUT_CALL = re.compile(r"(?<![\w.])(input(?:\.\w+)?)\s*\(")
_TYPE = r"(?:int|float|bool|string|color|line|label|box|table|linefill|polyline|map<[^>]*>|matrix<\w+>|array<\w+>|\w+\[\])"
_DECLARE = re.compile(rf"^(?:(?:var|varip|const|simple|series)\s+)*(?:{_TYPE}\s+)?([A-Za-z_]\w*)\s*=(?![=>])")
_BLOCK = re.compile(r"^(?:if|else|for|while|switch)\b|=>\s*$|(?:=|:=)\s*(?:if|for|while|switch)\b")
_NAME = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\b(?!\s*=[^=>])")


def _code_lines(source: str) -> list[tuple[int, int, str]]:
    """(line number, indent, code) for each line that starts a statement, with strings and comments blanked."""
    found = []
    depth = 0
    for number, raw in enumerate((source or "").splitlines(), start=1):
        text = _blank(raw)
        code = text.strip()
        starts = depth == 0
        depth = max(0, depth + sum(code.count(c) for c in "([") - sum(code.count(c) for c in ")]"))
        if not code or not starts:
            continue
        expanded = text.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip())
        # A wrapped line is indented by anything but a multiple of four. It continues the line above.
        if indent % 4:
            continue
        found.append((number, indent // 4, code))
    return found


def _blank(line: str) -> str:
    out = []
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
                out.append(char)
            index += 1
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            index += 1
            continue
        if line.startswith("//", index):
            break
        out.append(char)
        index += 1
    return "".join(out)


def tv_problems(source: str) -> list[str]:
    """What the TradingView Pine editor refuses in this script. Empty when none of these rules is broken."""
    lines = _code_lines(source)
    problems = []
    scopes: list[dict[str, int]] = [{}]
    global_names: dict[str, int] = {}
    local_names: set[str] = set()
    for number, level, code in lines:
        del scopes[level + 1:]
        while len(scopes) <= level:
            scopes.append({})
        if level:
            for pattern in (_GLOBAL_CALL, _INPUT_CALL):
                for call in pattern.finditer(code):
                    problems.append(f"Line {number}: Cannot use '{call.group(1)}' in local scope. Move it out of the indented block.")
        declared = _DECLARE.match(code)
        if declared:
            name = declared.group(1)
            if name in scopes[level]:
                problems.append(f"Line {number}: '{name}' is already defined on line {scopes[level][name]}. Use := to change it.")
            else:
                scopes[level][name] = number
                if level:
                    local_names.add(name)
                else:
                    global_names.setdefault(name, number)
    for number, _level, code in lines:
        declared = _DECLARE.match(code)
        body = code[declared.end():] if declared else code
        for name in dict.fromkeys(_NAME.findall(_without_text(body))):
            first = global_names.get(name)
            if first is not None and first > number and name not in local_names:
                problems.append(f"Line {number}: Undeclared identifier '{name}'. It is declared below, on line {first}.")
    return problems


def _without_text(code: str) -> str:
    return re.sub(r'"[^"]*"?|\'[^\']*\'?', '""', code)


def opens_block(code: str) -> bool:
    return bool(_BLOCK.search(code))


_GAVE_UP = re.compile(r"\b(truncated|placeholder|simplified|missing/|omitted|rest of the (?:script|code)|for brevity)\b", re.I)


def placeholder_problem(original: str, revised: str) -> str:
    """Why a model's script is not whole: a comment says part of it was left out."""
    said = _GAVE_UP.search("\n".join(line for line in (revised or "").splitlines() if "//" in line))
    if said and not _GAVE_UP.search(original or ""):
        return f"The rewrite says part of the script is {said.group(1).lower()}. A rewrite keeps the whole script."
    return ""


def rewrite_problem(original: str, revised: str) -> str:
    """Why a model's rewrite must not replace the script. A rewrite changes the script; it does not replace its logic."""
    said = placeholder_problem(original, revised)
    if said:
        return said
    before = len(_code_lines(original))
    after = len(_code_lines(revised))
    if before >= 40 and after < before * 0.6:
        return f"The rewrite dropped the script from {before} lines of code to {after}. A rewrite keeps the script's logic."
    return ""
