"""The chart developer adds a missing entry rule before a study starts.

A script whose long and short conditions the engine can already read does not
need a new rule. Anything else is written once, saved, and used on every chart.
"""

from __future__ import annotations

import ast
import re
import threading
from pathlib import Path

RULES = Path(__file__).resolve().parent / "entryRules"
_BUILTIN = {
    "POC Confluence",
    "Volume Profile + Smart Trail",
    "60 Candle Range Stay",
    "VWAP Breakout (Intraday)",
    "BOS Breakout Evening ES 2min",
    "EMA BB Mean Reversion",
    "Night Shift Delta Volume Breakout",
}
_PRESET = {
    "close", "open", "high", "low", "volume",
    "close_1", "open_1", "high_1", "low_1",
    "in_session", "entry_allowed",
    "cooled_down", "cooled", "cooled_off",
    "daily_locked", "strategy_position_size", "bar_index",
}
_CALLS = {"min", "max", "abs", "len", "range", "int", "float", "bool", "sum", "enumerate", "round", "any", "all"}
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def ensure_entry_rule(board, developer: str, source: str, bars: list, *, wait: bool = False) -> bool | None:
    """True when this script can enter. The developer writes the rule on this agent's own thread.

    None means another agent is already writing this rule. The caller does not wait.
    """
    title = _title(source)
    if _ready(source):
        return True
    if board is None:
        return False
    lock = _lock(title)
    if wait:
        lock.acquire()
    elif not lock.acquire(blocking=False):
        return None
    try:
        if _ready(source):
            return True
        if board is not None:
            board.log(developer, f"Adding the entry rule for {title}")
        try:
            added = bool(_write_rule(developer, source, bars))
        except Exception as exc:
            board.post(developer, f"The entry rule for {title} was not added: {exc}", kind="error", channel=developer)
            return False
        if added:
            from futuresfund.learn import ATTEMPT_LIMIT

            board.post(
                developer,
                f"The entry rule for {title} is on the engine. The {ATTEMPT_LIMIT} attempts use that rule.",
                kind="report",
                channel=developer,
            )
            return True
        board.post(
            developer,
            f"The entry rule for {title} was not added. The study will not record empty results for it.",
            kind="report",
            channel=developer,
        )
        return False
    finally:
        lock.release()


def run_saved(title: str, bars: list, values: dict, slip: float, commission: float, point: float):
    """Run a rule the developer already saved. None when this title has no file."""
    path = _path(title)
    if not path.is_file():
        return None
    fn = _load(path)
    pairs = fn(bars, values)
    if not isinstance(pairs, list) or len(pairs) != len(bars):
        raise RuntimeError(f"The entry rule for {title} did not return one signal per bar.")
    signals = []
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) < 2:
            raise RuntimeError(f"The entry rule for {title} returned a signal that is not a long and a short.")
        signals.append((bool(pair[0]), bool(pair[1]), None))
    from futuresfund.script_backtest import _simulate, _stop_points, _tp_points

    return _simulate(bars, signals, _stop_points(values, point), _tp_points(values, point), slip, commission, point, values)


def covered(source: str) -> bool:
    """The engine can already read this script's long and short conditions."""
    return _ready(source)


def save_rule(title: str, function_source: str, bars: list, values: dict | None = None) -> None:
    """Store one checked entry rule. A rule that never enters, or that is not safe, is refused."""
    body = _extract(function_source)
    fn = _compile(body)
    _probe(fn, bars, values or {})
    RULES.mkdir(parents=True, exist_ok=True)
    _path(title).write_text(body + "\n", encoding="utf-8")


def _ready(source: str) -> bool:
    """The Pine runtime runs the script itself. A rule is only written for what it cannot run yet."""
    from futuresfund.pine_runtime import supported

    if supported(source):
        return True
    title = _title(source)
    if title in _BUILTIN or _path(title).is_file():
        return True
    return _generic_covers(source)


def _generic_covers(source: str) -> bool:
    from futuresfund.script_backtest import _bool_lines, _signal_names, _values

    long_name, short_name = _signal_names(source)
    if not long_name:
        return False
    known = set(_PRESET)
    known.update(_values(source, {}))
    known.update(_series_names(source))
    defined = {name: expr for name, expr in _bool_lines(source)}
    pending = dict(defined)
    changed = True
    while changed and pending:
        changed = False
        for name, expr in list(pending.items()):
            if "?" in expr:
                pending.pop(name)
                continue
            names = _expr_names(expr)
            if names is None:
                pending.pop(name)
                continue
            if names <= known:
                known.add(name)
                pending.pop(name)
                changed = True
    return long_name in known and short_name in known


