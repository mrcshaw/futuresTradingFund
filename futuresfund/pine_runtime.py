"""Pine Script v5, compiled to Python and run bar by bar on TradingView's broker.

The script runs top to bottom on every bar, the way TradingView runs it. A
variable keeps the value it closed each bar with, so x[n] reads it back. var
and varip keep their value between bars. Each ta.* call keeps its own state.
strategy.* calls go to futuresfund.tv_broker, which fills orders the way the
Strategy Tester does. Drawing calls that cannot change a trade are accepted
without being evaluated.
"""

from __future__ import annotations

import ast
import hashlib
import math
import re
import sys
import threading
import time
from types import SimpleNamespace

from futuresfund import pine_lib as L
from futuresfund.tv_broker import Broker

PineError = L.PineError


class PineUnsupported(RuntimeError):
    """The script uses something this runtime does not run."""


# ---------- logical lines and tokens ----------

def _strip_comment(line: str) -> str:
    quote = ""
    k = 0
    while k < len(line):
        ch = line[k]
        if quote:
            if ch == "\\":
                k += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "/" and line.startswith("//", k):
            return line[:k]
        k += 1
    return line


def _balance(text: str) -> int:
    depth = 0
    quote = ""
    k = 0
    while k < len(text):
        ch = text[k]
        if quote:
            if ch == "\\":
                k += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        k += 1
    return depth


