"""Find the ATR that sets a script's exit distance, and swap it for the desk's dollar stop.

Only ATR that feeds a strategy.exit stop, target, or trail counts: the identifiers an
exit argument reads and the variables assigned to them (stop_dist, sl_dist, a current_stop
that is set to close - sl_dist, an inline close - atr * mult). ATR that drives a signal, such
as a SuperTrend band, a Keltner channel, or a gap size, is never touched.

strategy_team.convert_atr_exit uses this module. It also declares the dollar inputs and the
open-loss backstop, so the converted stop always holds.
"""

from __future__ import annotations

import re

EXIT_ARGS = ("stop", "loss", "limit", "profit", "trail_price", "trail_points", "trail_offset")
# loss, profit, trail_points, and trail_offset are in ticks, not price.
TICK_ARGS = frozenset({"loss", "profit", "trail_points", "trail_offset"})
TARGET_ARGS = frozenset({"limit", "profit"})
STOP_DISTANCE = "stop_dollars / pv"
TARGET_DISTANCE = "tp_dollars / pv"
# The names the converted exit reads. They are never removed as dead inputs.
DESK_NAMES = frozenset({"pv", "stop_dollars", "tp_dollars"})

_ATR_CALL = re.compile(r"(?<![\w.])(?:ta\s*\.\s*)?atr\s*\(")
_TOKEN = re.compile(
    r"""\s+|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+|"""
    r"""[A-Za-z_]\w*(?:[ \t]*\.[ \t]*[A-Za-z_]\w*)*|:=|==|!=|<=|>=|=>|.""",
    re.S,
)
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_NAME = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\b(?![ \t]*\.)(?![ \t]*\()(?![ \t]*=(?!=))")
_ASSIGN = re.compile(
    r"^(?P<lead>[ \t]*(?:(?:var|varip)[ \t]+)?(?:(?:const|simple|series|int|float|bool|string|color)[ \t]+)*)"
    r"(?P<name>[A-Za-z_]\w*)[ \t]*(?P<op>:=|=)(?!=)[ \t]*"
)
_WORD_OPS = frozenset({"and", "or", "not"})
_PINE_WORDS = frozenset({
    "and", "or", "not", "na", "true", "false", "if", "else", "for", "to", "by", "while", "var", "varip",
    "switch", "import", "export", "method", "type", "int", "float", "bool", "string", "color",
})
_STOP_WORDS = frozenset({"stop", "stp", "sl", "loss", "risk"})
_TARGET_WORDS = frozenset({"tp", "target", "tgt", "profit", "take", "limit", "reward"})
# A variable named like a distance is replaced whole: stop_dist = atr * mult becomes stop_dist = stop_dollars / pv.
_SPAN_WORDS = frozenset({"dist", "distance", "offset", "size", "range", "width", "gap", "amt", "amount"})
_EXIT_WORDS = _STOP_WORDS | _TARGET_WORDS | _SPAN_WORDS | frozenset({"pts", "points", "ticks", "exit", "trail", "be", "breakeven", "bracket"})


# ----- reading the script -----

def code_part(line: str) -> str:
    """The line without its // comment. A // inside a string stays."""
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
        elif char in "\"'":
            quote = char
        elif char == "/" and line.startswith("//", index):
            return line[:index]
        index += 1
    return line


def _plain(code: str) -> str:
    return _STRING.sub('""', code)


def _depth_change(code: str) -> int:
    plain = _plain(code)
    return plain.count("(") + plain.count("[") - plain.count(")") - plain.count("]")


def plain_code(line: str) -> str:
    """The line without its comment, with every string emptied."""
    return _plain(code_part(line or ""))


def depth_change(line: str) -> int:
    """How many brackets the line leaves open."""
    return _depth_change(code_part(line or ""))


def mentions_atr(text: str) -> bool:
    """The script calls ta.atr (or atr in Pine v4) somewhere outside a string."""
    return bool(_ATR_CALL.search(_plain(text or "")))


def names_in(expr: str) -> set[str]:
    """The variables an expression reads. Namespaces, calls, keyword arguments, and strings are left out."""
    return {found.group(1) for found in _NAME.finditer(_plain(code_part(expr or ""))) if found.group(1) not in _PINE_WORDS}


def _words(name: str) -> set[str]:
    return {part.lower() for part in re.split(r"_+|(?<=[a-z0-9])(?=[A-Z])", name or "") if part}


def _match_close(text: str, index: int) -> int | None:
    """The index of the bracket that closes the one at index. Strings are skipped."""
    pairs = {"(": ")", "[": "]"}
    opener = text[index]
    closer = pairs[opener]
    depth = 0
    quote = ""
    cursor = index
    while cursor < len(text):
        char = text[cursor]
        if quote:
            if char == "\\":
                cursor += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def split_args(args: str) -> list[str]:
    """Top-level arguments of a call, outer spaces trimmed. Commas inside brackets and strings stay."""
    parts = []
    buf = []
    depth = 0
    quote = ""
    for char in args:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ('"', "'"):
            quote = char
            buf.append(char)
            continue
        if char in "([":
            depth += 1
        elif char in ")]" and depth:
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


class Script:
    """The statements of one script: assignments by name, and every strategy.exit call."""

    def __init__(self, text: str):
        self.text = (text or "").replace("\r\n", "\n")
        self.lines = self.text.split("\n")
        self.spans = self._spans()
        self.assigns: dict[str, list[dict]] = {}
        self.statements: list[dict] = []
        for start, end in self.spans:
            first = code_part(self.lines[start])
            joined = " ".join(code_part(self.lines[index]).strip() for index in range(start, end + 1))
            row = {"start": start, "end": end, "code": joined, "first": first, "name": None}
            found = _ASSIGN.match(first)
            if found and not first[found.end():].startswith("="):
                rhs = " ".join([first[found.end():].strip(), *(code_part(self.lines[index]).strip() for index in range(start + 1, end + 1))]).strip()
                row.update({
                    "name": found.group("name"),
                    "op": found.group("op"),
                    "lead": found.group("lead"),
                    "rhs_at": found.end(),
                    "rhs": rhs,
                    "input": bool(re.match(r"input\b", rhs)),
                })
                self.assigns.setdefault(found.group("name"), []).append(row)
            row["exit"] = joined.lstrip().startswith("strategy.exit")
            self.statements.append(row)
        self.calls = self._exit_calls()
        self.series = self._atr_series()

    def _spans(self) -> list[tuple[int, int]]:
        spans = []
        depth = 0
        start = 0
        for index, line in enumerate(self.lines):
            if depth == 0:
                start = index
            depth = max(0, depth + _depth_change(code_part(line)))
            if depth == 0:
                spans.append((start, index))
        if depth:
            spans.append((start, len(self.lines) - 1))
        return spans

    def _exit_calls(self) -> list[dict]:
        calls = []
        cursor = 0
        text = self.text
        token = "strategy.exit"
        while True:
            start = text.find(token, cursor)
            if start < 0:
                return calls
            line_start = text.rfind("\n", 0, start) + 1
            if len(code_part(text[line_start:start + len(token)])) < start + len(token) - line_start:
                cursor = start + len(token)
                continue
            paren = start + len(token)
            while paren < len(text) and text[paren] in " \t":
                paren += 1
            if paren >= len(text) or text[paren] != "(":
                cursor = start + len(token)
                continue
            end = _match_close(text, paren)
            if end is None:
                return calls
            args = []
            for part in split_args(text[paren + 1:end]):
                found = re.match(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", part, re.S)
                if found:
                    args.append({"part": part, "kw": found.group(1), "expr": found.group(2).strip()})
            calls.append({"start": start, "end": end, "args": args})
            cursor = end + 1

    def _atr_series(self) -> set[str]:
        """Variables that hold the ATR itself: atr_val = ta.atr(14), not stop_dist = atr_val * 2."""
        series = set()
        for name, rows in self.assigns.items():
            for row in rows:
                rhs = _plain(row["rhs"])
                if not _ATR_CALL.search(rhs):
                    continue
                rest = _without_atr_calls(rhs)
                if "*" not in rest and "/" not in rest:
                    series.add(name)
        grown = True
        while grown:
            grown = False
            for name, rows in self.assigns.items():
                if name in series:
                    continue
                for row in rows:
                    alias = re.fullmatch(r"(?:nz\s*\(\s*)?([A-Za-z_]\w*)(?:\s*\[\s*\d+\s*\])?(?:\s*\))?", row["rhs"].strip())
                    if alias and alias.group(1) in series and alias.group(1) != name:
                        series.add(name)
                        grown = True
                        break
        return series

    def has_atr(self, expr: str) -> bool:
        plain = _plain(code_part(expr or ""))
        return bool(_ATR_CALL.search(plain)) or bool(names_in(plain) & self.series)

    def reads(self, name: str, skip: tuple[int, int] | None = None) -> list[dict]:
        """The statements that read name. Its own declaration is skipped."""
        pattern = re.compile(rf"(?<![\w.]){re.escape(name)}\b(?![ \t]*=(?!=))(?![ \t]*:=)")
        found = []
        for row in self.statements:
            if skip is not None and (row["start"], row["end"]) == skip:
                continue
            code = _plain(row["code"])
            if row.get("name") == name:
                code = _plain(row["rhs"])
            if pattern.search(code):
                found.append(row)
        return found


def _without_atr_calls(text: str) -> str:
    out = []
    cursor = 0
    while True:
        found = _ATR_CALL.search(text, cursor)
        if found is None:
            out.append(text[cursor:])
            return "".join(out)
        out.append(text[cursor:found.start()])
        close = _match_close(text, found.end() - 1)
        if close is None:
            out.append(text[found.start():])
            return "".join(out)
        out.append("ATR")
        cursor = close + 1


# ----- swapping the ATR term -----

def _tokens(expr: str) -> list[str]:
    return [found.group(0) for found in _TOKEN.finditer(expr or "")]


def _close_token(tokens: list[str], index: int, stop: int) -> int:
    opener = tokens[index]
    closer = ")" if opener == "(" else "]"
    depth = 0
    for cursor in range(index, stop):
        if tokens[cursor] == opener:
            depth += 1
        elif tokens[cursor] == closer:
            depth -= 1
            if depth == 0:
                return cursor
    return stop - 1


def _history(tokens: list[str], index: int, stop: int, text: str) -> tuple[str, int]:
    """A [n] history reference stays with the value it reads."""
    if index < stop and tokens[index] == "[":
        close = _close_token(tokens, index, stop)
        return text + "".join(tokens[index:close + 1]), close + 1
    return text, index


def _level(tokens: list[str], start: int, stop: int, series: set[str], swap: str) -> tuple[str, bool]:
    """One bracket level with every ATR product swapped. The flag says the whole level was one ATR product."""
    items: list[list[str]] = []
    index = start
    while index < stop:
        token = tokens[index]
        if token.isspace():
            items.append(["ws", token])
            index += 1
            continue
        if token == "(":
            close = _close_token(tokens, index, stop)
            inner, pure = _level(tokens, index + 1, close, series, swap)
            original = "".join(tokens[index:close + 1])
            text, index = _history(tokens, close + 1, stop, original if pure else f"({inner})")
            items.append(["atr" if pure else "atom", text])
            continue
        if token == "[":
            close = _close_token(tokens, index, stop)
            items.append(["atom", "".join(tokens[index:close + 1])])
            index = close + 1
            continue
        if re.match(r"[A-Za-z_]", token):
            word = re.sub(r"\s+", "", token)
            if word in _WORD_OPS:
                items.append(["op", token])
                index += 1
                continue
            ahead = index + 1
            while ahead < stop and tokens[ahead].isspace():
                ahead += 1
            if ahead < stop and tokens[ahead] == "(":
                close = _close_token(tokens, ahead, stop)
                original = "".join(tokens[index:close + 1])
                if word in ("ta.atr", "atr"):
                    kind, text = "atr", original
                else:
                    inner, pure = _level(tokens, ahead + 1, close, series, swap)
                    if pure and word in ("nz", "math.abs"):
                        kind, text = "atr", original
                    else:
                        kind, text = "atom", "".join(tokens[index:ahead + 1]) + inner + ")"
                text, index = _history(tokens, close + 1, stop, text)
                items.append([kind, text])
                continue
            text, index = _history(tokens, index + 1, stop, token)
            items.append(["atr" if word in series else "atom", text])
            continue
        if token == "*":
            items.append(["mul", token])
        elif token in ("/", "%"):
            items.append(["div", token])
        elif re.match(r"[\d.]", token) and token != "." or token[:1] in "\"'":
            items.append(["atom", token])
        else:
            items.append(["op", token])
        index += 1
    return _swap_runs(items, swap)


def _swap_runs(items: list[list[str]], swap: str) -> tuple[str, bool]:
    """Replace every run of factors joined by * that holds an ATR value."""
    out: list[str] = []
    solid = [index for index, item in enumerate(items) if item[0] != "ws"]
    ops = [index for index in solid if items[index][0] == "op"]
    divs = [index for index in solid if items[index][0] == "div"]
    pure = False
    cursor = 0
    runs = []
    position = 0
    while position < len(solid):
        index = solid[position]
        if items[index][0] not in ("atom", "atr"):
            position += 1
            continue
        run = [index]
        ahead = position + 1
        while ahead + 1 < len(solid) and items[solid[ahead]][0] == "mul" and items[solid[ahead + 1]][0] in ("atom", "atr"):
            run.append(solid[ahead + 1])
            ahead += 2
        if any(items[member][0] == "atr" for member in run):
            runs.append((run[0], run[-1]))
        position = ahead
    if len(runs) == 1 and not ops and not divs and runs[0] == (solid[0], solid[-1]):
        pure = True
    for first, last in runs:
        before = next((items[index] for index in range(first - 1, -1, -1) if items[index][0] != "ws"), None)
        wrapped = f"({swap})" if before is not None and before[0] in ("div", "mul") else swap
        out.append("".join(item[1] for item in items[cursor:first]))
        out.append(wrapped)
        cursor = last + 1
    out.append("".join(item[1] for item in items[cursor:]))
    return "".join(out), pure


def swap_atr(expr: str, series: set[str], swap: str) -> str:
    """expr with each ATR product, such as atr_val * stop_mult, replaced by swap."""
    tokens = _tokens(expr)
    text, _pure = _level(tokens, 0, len(tokens), set(series), swap)
    return text


# ----- the exit chain -----

def _role_of(name: str, reached: set[tuple[str, str]]) -> str:
    words = _words(name)
    if words & _STOP_WORDS and not words & _TARGET_WORDS:
        return "stop"
    if words & _TARGET_WORDS and not words & _STOP_WORDS:
        return "target"
    return "stop" if any(role == "stop" for role, _unit in reached) else "target"


def _distance(role: str, unit: str, expr: str) -> str:
    swap = STOP_DISTANCE if role == "stop" else TARGET_DISTANCE
    if unit == "ticks" and "mintick" not in expr:
        swap += " / syminfo.mintick"
    return swap


def _reach(script: Script) -> dict[str, set[tuple[str, str]]]:
    """Every variable an exit argument reads, directly or through the variables assigned to it, with its role and unit."""
    reach: dict[str, set[tuple[str, str]]] = {}
    queue: list[tuple[str, str, str]] = []
    for call in script.calls:
        for arg in call["args"]:
            if arg["kw"] not in EXIT_ARGS:
                continue
            role = "target" if arg["kw"] in TARGET_ARGS else "stop"
            unit = "ticks" if arg["kw"] in TICK_ARGS and "mintick" not in arg["expr"] else "points"
            queue.extend((name, role, unit) for name in names_in(arg["expr"]))
    while queue:
        name, role, unit = queue.pop()
        seen = reach.setdefault(name, set())
        if (role, unit) in seen:
            continue
        seen.add((role, unit))
        if name in script.series:
            continue
        for row in script.assigns.get(name, []):
            if row["input"]:
                continue
            carried = "points" if "mintick" in row["rhs"] else unit
            queue.extend((other, role, carried) for other in names_in(row["rhs"]) if other != name)
    return reach


def _exit_only(script: Script, name: str, reach: dict) -> bool:
    """Every read of name is an exit call or another variable on the exit chain, so no signal reads it."""
    rows = script.reads(name)
    if not rows:
        return False
    for row in rows:
        if row.get("exit"):
            continue
        target = row.get("name")
        if target and target in reach and target not in script.series:
            continue
        return False
    return True


# Statements that only draw or log. A value they read changes the picture, not a trade.
_DRAWING = re.compile(
    r"^\s*(?:plot|plotshape|plotchar|plotarrow|plotcandle|plotbar|hline|fill|bgcolor|barcolor"
    r"|label\.\w+|line\.\w+|box\.\w+|table\.\w+|linefill\.\w+|log\.\w+)\s*\("
)


def _signal_reads(script: Script, name: str) -> bool:
    """A signal reads name, directly or through the variables assigned from it.

    An if condition, an entry, a crossover, or anything else that is not an exit call or a drawing
    counts. A variable that reads name passes its own readers on: trail := math.max(trail, up_level)
    makes a signal that reads trail a signal that reads up_level.
    """
    seen: set[str] = set()
    queue = [name]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        word = re.compile(rf"(?<![\w.]){re.escape(current)}\b")
        guard = re.compile(rf"(?<![\w.])na\s*\(\s*{re.escape(current)}\s*\)")
        for row in script.reads(current):
            if row.get("exit"):
                continue
            code = _plain(row["rhs"] if row.get("name") and row.get("rhs") is not None else row["code"])
            if not word.search(guard.sub("", code)):
                # Only a warm-up guard, not na(sl_dist): it waits for the ATR's first bars and reads no level.
                continue
            target = row.get("name")
            if target:
                if target in script.series:
                    return True
                queue.append(target)
                continue
            if _DRAWING.match(row["code"]):
                continue
            body = _exit_block(script, row)
            if body is not None:
                # An if whose block only manages the exit, such as if new_stop > current_stop, is part of the
                # exit. What its block assigns is followed like any other reader.
                queue.extend(body)
                continue
            return True
    return False


_IF_HEAD = re.compile(r"^\s*(?:else\s+)?if\b")
_ENTRY_CALL = re.compile(r"(?<![\w.])strategy\.(?:entry|order)\s*\(")


def _exit_block(script: Script, row: dict) -> list[str] | None:
    """The names an if block assigns when the block opens no trade, or None for an if that does (a signal)."""
    if not _IF_HEAD.match(row["code"]):
        return None
    head = script.lines[row["start"]]
    indent = len(head) - len(head.lstrip())
    names: list[str] = []
    for other in script.statements:
        if other["start"] <= row["end"]:
            continue
        line = script.lines[other["start"]]
        if not code_part(line).strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if _ENTRY_CALL.search(_plain(other["code"])):
            return None
        if other.get("name"):
            names.append(other["name"])
    return names


def sites(text: str) -> list[dict]:
    """Every place ATR sets an exit distance, with what it becomes."""
    if not _ATR_CALL.search(_plain(text or "")):
        return []
    script = Script(text)
    reach = _reach(script)
    found = []
    for number, call in enumerate(script.calls):
        for arg in call["args"]:
            if arg["kw"] not in EXIT_ARGS or not script.has_atr(arg["expr"]):
                continue
            role = "target" if arg["kw"] in TARGET_ARGS else "stop"
            unit = "ticks" if arg["kw"] in TICK_ARGS and "mintick" not in arg["expr"] else "points"
            new = swap_atr(arg["expr"], script.series, _distance(role, unit, arg["expr"]))
            if new != arg["expr"]:
                found.append({
                    "kind": "arg", "call": number, "part": arg["part"], "kw": arg["kw"], "old": arg["expr"], "new": new,
                    "role": role, "line": script.text.count("\n", 0, call["start"]),
                })
    for name, reached in reach.items():
        if name in script.series or name in DESK_NAMES:
            continue
        rows = script.assigns.get(name) or []
        if not rows or not (_words(name) & _EXIT_WORDS or _exit_only(script, name, reach)):
            continue
        if _signal_reads(script, name):
            # A band a signal also reads (a stop line an entry crosses, a SuperTrend level) keeps its ATR,
            # whatever its name: the entry must not change.
            continue
        role = _role_of(name, reached)
        unit = "ticks" if {item[1] for item in reached} == {"ticks"} else "points"
        for row in rows:
            if row["input"] or not script.has_atr(row["rhs"]):
                continue
            swap = _distance(role, unit, row["rhs"])
            if _words(name) & _SPAN_WORDS and "mintick" not in row["rhs"]:
                new = swap
            else:
                new = swap_atr(row["rhs"], script.series, swap)
            if new != row["rhs"]:
                found.append({"kind": "assign", "row": row, "name": name, "old": row["rhs"], "new": new, "role": role, "line": row["start"]})
    # Top of the script first. An assignment is rewritten before an exit call on the same line number.
    found.sort(key=lambda site: (site["line"], site["kind"] != "assign"))
    return found


def shown(site: dict) -> str:
    """The site as one short line, for a note."""
    if site["kind"] == "assign":
        return f"{site['name']} = {site['old']}"
    return f"{site['kw']}={site['old']}"


def apply_site(text: str, site: dict) -> str:
    """The script with this one site rewritten."""
    if site["kind"] == "assign":
        lines = (text or "").replace("\r\n", "\n").split("\n")
        row = site["row"]
        first = lines[row["start"]]
        comment = first[len(code_part(first)):] if row["start"] == row["end"] else ""
        rebuilt = code_part(first)[: row["rhs_at"]] + site["new"] + (" " + comment.strip() if comment.strip() else "")
        lines[row["start"]:row["end"] + 1] = [rebuilt.rstrip()]
        return "\n".join(lines)
    script = Script(text)
    call = script.calls[site["call"]]
    body = script.text[call["start"]:call["end"] + 1]
    rebuilt = body.replace(site["part"], f"{site['kw']}={site['new']}", 1)
    return script.text[:call["start"]] + rebuilt + script.text[call["end"] + 1:]


# ----- what nothing reads any more -----

def _input_default(row: dict) -> str | None:
    rhs = row["rhs"]
    paren = rhs.find("(")
    close = _match_close(rhs, paren) if paren >= 0 else None
    if close is None:
        return None
    args = split_args(rhs[paren + 1:close])
    if args and not re.match(r"^[A-Za-z_]\w*\s*=(?!=)", args[0]):
        return args[0]
    for arg in args:
        found = re.match(r"^defval\s*=\s*(.+)$", arg, re.S)
        if found:
            return found.group(1).strip()
    return None


def drop_dead(text: str, candidates: set[str]) -> tuple[str, list[str], list[str]]:
    """Remove what the converted exit no longer reads: a multiplier input, the ATR length, ta.atr.

    A name goes only when nothing else in the script reads it. A multiplier input that a label
    still reads becomes a fixed value, so the search does not measure it as a dollar stop.
    """
    from futuresfund.research_rules import _exit_kind, exit_step

    removed: list[str] = []
    fixed: list[str] = []
    pending = {name for name in candidates if name not in DESK_NAMES}
    body = (text or "").replace("\r\n", "\n")
    for _ in range(64):
        if not pending:
            break
        script = Script(body)
        name = sorted(pending)[0]
        pending.discard(name)
        rows = script.assigns.get(name) or []
        # Only a single declaration at the left margin goes. A line inside an if block may be all that block holds.
        if len(rows) != 1 or rows[0]["op"] != "=" or rows[0]["lead"][:1].isspace():
            continue
        row = rows[0]
        span = (row["start"], row["end"])
        lines = script.lines
        if not script.reads(name, skip=span):
            lines[row["start"]:row["end"] + 1] = []
            body = "\n".join(lines)
            removed.append(name)
            pending |= {other for other in names_in(row["rhs"]) if other not in DESK_NAMES and other != name}
            continue
        if row["input"] and (_exit_kind(name) is not None or exit_step(name) is not None):
            value = _input_default(row)
            if value is None:
                continue
            indent = re.match(r"[ \t]*", lines[row["start"]]).group(0)
            lines[row["start"]:row["end"] + 1] = [f"{indent}{name} = {value}"]
            body = "\n".join(lines)
            fixed.append(name)
    return body, removed, fixed


def convert_sites(text: str) -> tuple[str, list[dict], set[str]]:
    """Rewrite every ATR exit site. Returns the text, the sites, and the names they stopped reading."""
    body = (text or "").replace("\r\n", "\n")
    done: list[dict] = []
    gone: set[str] = set()
    for _ in range(64):
        found = sites(body)
        if not found:
            break
        site = found[0]
        after = apply_site(body, site)
        if after == body:
            break
        gone |= names_in(site["old"]) - names_in(site["new"])
        done.append(site)
        body = after
    return body, done, gone