def _series_names(source: str) -> set[str]:
    names = set()
    for match in re.finditer(r"(\w+)\s*=\s*ta\.(?:ema|sma|atr|rsi|vwma|highest|lowest|crossover|crossunder)\(", source or ""):
        names.add(match.group(1))
    for match in re.finditer(r"(\w+)\s*=\s*(\w+)\s*-\s*\2\[(\w+)\]", source or ""):
        if match.group(2) in names:
            names.add(match.group(1))
    return names


def _expr_names(expr: str) -> set[str] | None:
    text = expr
    text = re.sub(r"\bnot\s+na\((\w+)\)", r"(\1 is not None)", text)
    text = re.sub(r"\bna\((\w+)\)", r"(\1 is None)", text)
    text = re.sub(r"\bstrategy\.position_size\b", "strategy_position_size", text)
    text = re.sub(r"(\w+)\[(\d+)\]", lambda match: f"{match.group(1)}_{match.group(2)}", text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _write_rule(developer: str, source: str, bars: list) -> bool:
    from futuresfund.llm import CONTEXT_CAP, _post, model_name
    from futuresfund.script_backtest import _values

    title = _title(source)
    values = _values(source, {})
    prompt = _prompt(source, values)
    error = ""
    for _attempt in range(2):
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the chart developer adding one entry rule to the engine. "
                    "Reply with a single Python function and nothing else."
                ),
            },
            {"role": "user", "content": prompt if not error else f"{prompt}\n\nThe last function failed: {error}\nWrite the function again."},
        ]
        reply = _post(model_name(developer), messages, num_ctx=8192, timeout=180)
        if reply.startswith("The meeting model could not answer"):
            error = reply
            continue
        try:
            save_rule(title, reply, bars, values)
        except (SyntaxError, ValueError, TimeoutError, RuntimeError, TypeError) as exc:
            error = str(exc)
            continue
        return True
    return False


def _prompt(source: str, values: dict) -> str:
    title = _title(source)
    inputs = ", ".join(f"{name}={value}" for name, value in list(values.items())[:40])
    body = _entry_text(source)
    return (
        f"Add the entry rule for {title}.\n"
        "Define signals(bars, values). Return a list with one (long, short) pair of booleans per bar.\n"
        "Each bar has .o .h .l .c .v and .when. values is the dict of inputs.\n"
        "Read every threshold from values by the input name. Do not hardcode an input.\n"
        "Use the script's own long and short conditions. No imports. No files.\n"
        "Allowed names: math, min, max, abs, len, range, int, float, bool, sum, enumerate, round, any, all.\n"
        f"Inputs: {inputs}\n\n"
        f"Pine entry:\n{body}"
    )[:12000]


def _entry_text(source: str) -> str:
    lines = (source or "").splitlines()
    inputs = [line for line in lines if "input." in line]
    start = 0
    for index, line in enumerate(lines):
        if "ta." in line or "bool long_" in line or "bool do_long" in line or "bool bull_" in line:
            start = index
            break
    end = len(lines)
    for index, line in enumerate(lines):
        if "strategy.entry" in line:
            end = min(len(lines), index + 8)
    chunk = inputs + lines[start:end]
    return "\n".join(chunk)[:9000]


def _extract(text: str) -> str:
    fenced = re.findall(r"```(?:python)?\s*(.*?)```", text or "", flags=re.S)
    body = fenced[0] if fenced else (text or "")
    start = body.find("def signals")
    if start < 0:
        raise ValueError("The entry rule has no signals function.")
    if re.search(r"^\s*import\b|^\s*from\b", body[:start], flags=re.M):
        raise ValueError("The entry rule cannot import.")
    return body[start:].strip()


def _compile(body: str):
    tree = ast.parse(body)
    if not _safe(tree):
        raise ValueError("The entry rule uses a call the engine will not run.")
    namespace = {
        "math": __import__("math"),
        "min": min, "max": max, "abs": abs, "len": len, "range": range,
        "int": int, "float": float, "bool": bool, "sum": sum,
        "enumerate": enumerate, "round": round, "any": any, "all": all,
    }
    exec(compile(tree, "<entry-rule>", "exec"), namespace, namespace)
    fn = namespace.get("signals")
    if not callable(fn):
        raise ValueError("The entry rule has no signals function.")
    return fn


