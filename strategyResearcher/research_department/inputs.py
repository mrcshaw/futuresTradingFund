"""Numeric inputs a meeting is allowed to change."""

from __future__ import annotations

import re
from dataclasses import dataclass


_INPUT = re.compile(
    r'^\s*([A-Za-z_]\w*)\s*=\s*input(?:\.(int|float))?\(\s*(-?\d+(?:\.\d+)?)'
    r'(?:\s*,\s*(?:title\s*=\s*)?"([^"]*)")?',
    re.MULTILINE,
)
_TITLE = re.compile(r"strategy\(\s*\"([^\"]+)\"")


@dataclass(frozen=True)
class ScriptInput:
    name: str
    kind: str
    default: float
    title: str = ""


def strategy_title(source: str) -> str:
    match = _TITLE.search(source or "")
    return match.group(1).strip() if match else "Script"


def script_inputs(source: str) -> list[ScriptInput]:
    found = []
    for name, kind, raw, title in _INPUT.findall(source or ""):
        if not kind:
            kind = "float" if "." in raw else "int"
        value = float(raw)
        if kind == "int":
            value = float(int(value))
        found.append(ScriptInput(name, kind, value, title or ""))
    return found