def _logical_lines(source: str) -> list[list]:
    """Blocks are indented by four spaces. A line indented by anything else, or inside brackets, continues the last one."""
    lines: list[list] = []
    depth = 0
    for number, raw in enumerate(source.replace("\t", "    ").splitlines(), 1):
        code = _strip_comment(raw).rstrip()
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        text = code.strip()
        if lines and (depth > 0 or indent % 4 != 0):
            lines[-1][1] += " " + text
        else:
            lines.append([indent // 4, text, number])
        depth = max(0, depth + _balance(text))
    return lines


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<color>\#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)
  | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op>:=|\+=|-=|\*=|/=|%=|==|!=|<=|>=|=>|[-+*/%<>=?:,.()\[\]{}])
""", re.X)


def _tokenize(text: str, number: int) -> list[tuple[str, str]]:
    tokens = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise PineUnsupported(f"Line {number}: cannot read {text[position:position + 24]!r}.")
        position = match.end()
        if match.lastgroup != "ws":
            tokens.append((match.lastgroup, match.group()))
    tokens.append(("end", ""))
    return tokens


def _unquote(text: str) -> str:
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text[1:-1]


# ---------- parser ----------

class _Parser:
    def __init__(self, tokens, number: int) -> None:
        self.tokens = tokens
        self.k = 0
        self.number = number

    def peek(self, ahead: int = 0):
        return self.tokens[min(self.k + ahead, len(self.tokens) - 1)]

    def take(self):
        token = self.tokens[self.k]
        self.k += 1
        return token

    def is_op(self, value: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token[0] == "op" and token[1] == value

    def is_word(self, value: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token[0] == "name" and token[1] == value

    def accept_op(self, value: str) -> bool:
        if self.is_op(value):
            self.k += 1
            return True
        return False

    def accept_word(self, value: str) -> bool:
        if self.is_word(value):
            self.k += 1
            return True
        return False

    def op(self, value: str) -> None:
        if not self.accept_op(value):
            raise self.error(f"expected {value!r} but found {self.peek()[1]!r}")

    def name(self) -> str:
        token = self.take()
        if token[0] != "name":
            raise self.error(f"expected a name but found {token[1]!r}")
        return token[1]

    def done(self) -> bool:
        return self.peek()[0] == "end"

    def end(self) -> None:
        if not self.done():
            raise self.error(f"unexpected {self.peek()[1]!r}")

    def error(self, text: str) -> PineUnsupported:
        return PineUnsupported(f"Line {self.number}: {text}.")


def _expr(p):
    node = _or(p)
    if p.accept_op("?"):
        yes = _expr(p)
        p.op(":")
        no = _expr(p)
        return ("tern", node, yes, no)
    return node


def _or(p):
    node = _and(p)
    while p.accept_word("or"):
        node = ("or", node, _and(p))
    return node


def _and(p):
    node = _equal(p)
    while p.accept_word("and"):
        node = ("and", node, _equal(p))
    return node


def _equal(p):
    node = _compare(p)
    while p.peek()[0] == "op" and p.peek()[1] in ("==", "!="):
        op = p.take()[1]
        node = ("bin", op, node, _compare(p))
    return node


def _compare(p):
    node = _add(p)
    while p.peek()[0] == "op" and p.peek()[1] in ("<", ">", "<=", ">="):
        op = p.take()[1]
        node = ("bin", op, node, _add(p))
    return node


def _add(p):
    node = _multiply(p)
    while p.peek()[0] == "op" and p.peek()[1] in ("+", "-"):
        op = p.take()[1]
        node = ("bin", op, node, _multiply(p))
    return node


def _multiply(p):
    node = _unary(p)
    while p.peek()[0] == "op" and p.peek()[1] in ("*", "/", "%"):
        op = p.take()[1]
        node = ("bin", op, node, _unary(p))
    return node


def _unary(p):
    if p.accept_op("-"):
        return ("neg", _unary(p))
    if p.accept_op("+"):
        return _unary(p)
    if p.accept_word("not"):
        return ("not", _unary(p))
    return _postfix(p)


def _postfix(p):
    node = _primary(p)
    while True:
        if p.is_op("("):
            args, kwargs = _arguments(p)
            node = ("call", node, args, kwargs)
        elif p.is_op("."):
            p.take()
            attr = p.name()
            node = ("attr", node, attr)
            if attr == "new" and p.is_op("<"):
                _skip_generic(p)
        elif p.is_op("["):
            p.take()
            index = _expr(p)
            p.op("]")
            node = ("hist", node, index)
        else:
            return node


def _skip_generic(p) -> None:
    depth = 0
    while True:
        token = p.take()
        if token[0] == "end":
            raise p.error("the type list is not closed")
        if token == ("op", "<"):
            depth += 1
        elif token == ("op", ">"):
            depth -= 1
            if depth == 0:
                return


def _primary(p):
    kind, value = p.take()
    if kind == "num":
        return ("num", int(value)) if value.isdigit() else ("num", float(value))
    if kind == "str":
        return ("str", _unquote(value))
    if kind == "color":
        return ("color", value)
    if kind == "name":
        if value == "true":
            return ("bool", True)
        if value == "false":
            return ("bool", False)
        return ("name", value)
    if kind == "op" and value == "(":
        node = _expr(p)
        p.op(")")
        return node
    if kind == "op" and value == "[":
        items = [_expr(p)]
        while p.accept_op(","):
            items.append(_expr(p))
        p.op("]")
        return ("tuple", items)
    raise p.error(f"unexpected {value!r}")


def _arguments(p):
    p.op("(")
    args, kwargs = [], {}
    if p.accept_op(")"):
        return args, kwargs
    while True:
        if p.peek()[0] == "name" and p.is_op("=", 1):
            key = p.take()[1]
            p.take()
            kwargs[key] = _expr(p)
        else:
            args.append(_expr(p))
        if p.accept_op(","):
            continue
        p.op(")")
        return args, kwargs


_TYPE_WORDS = {"int", "float", "bool", "string", "color", "label", "line", "box", "table", "linefill", "polyline", "array", "matrix", "map"}
_QUALIFIERS = {"series", "simple", "const"}


def _skip_type(p, types: set) -> bool:
    """Consume a type in front of a name, such as float, array<float>, or float[]."""
    start = p.k
    while p.peek()[0] == "name" and p.peek()[1] in _QUALIFIERS:
        p.take()
    token = p.peek()
    if token[0] != "name" or (token[1] not in _TYPE_WORDS and token[1] not in types):
        p.k = start
        return False
    p.take()
    if p.is_op("<"):
        _skip_generic(p)
    if p.is_op("[") and p.is_op("]", 1):
        p.take()
        p.take()
    if p.peek()[0] != "name":
        p.k = start
        return False
    return True


def _parse(source: str) -> list:
    lines = _logical_lines(source)
    types = {re.match(r"type\s+(\w+)", text).group(1) for depth, text, _ in lines if depth == 0 and re.match(r"type\s+\w+\s*$", text)}
    body, index = _block(lines, 0, 0, types)
    if index != len(lines):
        raise PineUnsupported(f"Line {lines[index][2]}: unexpected indent.")
    return body


def _block(lines, start: int, level: int, types: set):
    out = []
    k = start
    while k < len(lines):
        depth, text, number = lines[k]
        if depth < level:
            break
        if depth > level:
            raise PineUnsupported(f"Line {number}: unexpected indent.")
        statement, k = _statement(lines, k, level, types)
        if statement is not None:
            out.append(statement)
    return out, k


def _statement(lines, k: int, level: int, types: set):
    depth, text, number = lines[k]
    tokens = _tokenize(text, number)
    first = tokens[0]
    word = first[1] if first[0] == "name" else None
    if word == "if":
        return _if_statement(lines, k, level, types)
    if word == "for":
        return _for_statement(lines, k, level, types)
    if word == "while":
        p = _Parser(tokens, number)
        p.take()
        condition = _expr(p)
        p.end()
        body, nxt = _block(lines, k + 1, level + 1, types)
        return ("while", condition, body, number), nxt
    if word in ("break", "continue") and tokens[1][0] == "end":
        return (word, number), k + 1
    if word in ("import", "export", "method"):
        raise PineUnsupported(f"Line {number}: {word} is not supported yet.")
    if word == "switch":
        p = _Parser(tokens, number)
        p.take()
        subject = None if p.done() else _expr(p)
        p.end()
        cases, nxt = _switch_cases(lines, k + 1, level + 1, types)
        return ("switch", subject, cases, number), nxt
    split = _value_split(tokens)
    if split is not None:
        return _value_statement(lines, k, level, types, tokens, split, number)
    if word == "type" and len(tokens) == 3:
        name = tokens[1][1]
        fields = []
        nxt = k + 1
        while nxt < len(lines) and lines[nxt][0] > level:
            fields.append(_field(lines[nxt], types))
            nxt += 1
        return ("type", name, fields, number), nxt
    function = _function(lines, k, level, types, tokens, number)
    if function is not None:
        return function
    return _simple_statement(tokens, number, types), k + 1


def _value_split(tokens) -> int | None:
    """The index of = or := when an if or switch block gives the value, else None."""
    depth = 0
    for index, token in enumerate(tokens):
        if token[0] == "op" and token[1] in ("(", "["):
            depth += 1
        elif token[0] == "op" and token[1] in (")", "]"):
            depth -= 1
        elif depth == 0 and token[0] == "op" and token[1] in ("=", ":=") and tokens[index + 1][0] == "name" and tokens[index + 1][1] in ("if", "switch"):
            return index
    return None


def _value_statement(lines, k: int, level: int, types: set, tokens, split: int, number: int):
    op = tokens[split][1]
    rest = tokens[split + 1:]
    if rest[0][1] == "if":
        p = _Parser(rest, number)
        p.take()
        condition = _expr(p)
        p.end()
        body, nxt = _block(lines, k + 1, level + 1, types)
        branches = [(condition, body)]
        other = None
        while nxt < len(lines) and lines[nxt][0] == level:
            _, text2, number2 = lines[nxt]
            more = _tokenize(text2, number2)
            if more[0] != ("name", "else"):
                break
            if more[1] == ("name", "if"):
                p2 = _Parser(more, number2)
                p2.take()
                p2.take()
                condition2 = _expr(p2)
                p2.end()
                body2, nxt = _block(lines, nxt + 1, level + 1, types)
                branches.append((condition2, body2))
                continue
            other, nxt = _block(lines, nxt + 1, level + 1, types)
            break
        value = ("ifv", branches, other)
    else:
        p = _Parser(rest, number)
        p.take()
        subject = None if p.done() else _expr(p)
        p.end()
        cases, nxt = _switch_cases(lines, k + 1, level + 1, types)
        value = ("switchv", subject, cases)
    head = _Parser(tokens[:split] + [("end", "")], number)
    if op == "=":
        mode = None
        if head.peek()[0] == "name" and head.peek()[1] in ("var", "varip"):
            mode = head.take()[1]
        typed = _skip_type(head, types)
        name = head.name()
        head.end()
        return ("decl", mode, typed, name, value, number), nxt
    target = _postfix(head)
    head.end()
    return ("assign", target, ":=", value, number), nxt


def _switch_cases(lines, start: int, level: int, types: set):
    """Each case is (match or None for the default, body statements)."""
    cases = []
    k = start
    while k < len(lines) and lines[k][0] == level:
        _, text, number = lines[k]
        tokens = _tokenize(text, number)
        depth = 0
        arrow = None
        for index, token in enumerate(tokens):
            if token[0] == "op" and token[1] in ("(", "["):
                depth += 1
            elif token[0] == "op" and token[1] in (")", "]"):
                depth -= 1
            elif depth == 0 and token == ("op", "=>"):
                arrow = index
                break
        if arrow is None:
            raise PineUnsupported(f"Line {number}: a switch case needs =>.")
        match = None
        if arrow > 0:
            p = _Parser(tokens[:arrow] + [("end", "")], number)
            match = _expr(p)
            p.end()
        right = tokens[arrow + 1:]
        if right[0][0] == "end":
            body, k = _block(lines, k + 1, level + 1, types)
        else:
            body = [_simple_statement(right, number, types)]
            k += 1
        cases.append((match, body))
    return cases, k


def _field(line, types: set):
    _, text, number = line
    p = _Parser(_tokenize(text, number), number)
    _skip_type(p, types)
    name = p.name()
    default = _expr(p) if p.accept_op("=") else None
    p.end()
    return name, default


def _if_statement(lines, k: int, level: int, types: set):
    _, text, number = lines[k]
    p = _Parser(_tokenize(text, number), number)
    p.take()
    condition = _expr(p)
    p.end()
    body, nxt = _block(lines, k + 1, level + 1, types)
    branches = [(condition, body)]
    other = None
    while nxt < len(lines) and lines[nxt][0] == level:
        _, text2, number2 = lines[nxt]
        tokens = _tokenize(text2, number2)
        if tokens[0] != ("name", "else"):
            break
        if tokens[1] == ("name", "if"):
            p2 = _Parser(tokens, number2)
            p2.take()
            p2.take()
            condition2 = _expr(p2)
            p2.end()
            body2, nxt = _block(lines, nxt + 1, level + 1, types)
            branches.append((condition2, body2))
            continue
        if tokens[1][0] != "end":
            raise PineUnsupported(f"Line {number2}: cannot read the else line.")
        other, nxt = _block(lines, nxt + 1, level + 1, types)
        break
    return ("if", branches, other, number), nxt


def _for_statement(lines, k: int, level: int, types: set):
    _, text, number = lines[k]
    p = _Parser(_tokenize(text, number), number)
    p.take()
    if p.accept_op("["):
        names = [p.name()]
        while p.accept_op(","):
            names.append(p.name())
        p.op("]")
        if not p.accept_word("in"):
            raise p.error("expected in")
        iterable = _expr(p)
        p.end()
        body, nxt = _block(lines, k + 1, level + 1, types)
        return ("forin", names, iterable, body, number), nxt
    name = p.name()
    if p.accept_word("in"):
        iterable = _expr(p)
        p.end()
        body, nxt = _block(lines, k + 1, level + 1, types)
        return ("forin", [name], iterable, body, number), nxt
    p.op("=")
    start = _expr(p)
    if not p.accept_word("to"):
        raise p.error("expected to")
    end = _expr(p)
    step = _expr(p) if p.accept_word("by") else None
    p.end()
    body, nxt = _block(lines, k + 1, level + 1, types)
    return ("for", name, start, end, step, body, number), nxt


def _function(lines, k: int, level: int, types: set, tokens, number: int):
    if tokens[0][0] != "name" or tokens[1] != ("op", "("):
        return None
    depth = 0
    for index, token in enumerate(tokens):
        if token == ("op", "("):
            depth += 1
        elif token == ("op", ")"):
            depth -= 1
            if depth == 0:
                if tokens[index + 1] != ("op", "=>"):
                    return None
                break
    else:
        return None
    p = _Parser(tokens, number)
    name = p.name()
    p.op("(")
    params = []
    if not p.accept_op(")"):
        while True:
            _skip_type(p, types)
            param = p.name()
            default = _expr(p) if p.accept_op("=") else None
            params.append((param, default))
            if p.accept_op(","):
                continue
            p.op(")")
            break
    p.op("=>")
    if p.done():
        body, nxt = _block(lines, k + 1, level + 1, types)
        return ("func", name, params, body, None, number), nxt
    value = _expr(p)
    p.end()
    return ("func", name, params, None, value, number), k + 1


def _simple_statement(tokens, number: int, types: set):
    p = _Parser(tokens, number)
    if p.is_op("["):
        p.take()
        names = []
        try:
            names.append(p.name())
            while p.accept_op(","):
                names.append(p.name())
            p.op("]")
            if p.accept_op("="):
                value = _expr(p)
                p.end()
                return ("tdecl", names, value, number)
        except PineUnsupported:
            pass
        p.k = 0
    mode = None
    if p.peek()[0] == "name" and p.peek()[1] in ("var", "varip"):
        mode = p.take()[1]
    start = p.k
    typed = _skip_type(p, types)
    if p.peek()[0] == "name" and p.is_op("=", 1):
        name = p.take()[1]
        p.take()
        if p.is_word("if") or p.is_word("switch"):
            raise PineUnsupported(f"Line {number}: an if or switch value is not supported yet.")
        value = _expr(p)
        p.end()
        return ("decl", mode, typed, name, value, number)
    if mode or typed:
        raise PineUnsupported(f"Line {number}: cannot read the declaration.")
    p.k = start
    target = _postfix(p)
    if p.peek()[0] == "op" and p.peek()[1] in (":=", "+=", "-=", "*=", "/=", "%="):
        op = p.take()[1]
        value = _expr(p)
        p.end()
        return ("assign", target, op, value, number)
    p.k = 0
    value = _expr(p)
    p.end()
    return ("expr", value, number)


# ---------- compiler ----------

def _chain(node) -> list[str] | None:
    """["strategy", "closedtrades", "profit"] for strategy.closedtrades.profit, or None."""
    parts = []
    while node[0] == "attr":
        parts.append(node[2])
        node = node[1]
    if node[0] != "name":
        return None
    parts.append(node[1])
    return list(reversed(parts))


def _walk(node):
    """Every node under this one. A tuple that starts with a kind is a node; any other tuple is a pair of nodes."""
    if isinstance(node, tuple):
        if node and isinstance(node[0], str):
            yield node
            items = node[1:]
        else:
            items = node
        for item in items:
            yield from _walk(item)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from _walk(item)


_SERIES = {
    "open": "b_open", "high": "b_high", "low": "b_low", "close": "b_close", "volume": "b_volume",
    "time": "b_time", "time_close": "(b_time + rt.width_ms)", "hl2": "((b_high + b_low) / 2)",
    "hlc3": "((b_high + b_low + b_close) / 3)", "ohlc4": "((b_open + b_high + b_low + b_close) / 4)",
    "hlcc4": "((b_high + b_low + b_close + b_close) / 4)",
}
_SERIES_ARRAYS = {"open": "O", "high": "H", "low": "LO", "close": "C", "volume": "V", "time": "T"}
_TIME_PARTS = {"hour", "minute", "second", "dayofweek", "dayofmonth", "month", "year", "weekofyear"}
_DRAWING = {
    "plot", "plotshape", "plotchar", "plotarrow", "plotbar", "plotcandle", "bgcolor", "barcolor", "fill",
    "hline", "alertcondition", "alert", "max_bars_back", "library",
}
_QUIET_NAMESPACES = {"table", "linefill", "polyline", "log", "color", "chart"}
_STRATEGY_PROPS = {
    "netprofit", "equity", "openprofit", "grossprofit", "grossloss", "wintrades", "losstrades", "eventrades",
    "initial_capital", "max_drawdown", "max_runup", "position_entry_name", "cash", "margin_liquidation_price",
    "netprofit_percent", "grossprofit_percent", "grossloss_percent", "openprofit_percent", "max_contracts_held_all",
    "max_contracts_held_long", "max_contracts_held_short", "avg_trade", "avg_winning_trade", "avg_losing_trade",
}
_CONSTANT_NAMESPACES = {
    "display", "location", "shape", "size", "position", "xloc", "yloc", "extend", "text", "font", "plot",
    "hline", "format", "currency", "order", "alert", "barmerge", "scale", "adjustment", "splits", "dividends",
    "earnings", "session", "settlement_as_close", "backadjustment",
}
_DAYS = {"sunday": 1, "monday": 2, "tuesday": 3, "wednesday": 4, "thursday": 5, "friday": 6, "saturday": 7}
_TA_PARAMS = {
    "sma": ["source", "length"], "ema": ["source", "length"], "rma": ["source", "length"], "wma": ["source", "length"],
    "vwma": ["source", "length"], "atr": ["length"], "rsi": ["source", "length"], "stdev": ["source", "length", "biased"],
    "highest": ["source", "length"], "lowest": ["source", "length"], "highestbars": ["source", "length"],
    "lowestbars": ["source", "length"], "crossover": ["source1", "source2"], "crossunder": ["source1", "source2"],
    "cross": ["source1", "source2"], "change": ["source", "length"], "mom": ["source", "length"], "roc": ["source", "length"],
    "pivothigh": ["source", "leftbars", "rightbars"], "pivotlow": ["source", "leftbars", "rightbars"],
    "valuewhen": ["condition", "source", "occurrence"], "barssince": ["condition"], "cum": ["source"],
    "rising": ["source", "length"], "falling": ["source", "length"], "vwap": ["source", "anchor", "stdev_mult"],
    "macd": ["source", "fastlen", "slowlen", "siglen"], "bb": ["series", "length", "mult"],
    "stoch": ["source", "high", "low", "length"], "tr": ["handle_na"],
}


class _Scope:
    def __init__(self, name: str, names: set) -> None:
        self.name = name
        self.locals = names


class _Program:
    def __init__(self, settings: dict, code: str, factory, sites: int, line_map: dict) -> None:
        self.settings = settings
        self.code = code
        self.factory = factory
        self.sites = sites
        self.line_map = line_map


class _Compiler:
    def __init__(self, tree: list) -> None:
        self.tree = tree
        self.out: list[str] = []
        self.line_map: dict[int, int] = {}
        self.sites = 0
        self.script_vars: set[str] = set()
        self.var_vars: set[str] = set()
        self.hist_names: set[str] = set()
        self.funcs: dict[str, tuple] = {}
        self.types: dict[str, list] = {}
        self.inputs: list[tuple] = []
        self.hoisted: set[int] = set()
        self.source_inputs: set[str] = set()
        self.settings: dict = {}
        self.guards = 0
        self.used: set[str] = set()

    # Analysis.

    def _scan(self, statements, top: bool) -> None:
        for statement in statements:
            kind = statement[0]
            if kind == "decl":
                _, mode, _, name, value, _ = statement
                self.script_vars.add(name)
                if mode:
                    self.var_vars.add(name)
                chain = _chain(value[1]) if value[0] == "call" else None
                if top and not mode and chain and chain[0] == "input":
                    self.inputs.append((name, value))
                    self.hoisted.add(id(statement))
                    if chain == ["input", "source"]:
                        self.source_inputs.add(name)
                self._scan_value(value)
            elif kind == "assign":
                self._scan_value(statement[3])
            elif kind == "switch":
                for _, body in statement[2]:
                    self._scan(body, False)
            elif kind == "tdecl":
                self.script_vars.update(statement[1])
            elif kind == "if":
                for _, body in statement[1]:
                    self._scan(body, False)
                if statement[2]:
                    self._scan(statement[2], False)
            elif kind == "for":
                self.script_vars.add(statement[1])
                self._scan(statement[5], False)
            elif kind == "forin":
                self.script_vars.update(statement[1])
                self._scan(statement[3], False)
            elif kind == "while":
                self._scan(statement[2], False)
            elif kind == "func":
                if not top:
                    raise PineUnsupported(f"Line {statement[5]}: a function must be declared at the top of the script.")
                self.funcs[statement[1]] = statement
            elif kind == "type":
                self.types[statement[1]] = statement[2]
            elif kind == "expr" and top:
                node = statement[1]
                chain = _chain(node[1]) if node[0] == "call" else None
                if chain == ["strategy"]:
                    self.settings = self._settings(node)
                elif chain == ["indicator"]:
                    raise PineUnsupported("This script is an indicator, not a strategy.")
        for node in _walk(statements):
            if node[0] == "hist" and isinstance(node[1], tuple) and node[1][0] == "name":
                self.hist_names.add(node[1][1])

    def _scan_value(self, value) -> None:
        if value[0] == "ifv":
            for _, body in value[1]:
                self._scan(body, False)
            if value[2]:
                self._scan(value[2], False)
        elif value[0] == "switchv":
            for _, body in value[2]:
                self._scan(body, False)

    def _settings(self, node) -> dict:
        args, kwargs = node[2], node[3]
        found = {"title": self._constant_value(args[0]) if args else self._constant_value(kwargs.get("title", ("str", "")))}
        for key, value in kwargs.items():
            found[key] = self._constant_value(value)
        return found

    def _constant_value(self, node):
        kind = node[0]
        if kind in ("num", "str", "bool"):
            return node[1]
        if kind == "neg" and node[1][0] == "num":
            return -node[1][1]
        chain = _chain(node) if kind == "attr" else None
        if chain:
            return chain[-1]
        return None

    # Output.

    def emit(self, depth: int, text: str, line: int | None = None) -> None:
        self.out.append("    " * depth + text)
        if line:
            self.line_map[len(self.out)] = line

    def program(self) -> _Program:
        self._scan(self.tree, True)
        if not self.settings:
            raise PineUnsupported("The script has no strategy() declaration.")
        body = []
        saved = self.out
        self.out = body
        functions = [statement for statement in self.tree if statement[0] == "func"]
        for statement in functions:
            self._function(statement, 2)
        main = [statement for statement in self.tree if statement[0] not in ("func", "type")]
        self._statements(main, 2, None)
        self.out = saved
        self.emit(0, "def __build(rt, L):")
        self.emit(1, "_nan = L.nan; _t = L.t; _land = L.land; _lor = L.lor; _isna = L.isna; _nz = L.nz; _ne = L.ne; _div = L.div; _mod = L.mod")
        self.emit(1, "_hx = L.hx; _hv = L.hv; _hs = L.hs; _rng = L.rng; _UNSET = L.UNSET; _meth = L.method; _tp = L.time_part")
        self.emit(1, "_items = L.items; _int = L.to_int; _float = L.to_float")
        self.emit(1, "O = rt.O; H = rt.H; LO = rt.L; C = rt.C; V = rt.V; T = rt.T; N = rt.n; SITE = rt.sites; TA = L.TA; MATH = L.MATH")
        self.emit(1, "ARRAY = L.ARRAY; STR = L.STR; BOX = L.BOX; LINE = L.LINE")
        self.emit(1, "def _hsv(st, k, value):")
        self.emit(2, "return value if k == 0 else _hs(st, k)")
        for name, fields in self.types.items():
            self._type(name, fields)
        for name, value in self.inputs:
            self.emit(1, f"IN_{name} = {self._input(name, value, hoisted=True)}")
        for name in sorted(self.var_vars):
            self.emit(1, f"v_{name} = _UNSET")
        tracked = sorted(name for name in self.hist_names if name in self.script_vars)
        for name in tracked:
            self.emit(1, f"h_{name} = []")
        self.emit(1, "def __bar(i):")
        if self.var_vars:
            self.emit(2, "nonlocal " + ", ".join(f"v_{name}" for name in sorted(self.var_vars)))
        self.emit(2, "b_open = O[i]; b_high = H[i]; b_low = LO[i]; b_close = C[i]; b_volume = V[i]; b_time = T[i]")
        self.emit(2, "s_pos = rt.broker.position; s_avg = rt.avg_price(); s_closed = len(rt.broker.trades); s_open = 1 if s_pos else 0")
        for name in sorted(self.script_vars - self.var_vars):
            self.emit(2, f"v_{name} = _nan")
        for name in sorted(self.script_vars & {name for name, _ in self.inputs}):
            self.emit(2, f"v_{name} = IN_{name}" if name not in self.source_inputs else f"v_{name} = rt.source(IN_{name}, i)")
        offset = len(self.out)
        for line_number, text in enumerate(body, 1):
            self.out.append(text)
        for generated, pine_line in list(self._body_map.items()):
            self.line_map[offset + generated] = pine_line
        for name in tracked:
            self.emit(2, f"h_{name}.append(v_{name})")
        self.emit(2, "rt.pos_hist.append(s_pos); rt.avg_hist.append(s_avg)")
        self.emit(1, "return __bar")
        code = "\n".join(self.out) + "\n"
        namespace: dict = {}
        exec(compile(code, "<pine>", "exec"), namespace)
        return _Program(self.settings, code, namespace["__build"], self.sites, self.line_map)

    @property
    def _body_map(self) -> dict:
        return getattr(self, "_saved_map", {})

    def _type(self, name: str, fields: list) -> None:
        """A user type. .new() fills each missing field's default where it is called, so a default can read the bar."""
        params = ", ".join(field for field, _ in fields)
        self.emit(1, f"class T_{name}:")
        self.emit(2, "__slots__ = (" + "".join(f"'{field}', " for field, _ in fields) + ")")
        self.emit(2, f"def __init__(self{', ' + params if params else ''}):")
        if fields:
            for field, _ in fields:
                self.emit(3, f"self.{field} = {field}")
        else:
            self.emit(3, "pass")

    # Statements.

    def _statements(self, statements, depth: int, scope) -> None:
        before = len(self.out)
        for statement in statements:
            self._statement(statement, depth, scope)
        if len(self.out) == before:
            self._line(depth, "pass", None)

    def _line(self, depth: int, text: str, line: int | None) -> None:
        self.out.append("    " * depth + text)
        if line:
            mapping = getattr(self, "_saved_map", None)
            if mapping is None:
                mapping = self._saved_map = {}
            mapping[len(self.out)] = line

    def _statement(self, statement, depth: int, scope) -> None:
        kind = statement[0]
        line = statement[-1]
        if kind == "decl":
            _, mode, _, name, value, _ = statement
            target = self._target(name, scope)
            if scope is None and id(statement) in self.hoisted:
                return
            if mode and scope is not None:
                raise PineUnsupported(f"Line {line}: var inside a function is not supported yet.")
            if value[0] in ("ifv", "switchv"):
                if mode:
                    self._line(depth, f"if {target} is _UNSET:", line)
                    depth += 1
                self._branches(value, depth, scope, line, lambda body, d: self._tail(body, d, scope, f"{target} = "))
                return
            text = self._c(value, scope)
            if mode:
                self._line(depth, f"if {target} is _UNSET:", line)
                self._line(depth + 1, f"{target} = {text}", line)
            else:
                self._line(depth, f"{target} = {text}", line)
        elif kind == "tdecl":
            names = ", ".join(self._target(name, scope) for name in statement[1])
            self._line(depth, f"{names} = {self._c(statement[2], scope)}", line)
        elif kind == "assign":
            _, target_node, op, value, _ = statement
            if target_node[0] == "name":
                target = self._target(target_node[1], scope)
            elif target_node[0] == "attr":
                target = f"{self._c(target_node[1], scope)}.{target_node[2]}"
            else:
                raise PineUnsupported(f"Line {line}: cannot assign to this target.")
            if value[0] in ("ifv", "switchv"):
                self._branches(value, depth, scope, line, lambda body, d: self._tail(body, d, scope, f"{target} = "))
                return
            text = self._c(value, scope)
            if op == ":=":
                self._line(depth, f"{target} = {text}", line)
            elif op == "/=":
                self._line(depth, f"{target} = _div({target}, {text})", line)
            elif op == "%=":
                self._line(depth, f"{target} = _mod({target}, {text})", line)
            else:
                self._line(depth, f"{target} = {target} {op[0]} ({text})", line)
        elif kind == "if":
            for number, (condition, body) in enumerate(statement[1]):
                word = "if" if number == 0 else "elif"
                self._line(depth, f"{word} {self._b(condition, scope)}:", line)
                self._statements(body, depth + 1, scope)
            if statement[2] is not None:
                self._line(depth, "else:", line)
                self._statements(statement[2], depth + 1, scope)
        elif kind == "for":
            _, name, start, end, step, body, _ = statement
            step_text = self._c(step, scope) if step is not None else "None"
            self._line(depth, f"for {self._target(name, scope)} in _rng({self._c(start, scope)}, {self._c(end, scope)}, {step_text}):", line)
            self._statements(body, depth + 1, scope)
        elif kind == "forin":
            _, names, iterable, body, _ = statement
            source = self._c(iterable, scope)
            if len(names) == 1:
                self._line(depth, f"for {self._target(names[0], scope)} in list(_items({source})):", line)
            else:
                self._line(depth, f"for {self._target(names[0], scope)}, {self._target(names[1], scope)} in enumerate(list(_items({source}))):", line)
            self._statements(body, depth + 1, scope)
        elif kind == "while":
            self.guards += 1
            guard = f"_g{self.guards}"
            self._line(depth, f"{guard} = 0", line)
            self._line(depth, f"while {self._b(statement[1], scope)}:", line)
            self._line(depth + 1, f"{guard} += 1", line)
            self._line(depth + 1, f"if {guard} > 200000: raise L.PineError('The while loop on line {line} did not end.')", line)
            self._statements(statement[2], depth + 1, scope)
        elif kind == "switch":
            self._branches(("switchv", statement[1], statement[2]), depth, scope, line, lambda body, d: self._statements(body, d, scope))
        elif kind in ("break", "continue"):
            self._line(depth, kind, line)
        elif kind == "expr":
            node = statement[1]
            if self._quiet(node):
                return
            self._line(depth, self._c(node, scope), line)
        elif kind == "func":
            raise PineUnsupported(f"Line {line}: a function must be declared at the top of the script.")
        elif kind == "type":
            raise PineUnsupported(f"Line {line}: a type must be declared at the top of the script.")

    def _function(self, statement, depth: int) -> None:
        _, name, params, body, value, line = statement
        names = {param for param, _ in params}
        if body:
            for node in _walk(body):
                if node[0] in ("decl",):
                    names.add(node[3])
                elif node[0] == "tdecl":
                    names.update(node[1])
                elif node[0] == "for":
                    names.add(node[1])
                elif node[0] == "forin":
                    names.update(node[1])
        scope = _Scope(name, names)
        signature = ["__site"]
        for param, default in params:
            signature.append(f"l_{param}" + (f"={self._c(default, scope)}" if default is not None else ""))
        self._line(depth, f"def u_{name}({', '.join(signature)}):", line)
        if value is not None:
            self._line(depth + 1, f"return {self._c(value, scope)}", line)
            return
        self._tail(body, depth + 1, scope, "return ")

    def _tail(self, body, depth: int, scope, lead: str) -> None:
        """Run a block and hand its value, the value of its last line, to lead (an assignment or a return)."""
        if not body:
            self._line(depth, f"{lead}_nan", None)
            return
        *head, last = body
        for item in head:
            self._statement(item, depth, scope)
        kind = last[0]
        line = last[-1]
        if kind == "expr":
            self._line(depth, f"{lead}{self._c(last[1], scope)}", line)
        elif kind == "decl":
            self._statement(last, depth, scope)
            self._line(depth, f"{lead}{self._target(last[3], scope)}", line)
        elif kind == "tdecl":
            self._statement(last, depth, scope)
            self._line(depth, lead + "(" + ", ".join(self._target(item, scope) for item in last[1]) + ",)", line)
        elif kind == "assign" and last[1][0] == "name":
            self._statement(last, depth, scope)
            self._line(depth, f"{lead}{self._target(last[1][1], scope)}", line)
        elif kind == "if":
            self._branches(("ifv", last[1], last[2]), depth, scope, line, lambda inner, d: self._tail(inner, d, scope, lead))
        elif kind == "switch":
            self._branches(("switchv", last[1], last[2]), depth, scope, line, lambda inner, d: self._tail(inner, d, scope, lead))
        else:
            self._statement(last, depth, scope)
            self._line(depth, f"{lead}_nan", line)

    def _branches(self, value, depth: int, scope, line: int, body_fn) -> None:
        """An if or switch, each branch compiled by body_fn. A missing else still runs body_fn on nothing."""
        if value[0] == "ifv":
            for number, (condition, body) in enumerate(value[1]):
                self._line(depth, f"{'if' if number == 0 else 'elif'} {self._b(condition, scope)}:", line)
                body_fn(body, depth + 1)
            self._line(depth, "else:", line)
            if value[2] is not None:
                body_fn(value[2], depth + 1)
            else:
                body_fn([], depth + 1)
            return
        subject, cases = value[1], value[2]
        holder = None
        if subject is not None:
            self.guards += 1
            holder = f"_s{self.guards}"
            self._line(depth, f"{holder} = {self._c(subject, scope)}", line)
        default = None
        first = True
        for match, body in cases:
            if match is None:
                default = body
                continue
            test = f"({holder} == {self._c(match, scope)})" if holder else self._b(match, scope)
            self._line(depth, f"{'if' if first else 'elif'} {test}:", line)
            body_fn(body, depth + 1)
            first = False
        if first:
            body_fn(default or [], depth)
            return
        self._line(depth, "else:", line)
        body_fn(default or [], depth + 1)

    def _target(self, name: str, scope) -> str:
        if scope is not None and name in scope.locals:
            return f"l_{name}"
        if scope is not None:
            raise PineUnsupported(f"A function cannot change the script variable {name}.")
        return f"v_{name}"

    # Expressions.

    def _site(self, scope) -> str:
        sid = self.sites
        self.sites += 1
        return f"SITE[{sid}]" if scope is None else f"rt.site(__site, {sid})"

    def _b(self, node, scope) -> str:
        text = self._c(node, scope)
        return text if self._is_bool(node) else f"_t({text})"

    def _is_bool(self, node) -> bool:
        kind = node[0]
        if kind in ("and", "or", "not", "bool"):
            return True
        if kind == "bin" and node[1] in ("==", "!=", "<", ">", "<=", ">="):
            return True
        if kind == "call":
            chain = _chain(node[1])
            if chain == ["na"]:
                return True
            if chain and len(chain) == 2 and chain[0] == "ta" and chain[1] in L.BOOL_TA:
                return True
        return False

    def _c(self, node, scope) -> str:
        kind = node[0]
        if kind == "num":
            return repr(node[1])
        if kind in ("str", "color"):
            return repr(node[1])
        if kind == "bool":
            return "True" if node[1] else "False"
        if kind == "tuple":
            items = [self._c(item, scope) for item in node[1]]
            return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"
        if kind == "name":
            return self._name(node[1], scope)
        if kind == "attr":
            return self._attr(node, scope)
        if kind == "call":
            return self._call(node, scope)
        if kind == "hist":
            return self._history(node, scope)
        if kind == "neg":
            return f"(-{self._c(node[1], scope)})"
        if kind == "not":
            return f"(not {self._b(node[1], scope)})"
        if kind in ("and", "or"):
            # Pine evaluates both sides on every bar. A ta.* call on the right
            # must keep its history even when the left side is false.
            fn = "_land" if kind == "and" else "_lor"
            return f"{fn}({self._b(node[1], scope)}, {self._b(node[2], scope)})"
        if kind == "tern":
            return f"({self._c(node[2], scope)} if {self._b(node[1], scope)} else {self._c(node[3], scope)})"
        if kind == "bin":
            op = node[1]
            left = self._c(node[2], scope)
            right = self._c(node[3], scope)
            if op == "/":
                return f"_div({left}, {right})"
            if op == "%":
                return f"_mod({left}, {right})"
            if op == "!=":
                return f"_ne({left}, {right})"
            return f"({left} {op} {right})"
        raise PineUnsupported(f"Cannot compile {kind}.")

    def _name(self, name: str, scope) -> str:
        if scope is not None and name in scope.locals:
            return f"l_{name}"
        if name in self.script_vars:
            return f"v_{name}"
        if name in _SERIES:
            return _SERIES[name]
        if name == "na":
            return "_nan"
        if name == "bar_index":
            return "i"
        if name == "last_bar_index":
            return "(N - 1)"
        if name == "timenow":
            return "T[N - 1]"
        if name in _TIME_PARTS:
            return f"_tp('{name}', b_time, rt.tz)"
        raise PineUnsupported(f"Unknown name {name}.")

    def _attr(self, node, scope) -> str:
        chain = _chain(node)
        if chain and not self._is_value(chain[0], scope):
            return self._constant(chain)
        return f"{self._c(node[1], scope)}.{node[2]}"

    def _is_value(self, name: str, scope) -> bool:
        return (scope is not None and name in scope.locals) or name in self.script_vars

    def _constant(self, chain: list[str]) -> str:
        head = chain[0]
        text = ".".join(chain)
        if head == "strategy":
            rest = chain[1:]
            if rest == ["long"]:
                return "'long'"
            if rest == ["short"]:
                return "'short'"
            if rest == ["position_size"]:
                return "s_pos"
            if rest == ["position_avg_price"]:
                return "s_avg"
            if rest == ["opentrades"]:
                return "s_open"
            if rest == ["closedtrades"]:
                return "s_closed"
            if len(rest) == 1 and rest[0] in _STRATEGY_PROPS:
                return f"rt.prop('{rest[0]}')"
            if rest and rest[0] in ("fixed", "cash", "percent_of_equity"):
                return repr(rest[0])
            if rest == ["default_qty_value"]:
                return "rt.qty_value"
            if rest == ["default_qty_type"]:
                return "rt.qty_type"
            if rest and rest[0] in ("commission", "oca", "direction"):
                return repr(rest[-1])
            raise PineUnsupported(f"{text} is not supported yet.")
        if head == "syminfo":
            return f"rt.syminfo('{chain[1]}')"
        if head == "timeframe":
            return f"rt.timeframe_attr('{chain[1]}')"
        if head == "barstate":
            states = {
                "isconfirmed": "True", "isfirst": "(i == 0)", "islast": "(i == N - 1)", "isrealtime": "False",
                "isnew": "True", "ishistory": "True", "islastconfirmedhistory": "(i == N - 1)",
            }
            if chain[1] in states:
                return states[chain[1]]
            raise PineUnsupported(f"{text} is not supported yet.")
        if head == "math" and chain[1] in L.MATH_CONST:
            return repr(L.MATH_CONST[chain[1]])
        if head == "dayofweek" and chain[1] in _DAYS:
            return str(_DAYS[chain[1]])
        if head == "ta" and chain[1] == "tr":
            return "L.true_range(rt, i, False)"
        if head == "color" or head in _CONSTANT_NAMESPACES or head in ("label", "line", "box", "table"):
            return repr(text)
        raise PineUnsupported(f"{text} is not supported yet.")

    def _quiet(self, node) -> bool:
        """A call that draws or alerts and cannot change a trade."""
        if node[0] != "call":
            return False
        chain = _chain(node[1])
        if not chain or self._is_value(chain[0], None):
            return False
        if chain == ["strategy"]:
            return True
        if len(chain) == 1 and chain[0] in _DRAWING:
            return True
        return chain[0] in _QUIET_NAMESPACES or chain[0] == "label"

    def _args(self, args, kwargs, scope, prefix: str = "") -> str:
        parts = [self._c(arg, scope) for arg in args]
        parts += [f"{prefix}{key}={self._c(value, scope)}" for key, value in kwargs.items()]
        return ", ".join(parts)

    def _call(self, node, scope, site: str | None = None) -> str:
        func, args, kwargs = node[1], node[2], node[3]
        chain = _chain(func)
        if chain is None:
            if func[0] == "attr":
                return f"_meth({self._c(func[1], scope)}, '{func[2]}'{', ' if args else ''}{self._args(args, {}, scope)})"
            raise PineUnsupported("Calling this value is not supported yet.")
        head = chain[0]
        if self._is_value(head, scope):
            base = self._c(func[1], scope)
            if func[0] != "attr":
                raise PineUnsupported(f"{head} is not a function.")
            return f"_meth({base}, '{func[2]}'{', ' if args else ''}{self._args(args, {}, scope)})"
        if len(chain) == 1:
            return self._plain_call(head, args, kwargs, scope, site)
        if head in self.types and chain[1:] == ["new"]:
            fields = self.types[head]
            if len(args) > len(fields):
                raise PineUnsupported(f"{head}.new has more values than fields.")
            values = []
            for index, (field, default) in enumerate(fields):
                if index < len(args):
                    values.append(self._c(args[index], scope))
                elif field in kwargs:
                    values.append(self._c(kwargs[field], scope))
                else:
                    values.append(self._c(default, scope) if default is not None else "_nan")
            return f"T_{head}({', '.join(values)})"
        return self._namespace_call(chain, args, kwargs, scope, site)

    def _plain_call(self, name: str, args, kwargs, scope, site) -> str:
        if name in self.funcs:
            params = [param for param, _ in self.funcs[name][2]]
            sid = self.sites
            self.sites += 1
            key = str(sid) if scope is None else f"(__site, {sid})"
            parts = [key] + [self._c(arg, scope) for arg in args]
            for key_name, value in kwargs.items():
                if key_name not in params:
                    raise PineUnsupported(f"{name} has no parameter {key_name}.")
                parts.append(f"l_{key_name}={self._c(value, scope)}")
            return f"u_{name}({', '.join(parts)})"
        compiled = [self._c(arg, scope) for arg in args]
        if name == "na":
            return f"_isna({compiled[0]})" if compiled else "_nan"
        if name == "nz":
            return f"_nz({compiled[0]}, {compiled[1] if len(compiled) > 1 else '0'})"
        if name == "fixnan":
            return f"L.ta_fixnan({site or self._site(scope)}, i, rt, {compiled[0]})"
        if name == "int":
            return f"_int({compiled[0]})"
        if name == "float":
            return f"_float({compiled[0]})"
        if name == "bool":
            return f"_t({compiled[0]})"
        if name == "string":
            return f"str({compiled[0]})"
        if name in _TIME_PARTS:
            moment = compiled[0] if compiled else "b_time"
            zone = compiled[1] if len(compiled) > 1 else self._c(kwargs["timezone"], scope) if "timezone" in kwargs else "rt.tz"
            return f"_tp('{name}', {moment}, {zone})"
        if name in ("time", "time_close"):
            if len(args) >= 2 or "session" in kwargs:
                session = compiled[1] if len(compiled) > 1 else self._c(kwargs["session"], scope)
                zone = compiled[2] if len(compiled) > 2 else self._c(kwargs["timezone"], scope) if "timezone" in kwargs else "rt.tz"
                return f"rt.session_time(i, {session}, {zone}, {name == 'time_close'})"
            if args:
                return f"rt.period_open(i, {compiled[0]})"
            return "b_time" if name == "time" else "(b_time + rt.width_ms)"
        if name == "timestamp":
            return f"rt.timestamp({', '.join(compiled)})"
        if name == "input":
            return self._input(None, ("call", ("name", "input"), args, kwargs), hoisted=False, scope=scope)
        if name in _DRAWING or name == "strategy":
            return "None"
        if name == "indicator":
            raise PineUnsupported("This script is an indicator, not a strategy.")
        if name in ("color", "label", "line", "box", "table", "array"):
            return compiled[0] if compiled else "None"
        raise PineUnsupported(f"The function {name} is not supported yet.")

    def _namespace_call(self, chain, args, kwargs, scope, site) -> str:
        head, fn = chain[0], chain[-1]
        text = ".".join(chain)
        if head == "request" and fn == "security":
            return self._security(args, kwargs, scope)
        if head == "ta" and len(chain) == 2:
            if fn not in L.TA:
                raise PineUnsupported(f"{text} is not supported yet.")
            where = site or self._site(scope)
            joined = ", ".join(self._c(arg, scope) for arg in self._ordered(fn, args, kwargs))
            return f"TA['{fn}']({where}, i, rt{', ' if joined else ''}{joined})"
        if head == "math" and len(chain) == 2:
            if fn == "sum":
                where = site or self._site(scope)
                return f"L.ta_sum({where}, i, rt, {self._args(args, kwargs, scope)})"
            if fn == "round_to_mintick":
                return f"rt.round_tick({self._args(args, kwargs, scope)})"
            if fn not in L.MATH:
                raise PineUnsupported(f"{text} is not supported yet.")
            return f"MATH['{fn}']({self._args(args, kwargs, scope)})"
        if head == "array" and len(chain) == 2:
            if fn not in L.ARRAY:
                raise PineUnsupported(f"{text} is not supported yet.")
            return f"ARRAY['{fn}']({self._args(args, kwargs, scope)})"
        if head == "str" and len(chain) == 2:
            if fn not in L.STR:
                raise PineUnsupported(f"{text} is not supported yet.")
            return f"STR['{fn}']({self._args(args, {}, scope)})"
        if head == "strategy":
            rest = chain[1:]
            if rest in (["entry"], ["exit"], ["close"], ["close_all"], ["cancel"], ["cancel_all"]):
                return f"rt.st_{rest[0]}({self._args(args, kwargs, scope)})"
            if len(rest) == 2 and rest[0] in ("closedtrades", "opentrades"):
                which = "closed" if rest[0] == "closedtrades" else "opened"
                return f"rt.{which}('{rest[1]}', {self._args(args, kwargs, scope) or '0'})"
            if rest in (["convert_to_account"], ["convert_to_symbol"]):
                return self._c(args[0], scope)
            raise PineUnsupported(f"{text} is not supported yet.")
        if head == "input":
            return self._input(None, ("call", ("attr", ("name", "input"), fn), args, kwargs), hoisted=False, scope=scope)
        if head == "box" and len(chain) == 2:
            if fn in L.BOX:
                return f"BOX['{fn}']({self._args(args, kwargs, scope)})"
            return "None"
        if head == "line" and len(chain) == 2:
            if fn in L.LINE:
                return f"LINE['{fn}']({self._args(args, kwargs, scope)})"
            return "None"
        if head == "label":
            return "L.Label()" if fn == "new" else "None"
        if head in _QUIET_NAMESPACES:
            return "None"
        if head == "runtime" and fn == "error":
            return f"rt.fail({self._args(args, {}, scope)})"
        raise PineUnsupported(f"{text} is not supported yet.")

    def _ordered(self, fn: str, args, kwargs) -> list:
        """ta.* arguments in Pine's order, so a named argument lands where the function expects it."""
        if not kwargs:
            return list(args)
        names = _TA_PARAMS.get(fn)
        if names is None:
            raise PineUnsupported(f"Named arguments to ta.{fn} are not supported yet.")
        ordered = list(args)
        for name in names[len(args):]:
            if name in kwargs:
                ordered.append(kwargs[name])
            elif any(later in kwargs for later in names[names.index(name) + 1:]):
                raise PineUnsupported(f"ta.{fn} skips the argument {name}.")
            else:
                break
        return ordered

    def _security(self, args, kwargs, scope) -> str:
        """A higher-timeframe hma, RSI, or linear regression, read after that bar closes."""
        if len(args) < 3 and "expression" not in kwargs:
            raise PineUnsupported("request.security needs a symbol, a timeframe, and an expression.")
        expression = args[2] if len(args) > 2 else kwargs.get("expression")
        timeframe = args[1] if len(args) > 1 else kwargs.get("timeframe")
        if expression is None or timeframe is None:
            raise PineUnsupported("request.security needs a symbol, a timeframe, and an expression.")
        lookahead = kwargs.get("lookahead")
        chain = _chain(lookahead) if isinstance(lookahead, tuple) else None
        if chain and chain[-1] == "lookahead_on":
            raise PineUnsupported("request.security lookahead_on is not used. The higher-timeframe bar is read after it closes.")
        if expression[0] != "call":
            raise PineUnsupported("request.security on this desk supports hma(), ta.rsi(), and ta.linreg().")
        func, call_args, call_kwargs = expression[1], expression[2], expression[3]
        kind = _chain(func)
        source = call_args[0] if call_args else call_kwargs.get("source") or call_kwargs.get("src")
        if not (isinstance(source, tuple) and source[0] == "name" and source[1] == "close"):
            raise PineUnsupported("request.security on this desk reads the chart close.")
        if kind == ["hma"]:
            length = call_args[1] if len(call_args) > 1 else call_kwargs.get("length")
            name = "hma"
            offset = None
        elif kind == ["ta", "rsi"]:
            length = call_args[1] if len(call_args) > 1 else call_kwargs.get("length")
            name = "rsi"
            offset = None
        elif kind == ["ta", "linreg"]:
            length = call_args[1] if len(call_args) > 1 else call_kwargs.get("length")
            offset = call_args[2] if len(call_args) > 2 else call_kwargs.get("offset")
            name = "linreg"
        else:
            raise PineUnsupported("request.security on this desk supports hma(), ta.rsi(), and ta.linreg().")
        if length is None:
            raise PineUnsupported("request.security is missing the length.")
        span = self._c(timeframe, scope)
        length_text = self._c(length, scope)
        offset_text = self._c(offset, scope) if offset is not None else "0"
        return f"rt.security_series(i, {span}, '{name}', {length_text}, {offset_text})"

    def _input(self, name, node, hoisted: bool, scope=None) -> str:
        chain = _chain(node[1])
        kind = chain[1] if len(chain) > 1 else "auto"
        args, kwargs = node[2], node[3]
        if kind == "source":
            default = args[0] if args else kwargs.get("defval", ("name", "close"))
            key = default[1] if default[0] == "name" else "close"
            title = self._c(args[1], None) if len(args) > 1 else self._c(kwargs["title"], None) if "title" in kwargs else "''"
            if not hoisted:
                raise PineUnsupported("input.source inside an expression is not supported yet.")
            return f"rt.input_source({name!r}, {key!r}, {title})"
        default = args[0] if args else kwargs.get("defval")
        title = args[1] if len(args) > 1 else kwargs.get("title")
        options = kwargs.get("options")
        default_text = self._c(default, scope) if default is not None else "_nan"
        title_text = self._c(title, scope) if title is not None else "''"
        options_text = self._c(options, scope) if options is not None else "None"
        return f"rt.input({name!r}, {kind!r}, {default_text}, {title_text}, {options_text})"

    def _history(self, node, scope) -> str:
        base, index = node[1], node[2]
        k = self._c(index, scope)
        if base[0] == "name":
            name = base[1]
            if scope is not None and name in scope.locals:
                raise PineUnsupported(f"History on the function variable {name} is not supported yet.")
            if name in self.source_inputs:
                return f"_hx(rt.series(IN_{name}), i, {k})"
            if name in self.script_vars:
                return f"_hv(h_{name}, {k}, v_{name})"
            if name in _SERIES_ARRAYS:
                return f"_hx({_SERIES_ARRAYS[name]}, i, {k})"
            if name in ("hl2", "hlc3", "ohlc4", "hlcc4"):
                return f"rt.composite('{name}', i, {k})"
            if name == "bar_index":
                return f"(i - ({k}))"
            raise PineUnsupported(f"History on {name} is not supported yet.")
        chain = _chain(base)
        if chain == ["strategy", "position_size"]:
            return f"_hv(rt.pos_hist, {k}, s_pos)"
        if chain == ["strategy", "position_avg_price"]:
            return f"_hv(rt.avg_hist, {k}, s_avg)"
        if base[0] == "call":
            inner = _chain(base[1])
            if inner and inner[0] == "ta" and len(inner) == 2:
                where = self._site(scope)
                call = self._call(base, scope, site=where)
                return f"_hsv({where}, {k}, {call})"
        raise PineUnsupported("History on this expression is not supported yet.")


# ---------- runtime ----------

_EXCHANGE_TZ = {
    "ES": "America/Chicago", "MES": "America/Chicago", "NQ": "America/Chicago", "MNQ": "America/Chicago",
    "YM": "America/Chicago", "MYM": "America/Chicago", "RTY": "America/Chicago", "M2K": "America/Chicago",
    "QO": "America/New_York", "GC": "America/New_York", "MGC": "America/New_York", "SI": "America/New_York",
    "CL": "America/New_York", "MCL": "America/New_York",
}
_MINTICK = {"ES": 0.25, "MES": 0.25, "NQ": 0.25, "MNQ": 0.25, "YM": 1.0, "RTY": 0.1, "QO": 0.25, "GC": 0.1, "MGC": 0.1, "CL": 0.01}
_TICKER = {"ES": "CME_MINI:ES1!", "NQ": "CME_MINI:NQ1!", "QO": "COMEX_MINI:QO1!", "GC": "COMEX:GC1!", "CL": "NYMEX:CL1!"}
_MINUTES = {"1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


def _wma_series(values: list, length: int) -> list:
    span = max(int(length), 1)
    denom = span * (span + 1) / 2.0
    out = [L.nan] * len(values)
    for index in range(span - 1, len(values)):
        window = values[index - span + 1:index + 1]
        if any(L.isna(value) for value in window):
            continue
        out[index] = sum(value * (step + 1) for step, value in enumerate(window)) / denom
    return out


def _hma_series(values: list, length: int) -> list:
    span = max(int(length), 1)
    half = max(span // 2, 1)
    root = max(int(math.floor(math.sqrt(span))), 1)
    fast = _wma_series(values, half)
    slow = _wma_series(values, span)
    diff = [L.nan if L.isna(left) or L.isna(right) else 2.0 * left - right for left, right in zip(fast, slow)]
    return _wma_series(diff, root)


def _rsi_series(values: list, length: int) -> list:
    up = L._Average()
    down = L._Average()
    out = []
    previous = None
    for value in values:
        if previous is None or L.isna(previous) or L.isna(value):
            out.append(L.nan)
            previous = value
            continue
        change = value - previous
        previous = value
        gained = up.update(max(change, 0.0), length, True)
        lost = down.update(max(-change, 0.0), length, True)
        if L.isna(gained) or L.isna(lost):
            out.append(L.nan)
        elif lost == 0:
            out.append(100.0)
        elif gained == 0:
            out.append(0.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + gained / lost))
    return out


def _linreg_series(values: list, length: int, offset: int = 0) -> list:
    span = max(int(length), 1)
    shift = int(offset or 0)
    out = [L.nan] * len(values)
    sum_x = (span - 1) * span / 2.0
    sum_xx = (span - 1) * span * (2 * span - 1) / 6.0
    denom = span * sum_xx - sum_x * sum_x
    if denom == 0:
        return out
    for index in range(span - 1, len(values)):
        window = values[index - span + 1:index + 1]
        if any(L.isna(value) for value in window):
            continue
        sum_y = sum(window)
        sum_xy = sum(step * window[step] for step in range(span))
        slope = (span * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / span
        out[index] = intercept + slope * ((span - 1) - shift)
    return out


class Runtime:
    """The bars, the inputs, and the broker one compiled script runs against."""

    def __init__(self, bars, settings: dict, inputs: dict, root: str, timeframe: str) -> None:
        from futuresfund.contracts import POINT_VALUE

        self.bars = bars
        self.n = len(bars)
        self.O = [bar.o for bar in bars]
        self.H = [bar.h for bar in bars]
        self.L = [bar.l for bar in bars]
        self.C = [bar.c for bar in bars]
        self.V = [bar.v for bar in bars]
        self.T = [bar.epoch * 1000 for bar in bars]
        self.root = root.upper()
        self.mintick = _MINTICK.get(self.root, 0.25)
        self.pointvalue = float(POINT_VALUE.get(self.root, 50))
        self.tz = _EXCHANGE_TZ.get(self.root, "America/New_York")
        minutes = _MINUTES.get(timeframe, 1)
        self.minutes = minutes
        self.width_ms = minutes * 60000
        self.settings = settings
        self.overrides = {}
        for key, value in (inputs or {}).items():
            self.overrides[str(key).strip().lower()] = value
        self.capital = float(settings.get("initial_capital") or 1000000)
        self.qty_type = str(settings.get("default_qty_type") or "fixed")
        self.qty_value = float(settings.get("default_qty_value") or 1)
        self.pyramiding = int(settings.get("pyramiding") or 0)
        self.broker = Broker(
            bars,
            self.pointvalue,
            float(settings.get("commission_value") or 0),
            float(settings.get("slippage") or 0),
            tick=self.mintick,
            qty=1,
            on_close=bool(settings.get("process_orders_on_close")),
            commission_type=str(settings.get("commission_type") or "percent"),
        )
        self.pos_hist: list = []
        self.avg_hist: list = []
        self.sites: list = []
        self._function_sites: dict = {}
        self._inputs: dict = {}
        self._composites: dict = {}
        self._session_ids: list | None = None
        self._htf_buckets: dict = {}
        self._security_series: dict = {}

    # Inputs.

    def input(self, name, kind, default, title, options=None):
        value = default
        for key in (name, title):
            if key is None:
                continue
            found = self.overrides.get(str(key).strip().lower())
            if found is not None:
                value = found
                break
        return _coerce(kind, value, default)

    def input_source(self, name, default_key, title):
        value = default_key
        for key in (name, title):
            if key and str(key).strip().lower() in self.overrides:
                value = str(self.overrides[str(key).strip().lower()]).strip().lower()
                break
        return value if value in ("open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4", "hlcc4") else default_key

    def series(self, key: str) -> list:
        direct = {"open": self.O, "high": self.H, "low": self.L, "close": self.C, "volume": self.V}
        if key in direct:
            return direct[key]
        found = self._composites.get(key)
        if found is None:
            if key == "hl2":
                found = [(h + l) / 2 for h, l in zip(self.H, self.L)]
            elif key == "hlc3":
                found = [(h + l + c) / 3 for h, l, c in zip(self.H, self.L, self.C)]
            elif key == "ohlc4":
                found = [(o + h + l + c) / 4 for o, h, l, c in zip(self.O, self.H, self.L, self.C)]
            else:
                found = [(h + l + c + c) / 4 for h, l, c in zip(self.H, self.L, self.C)]
            self._composites[key] = found
        return found

    def source(self, key: str, i: int):
        return self.series(key)[i]

    def composite(self, key: str, i: int, k):
        return L.hx(self.series(key), i, k)

    # Call sites inside functions.

    def site(self, key, sid: int):
        found = self._function_sites.get((key, sid))
        if found is None:
            found = self._function_sites[(key, sid)] = L.Site()
        return found

    # Symbol and timeframe.

    def syminfo(self, name: str):
        values = {
            "mintick": self.mintick, "pointvalue": self.pointvalue, "timezone": self.tz, "currency": "USD",
            "tickerid": _TICKER.get(self.root, self.root), "ticker": f"{self.root}1!", "root": self.root,
            "type": "futures", "session": "regular", "basecurrency": "USD", "description": self.root,
        }
        if name not in values:
            raise PineUnsupported(f"syminfo.{name} is not supported yet.")
        return values[name]

    def timeframe_attr(self, name: str):
        values = {
            "period": str(self.minutes), "multiplier": self.minutes, "isintraday": True, "isminutes": True,
            "isseconds": False, "isdaily": False, "isweekly": False, "ismonthly": False, "isdwm": False,
            "isticks": False, "main_period": str(self.minutes),
        }
        if name not in values:
            raise PineUnsupported(f"timeframe.{name} is not supported yet.")
        return values[name]

    def round_tick(self, value):
        if L.isna(value):
            return L.nan
        return math.floor(value / self.mintick + 0.5 + 1e-9) * self.mintick

    # Time.

    def period_open(self, i: int, timeframe) -> int:
        """time("D") is the open of the session day that holds this bar, 18:00 New York time.

        A single argument is a timeframe, not a session. The same value holds for every bar of
        that period, so ta.change(time("D")) is true only on the first bar of a new day.
        """
        from datetime import timedelta

        stamp = int(self.T[i])
        if timeframe is None or L.isna(timeframe) or not str(timeframe).strip():
            return stamp
        key = str(timeframe).strip().upper().replace(" ", "")
        when = L.local_time(stamp, "America/New_York")

        def session_start(moment):
            start = moment.replace(hour=18, minute=0, second=0, microsecond=0)
            if (moment.hour, moment.minute) < (18, 0):
                start -= timedelta(days=1)
            return start

        if key in {"D", "1D"}:
            return int(session_start(when).timestamp() * 1000)
        if key in {"W", "1W"}:
            start = session_start(when)
            start -= timedelta(days=(start.weekday() + 1) % 7)
            return int(start.timestamp() * 1000)
        if key in {"M", "1M"}:
            start = session_start(when)
            first = start.replace(day=1)
            if first > start:
                first = (first - timedelta(days=1)).replace(day=1, hour=18, minute=0, second=0, microsecond=0)
            return int(first.timestamp() * 1000)
        digits = "".join(ch for ch in key if ch.isdigit())
        if digits and "S" not in key:
            minutes = int(digits) * (60 if key.endswith("H") else 1)
            if minutes > 0:
                epoch = stamp // 1000
                width = minutes * 60
                return (epoch - epoch % width) * 1000
        return stamp

    def security_series(self, i: int, timeframe, kind: str, length, offset=0):
        """The last closed higher-timeframe value. The bar still forming is not included."""
        import bisect

        try:
            span = int(length)
            shift = int(offset or 0)
        except (TypeError, ValueError):
            return L.nan
        if span < 1 or L.isna(timeframe):
            return L.nan
        key = (self._security_name(timeframe), kind, span, shift)
        series = self._security_series.get(key)
        if series is None:
            closes = [close for _index, close in self._closed_buckets(key[0])]
            if kind == "hma":
                series = _hma_series(closes, span)
            elif kind == "rsi":
                series = _rsi_series(closes, span)
            elif kind == "linreg":
                series = _linreg_series(closes, span, shift)
            else:
                return L.nan
            self._security_series[key] = series
        ready = self._closed_buckets(key[0])
        count = bisect.bisect_right(ready, (i, float("inf")))
        if count <= 0 or count > len(series) or L.isna(series[count - 1]):
            return L.nan
        return series[count - 1]

    def _security_name(self, timeframe) -> str:
        key = str(timeframe).strip().upper().replace(" ", "")
        if key in {"240", "4H"}:
            return "240"
        if key in {"D", "1D", "DAY"}:
            return "D"
        return key

    def _closed_buckets(self, name: str) -> list:
        found = self._htf_buckets.get(name)
        if found is not None:
            return found
        buckets = []
        current = None
        last_close = None
        for index in range(self.n):
            bucket = self._security_bucket(index, name)
            if current is None:
                current = bucket
                last_close = self.C[index]
                continue
            if bucket != current:
                buckets.append((index, last_close))
                current = bucket
                last_close = self.C[index]
            else:
                last_close = self.C[index]
        self._htf_buckets[name] = buckets
        return buckets

    def _security_bucket(self, i: int, name: str) -> int:
        from datetime import timedelta

        if name == "D":
            return self.period_open(i, "D")
        when = L.local_time(self.T[i], "America/New_York")
        start = when.replace(hour=18, minute=0, second=0, microsecond=0)
        if (when.hour, when.minute) < (18, 0):
            start -= timedelta(days=1)
        minutes = int((when - start).total_seconds() // 60)
        width = 240 if name == "240" else max(int("".join(ch for ch in name if ch.isdigit()) or "1"), 1)
        bucket = minutes // width
        return int((start + timedelta(minutes=bucket * width)).timestamp() * 1000)

    def session_time(self, i: int, session, tz, closing: bool = False):
        stamp = self.T[i]
        if session is None or L.isna(session) or not str(session).strip():
            return stamp + (self.width_ms if closing else 0)
        zone = tz if tz and not L.isna(tz) else self.tz
        when = L.local_time(stamp, zone)
        minute = when.hour * 60 + when.minute
        text = str(session)
        days = None
        if ":" in text:
            text, day_text = text.split(":", 1)
            days = {int(ch) for ch in day_text if ch.isdigit()}
        weekday = (when.weekday() + 1) % 7 + 1
        for part in text.split(","):
            part = part.strip()
            if part.lower() in ("24x7", "0000-0000"):
                inside = days is None or weekday in days
            else:
                match = re.match(r"(\d{2})(\d{2})-(\d{2})(\d{2})", part)
                if not match:
                    continue
                start = int(match.group(1)) * 60 + int(match.group(2))
                end = int(match.group(3)) * 60 + int(match.group(4))
                if start > end:
                    # The day number is the day the session ends. 1700-1700:23456 is
                    # Monday through Friday: Monday starts Sunday at 17:00 and ends
                    # Monday at 17:00. 1800-1645:12345 is Sunday through Thursday,
                    # so Friday is already shut, and the 16:45-18:00 halt is dark.
                    nxt = 1 if weekday == 7 else weekday + 1
                    started = minute >= start and (days is None or nxt in days)
                    carried = minute < end and (days is None or weekday in days)
                    inside = started or carried
                else:
                    inside = start <= minute < end and (days is None or weekday in days)
            if inside:
                return stamp + (self.width_ms if closing else 0)
        return L.nan

    def timestamp(self, *args):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        values = list(args)
        zone = self.tz
        if values and isinstance(values[0], str):
            zone = values.pop(0)
        parts = [int(value) for value in values] + [0] * (6 - len(values))
        when = datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=ZoneInfo(L._zone_name(zone)))
        return int(when.timestamp() * 1000)

    def new_session(self, i: int) -> bool:
        """The first bar of a CME trading day, which starts at 18:00 New York time."""
        if self._session_ids is None:
            from datetime import timedelta

            ids = []
            for stamp in self.T:
                when = L.local_time(stamp, "America/New_York") + timedelta(hours=6)
                ids.append(when.date())
            self._session_ids = ids
        return i == 0 or self._session_ids[i] != self._session_ids[i - 1]

    # The strategy.

    def avg_price(self):
        price = self.broker.entry_price
        return L.nan if price is None else price

    def _qty(self, qty):
        if qty is not None and not L.isna(qty):
            return max(int(qty), 1)
        if self.qty_type == "fixed":
            return max(int(self.qty_value), 1)
        price = self.C[len(self.pos_hist)] if len(self.pos_hist) < self.n else self.C[-1]
        cost = price * self.pointvalue
        if self.qty_type == "cash":
            return max(int(self.qty_value // cost), 1) if cost else 1
        equity = self.capital + self.prop("netprofit")
        return max(int((equity * self.qty_value / 100.0) // cost), 1) if cost else 1

    def st_entry(self, id, direction, qty=None, limit=None, stop=None, oca_name=None, oca_type=None,
                 comment=None, alert_message=None, disable_alert=None, when=True):
        if not L.t(when):
            return None
        if (limit is not None and not L.isna(limit)) or (stop is not None and not L.isna(stop)):
            raise PineUnsupported("strategy.entry with a limit or stop price is not supported yet.")
        side = 1 if direction in ("long", 1, True) else -1
        if self.pyramiding > 1 and self.broker.position * side > 0:
            raise PineUnsupported("Adding to a position (pyramiding) is not supported yet.")
        self.broker.entry(str(id), side, self._qty(qty))
        return None

    def st_exit(self, id, from_entry="", qty=None, qty_percent=None, profit=None, limit=None, loss=None, stop=None,
                trail_price=None, trail_points=None, trail_offset=None, oca_name=None, comment=None,
                comment_profit=None, comment_loss=None, comment_trailing=None, alert_message=None,
                alert_profit=None, alert_loss=None, alert_trailing=None, disable_alert=None, when=True):
        if not L.t(when):
            return None
        for value in (trail_price, trail_points, trail_offset):
            if value is not None and not L.isna(value):
                raise PineUnsupported("strategy.exit with a trailing stop is not supported yet.")

        def price(value):
            return None if value is None or L.isna(value) else float(value)

        self.broker.exit(
            str(id),
            "" if from_entry is None or L.isna(from_entry) else str(from_entry),
            stop=price(stop),
            limit=price(limit),
            profit=price(profit),
            loss=price(loss),
            label=str(comment) if comment and not L.isna(comment) else None,
        )
        return None

    def st_close(self, id, comment=None, qty=None, qty_percent=None, alert_message=None, immediately=False,
                 disable_alert=None, when=True):
        if not L.t(when):
            return None
        self.broker.close(str(id), str(comment) if comment else f"Close entry(s) order {id}")
        return None

    def st_close_all(self, comment=None, alert_message=None, immediately=False, disable_alert=None, when=True):
        if not L.t(when):
            return None
        self.broker.close_all(str(comment) if comment else "Close position order")
        return None

    def st_cancel(self, id, when=True):
        if L.t(when):
            self.broker.cancel(str(id))
        return None

    def st_cancel_all(self, when=True):
        if L.t(when):
            self.broker.cancel_all()
        return None

    def closed(self, field: str, index):
        trades = self.broker.trades
        k = int(index)
        if not 0 <= k < len(trades):
            return L.nan
        trade = trades[k]
        values = {
            "profit": trade["pnl"], "size": trade["size"], "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"], "entry_time": trade["entry_ms"], "exit_time": trade["exit_ms"],
            "entry_bar_index": trade["index"], "exit_bar_index": trade["exit_index"], "entry_id": trade["entry_signal"],
            "exit_id": trade["exit_signal"], "commission": trade.get("commission", 0.0),
            "entry_comment": trade["entry_signal"], "exit_comment": trade["exit_signal"],
            "max_runup": trade.get("favorable", 0.0), "max_drawdown": -min(trade.get("adverse", 0.0), 0.0),
        }
        if field not in values:
            raise PineUnsupported(f"strategy.closedtrades.{field} is not supported yet.")
        return values[field]

    def opened(self, field: str, index):
        trade = self.broker.open_trade
        if trade is None or int(index) != 0:
            return L.nan
        i = len(self.pos_hist)
        close = self.C[min(i, self.n - 1)]
        side = 1 if trade["size"] > 0 else -1
        values = {
            "entry_id": trade["entry_signal"], "entry_bar_index": trade["index"], "entry_price": trade["entry_price"],
            "entry_time": trade["entry_ms"], "size": trade["size"], "entry_comment": trade["entry_signal"],
            "profit": (close - trade["entry_price"]) * side * self.pointvalue * abs(trade["size"]) - trade["entry_fee"],
            "commission": trade["entry_fee"],
        }
        if field not in values:
            raise PineUnsupported(f"strategy.opentrades.{field} is not supported yet.")
        return values[field]

    def prop(self, name: str):
        trades = self.broker.trades
        pnls = [trade["pnl"] for trade in trades]
        if name == "netprofit":
            return sum(pnls)
        if name == "grossprofit":
            return sum(value for value in pnls if value > 0)
        if name == "grossloss":
            return -sum(value for value in pnls if value < 0)
        if name == "wintrades":
            return sum(1 for value in pnls if value > 0)
        if name == "losstrades":
            return sum(1 for value in pnls if value < 0)
        if name == "eventrades":
            return sum(1 for value in pnls if value == 0)
        if name == "initial_capital":
            return self.capital
        if name in ("openprofit", "equity", "cash"):
            open_profit = self.opened("profit", 0) if self.broker.open_trade else 0.0
            if name == "openprofit":
                return open_profit
            return self.capital + sum(pnls) + (open_profit if name == "equity" else 0.0)
        if name == "position_entry_name":
            return self.broker.entry_id
        if name == "max_drawdown":
            from futuresfund.tv_broker import tv_metrics

            return tv_metrics(trades)["max_drawdown"]
        raise PineUnsupported(f"strategy.{name} is not supported yet.")

    def fail(self, message):
        raise L.PineError(str(message))


def _coerce(kind: str, value, default):
    if L.isna(value) if not isinstance(value, str) else False:
        return value
    try:
        if kind == "int":
            return int(float(value))
        if kind == "float":
            return float(value)
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "on")
            return bool(value)
        if kind in ("string", "session", "timeframe", "symbol", "color", "text_area"):
            return value if isinstance(value, str) else str(value)
        if kind == "auto":
            if isinstance(default, bool):
                return _coerce("bool", value, default)
            if isinstance(default, int):
                return _coerce("int", value, default)
            if isinstance(default, float):
                return _coerce("float", value, default)
            return value
        return value
    except (TypeError, ValueError):
        return default


_COMPILED: dict[str, _Program] = {}
_COMPILE_LOCK = threading.Lock()


def compile_pine(source: str) -> _Program:
    key = hashlib.sha1((source or "").encode("utf-8", errors="replace")).hexdigest()
    with _COMPILE_LOCK:
        found = _COMPILED.get(key)
    if found is not None:
        return found
    compiler = _Compiler(_parse(source or ""))
    program = compiler.program()
    with _COMPILE_LOCK:
        _COMPILED[key] = program
    return program


def supported(source: str) -> bool:
    try:
        compile_pine(source)
    except PineUnsupported:
        return False
    return True


def run_pine(source: str, bars, inputs: dict | None = None, *, root: str = "ES", timeframe: str = "2m") -> list[dict]:
    """The closed trades the script takes on these bars."""
    return _run(source, bars, inputs, root, timeframe).trades


def run_pine_live(source: str, bars, inputs: dict | None = None, *, root: str = "ES", timeframe: str = "2m") -> dict:
    """The script on a live chart: its closed trades, the trade still open after the newest bar, and its waiting orders."""
    broker = _run(source, bars, inputs, root, timeframe)
    return {
        "trades": broker.trades,
        "open": dict(broker.open_trade) if broker.open_trade else None,
        "pending": broker.pending(),
        "on_close": broker.on_close,
        "slip": broker.slip,
        "broker": broker,
    }


def _run(source: str, bars, inputs: dict | None, root: str, timeframe: str):
    program = compile_pine(source)
    rt = Runtime(bars, program.settings, inputs or {}, root, timeframe)
    rt.sites = [L.Site() for _ in range(program.sites)]
    broker = rt.broker
    if (inputs or {}).get("_risk_style"):
        broker.arm_risk(inputs or {})
    try:
        step = program.factory(rt, L)
        for i in range(rt.n):
            if i and i % 250 == 0:
                time.sleep(0)
            broker.start_bar(i)
            step(i)
            broker.end_bar(i)
            # position_size on a bar is the size the script saw. A market order with
            # process_orders_on_close fills after that, so a one-bar trade does not
            # make the next bar's position_size[1] non-zero. TradingView does not
            # reverse those trades.
    except (L.PineError, PineUnsupported):
        raise
    except Exception as exc:
        raise L.PineError(f"Line {_pine_line(program, exc)}: {type(exc).__name__}: {exc}") from exc
    return broker


def _pine_line(program: _Program, exc: Exception):
    tb = sys.exc_info()[2]
    found = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<pine>":
            found = tb.tb_lineno
        tb = tb.tb_next
    if found is None:
        return "?"
    for number in range(found, 0, -1):
        if number in program.line_map:
            return program.line_map[number]
    return "?"