def _safe(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            return False
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in _CALLS:
                return False
    return True


def _probe(fn, bars: list, values: dict) -> None:
    box: dict = {}

    def run() -> None:
        try:
            box["pairs"] = fn(bars, values)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(60)
    if thread.is_alive():
        raise TimeoutError("The entry rule did not finish.")
    if box.get("error"):
        raise RuntimeError(str(box["error"]))
    pairs = box.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != len(bars):
        raise ValueError("The entry rule did not return one signal per bar.")
    if not any(isinstance(pair, (tuple, list)) and len(pair) >= 2 and (bool(pair[0]) or bool(pair[1])) for pair in pairs):
        raise ValueError("The entry rule did not take a trade on these bars.")


def _load(path: Path):
    return _compile(path.read_text(encoding="utf-8"))


def _path(title: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "script").lower()).strip("_") or "script"
    return RULES / f"{slug}.py"


def _title(source: str) -> str:
    match = re.search(r'strategy\s*\(\s*"([^"]+)"', source or "")
    return match.group(1).strip() if match else ""


def _lock(title: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(title)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[title] = lock
        return lock


def repair_dead_entry(source: str) -> str:
    """Make a generated entry able to fire.

    Two patterns the writer keeps producing never enter on any input. The current
    volume cannot be above ta.highest(volume), because that highest includes the
    current bar. A cooldown that stores this bar before the entry check, or that
    compares bar_index with an na stamp, is false on the signal bar too.
    """
    text = source or ""
    text = re.sub(r"ta\.highest\(\s*volume\s*,", "ta.highest(volume[1],", text)
    names = re.findall(r"^\s*var\s+int\s+(\w+)\s*=\s*na\b", text, flags=re.M)
    cooldown = _cooldown_name(text)
    for name in dict.fromkeys(names):
        text = _drop_stamp_only_blocks(text, name)
        text = _allow_first_entry(text, name, cooldown)
        text = _stamp_after_entry(text, name)
    if source.endswith("\n") and text and not text.endswith("\n"):
        text += "\n"
    return text


def _cooldown_name(source: str) -> str:
    match = re.search(r"\b(\w*cooldown\w*)\s*=\s*input\b", source, flags=re.I)
    return match.group(1) if match else "cooldown_bars"


def _drop_stamp_only_blocks(source: str, name: str) -> str:
    """Remove an if that only stores the current bar and never calls strategy.*."""
    lines = source.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if re.match(r"\s*if\b", line):
            indent = len(line) - len(line.lstrip(" "))
            block = [line]
            nxt = index + 1
            while nxt < len(lines):
                row = lines[nxt]
                if row.strip() and (len(row) - len(row.lstrip(" "))) <= indent:
                    break
                block.append(row)
                nxt += 1
            body = "\n".join(block)
            if f"{name} := bar_index" in body and "strategy." not in body:
                index = nxt
                continue
        kept.append(line)
        index += 1
    return "\n".join(kept)


def _allow_first_entry(source: str, name: str, cooldown: str) -> str:
    """A stamp that is still na has to allow the first entry, then honor the cooldown."""
    rows = []
    for line in source.splitlines():
        if f"na({name})" in line:
            rows.append(line)
            continue
        updated = re.sub(
            rf"bar_index\s*-\s*{name}\s*>\s*(\w+)",
            rf"(na({name}) or bar_index - {name} > \1)",
            line,
        )
        updated = re.sub(
            rf"bar_index\s*>\s*{name}\s*\+\s*(\w+)",
            rf"(na({name}) or bar_index > {name} + \1)",
            updated,
        )
        updated = re.sub(
            rf"bar_index\s*>\s*{name}\b(?!\s*\+)",
            rf"(na({name}) or bar_index > {name} + {cooldown})",
            updated,
        )
        rows.append(updated)
    return "\n".join(rows)


def _stamp_after_entry(source: str, name: str) -> str:
    """Store the entry bar after the order, so the same bar can still enter."""
    lines = source.splitlines()
    out: list[str] = []
    for index, line in enumerate(lines):
        out.append(line)
        if "strategy.entry(" not in line:
            continue
        follow = "\n".join(lines[index + 1:index + 4])
        if f"{name} := bar_index" in follow:
            continue
        indent = len(line) - len(line.lstrip(" "))
        out.append(" " * indent + f"{name} := bar_index")
    return "\n".join(out)
