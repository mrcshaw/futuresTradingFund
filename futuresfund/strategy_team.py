"""The strategy creation team.

The team is trained on strategies this desk has already backtested. It is not trained on
the scripts it wrote. Each chart has its own strategy developer, and those developers are
the only ones who save a new Pine script in createdStrategies. Engine 4 develops a queued
script on the full five-year download. It does not file it. The three chart engines file
a one-year result on the Not tested on the full dataset list when it clears the same
acceptance as that list.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from futuresfund.learn import LEARNED, LEARNING, NOTES

CREATED = LEARNING.parent / "createdStrategies"

CARD_KEEPER = "Card keeper"
STRATEGY_DEVELOPER = "Strategy developer"
SCRIPT_CHECKER = "Script checker"
CREATION_TESTER = "Creation tester"
LESSON_WRITER = "Lesson writer"
CREATION_ENGINE = 3
CREATION_CHART = "5m"
QUEUE_PATH = NOTES / "creation_queue.json"
CANDLE_NOTES = NOTES / "candle_probabilities.json"
_LIBRARY_SEEN: set[str] = set()
CONFIRM_CHARTS = ("2m", "15m")
MAX_SENT_BACK = 3
_create_lock = threading.Lock()
CARDS_PATH = NOTES / "strategy_cards.json"
LESSONS_PATH = NOTES / "creation_lessons.json"
SEEN_PATH = NOTES / "creation_sources.json"
_CREATED = "created_"
_lessons_lock = threading.Lock()


# Cards: every measured change from the studies.


def build_cards(learning: Path | None = None, notes: Path | None = None, learned: Path | None = None) -> list[dict]:
    """One card per studied Pine file, from the learning folder and the learned folder.

    A card carries the measured one-variable changes, steadier first, and the best result.
    Cards are ordered by the research objective in objective.py.
    """
    folders = [learning or LEARNING]
    if learned is not None:
        folders.append(learned)
    elif learning is None:
        folders.append(LEARNED)
    store = notes or NOTES
    from futuresfund.exemplars import studies

    quality = {row["id"]: row for row in studies(store)}
    cards = []
    seen = set()
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.pine")):
            if not path.is_file() or path.name.startswith(_CREATED) or path.name in seen:
                continue
            seen.add(path.name)
            record = _note_for(store, path)
            study = quality.get(path.stem.replace(" ", "_")) or {}
            cards.append({
                "file": path.name,
                "title": record.get("title") or path.stem,
                "folder": folder.name,
                "best": _best_line(record),
                "quality": {
                    "safe_green": study.get("safe_green", 0),
                    "tested": study.get("tested", 0),
                    "efficiency": study.get("efficiency", 0),
                },
                "changes": _changes(record),
            })
    cards.sort(key=_card_rank, reverse=True)
    return cards


def measured_cards(cards: list[dict]) -> list[dict]:
    """Cards that already have a tested change."""
    return [card for card in cards if card.get("changes")]


def saved_cards(path: Path | None = None) -> list[dict]:
    """Cards already on disk. The write loop does not rescan every research note to rebuild them."""
    target = path or CARDS_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("cards") if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def write_cards(cards: list[dict], path: Path | None = None) -> Path:
    target = path or CARDS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"cards": cards}, indent=2), encoding="utf-8")
    return target


def _card_rank(card: dict) -> tuple:
    """Cards from the studies that rank highest under the research objective come first, then the steadier study."""
    from futuresfund.objective import rank

    quality = card.get("quality") or {}
    best = card.get("best") or {}
    return rank(best) + (int(quality.get("safe_green") or 0), float(quality.get("efficiency") or 0))


def _best_line(record: dict) -> dict:
    best = record.get("best") if isinstance(record.get("best"), dict) else {}
    return {
        "net_profit": best.get("net_profit"),
        "max_drawdown": best.get("max_drawdown"),
        "trades": best.get("trades"),
        "timeframe": best.get("timeframe"),
        "contract": best.get("contract"),
    }


# The script checker.


def extract_pine(text: str) -> str:
    """The Pine script inside a model reply. The version line stays first."""
    body = text or ""
    fenced = re.findall(r"```(?:pine|pinescript)?\s*([\s\S]*?)```", body, flags=re.IGNORECASE)
    for block in fenced:
        if "//@version" in block or "strategy(" in block:
            body = block
            break
    start = body.find("//@version")
    if start < 0:
        start = body.find("strategy(")
    if start < 0:
        return ""
    return body[start:].strip()


def repair_pine(source: str) -> str:
    """Correct the writing mistakes this desk repeats before a script is saved.

    maxmax is the parameter maxval. ta.valuewhen takes a condition, then the
    value, then which occurrence. An assignment inside that call is not a condition.
    A script that can enter also flats at 16:45 New York.
    """
    text = source or ""
    text = re.sub(r"\bmaxmax\b", "maxval", text)
    text = re.sub(r"\bminmin\b", "minval", text)
    text = re.sub(
        r"ta\.valuewhen\(\s*([A-Za-z_]\w*)\s*=\s*\1\s*,",
        r"ta.valuewhen(not na(\1),",
        text,
    )
    return ensure_risk_management(ensure_session(text))


# The session every script is saved with. Hour and minute inputs, 18:00 to 16:00 New York.
# No weekday suffix. 12345 on a time() session was Sunday through Thursday, which shut Friday.
SESSION_INPUTS = "\n".join([
    "// ===== SESSION =====",
    'use_session   = input.bool(true, "Use Session Filter")',
    'sess_start_hr = input.int(18, "Session Start Hour (ET)")',
    'sess_start_mn = input.int(0,  "Session Start Minute")',
    'sess_end_hr   = input.int(16,  "Session End Hour (ET)")',
    'sess_end_mn   = input.int(0,  "Session End Minute")',
])
SESSION_LOGIC = "\n".join([
    "sess_bar_hr = hour(time, \"America/New_York\")",
    "sess_bar_mn = minute(time, \"America/New_York\")",
    "sess_bar_t = sess_bar_hr * 100 + sess_bar_mn",
    "sess_clock_start = sess_start_hr * 100 + sess_start_mn",
    "sess_clock_end = sess_end_hr * 100 + sess_end_mn",
    "in_session = not use_session or (sess_clock_start > sess_clock_end ? (sess_bar_t >= sess_clock_start or sess_bar_t < sess_clock_end) : (sess_bar_t >= sess_clock_start and sess_bar_t < sess_clock_end))",
    "if not in_session",
    '    strategy.close_all(comment="Session")',
])
_FUTURES_GATE = re.compile(
    r"^[ \t]*(?:bool[ \t]+)?(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*not[ \t]+na[ \t]*\([ \t]*time[ \t]*\((?P<body>[^)\n]*)\)[ \t]*\)[ \t]*(?://[^\n]*)?\r?\n?",
    re.M,
)
_IN_SESSION_DEF = re.compile(r"(?m)^[ \t]*(?:bool[ \t]+)?in_session[ \t]*=")


def _futures_window(body: str) -> bool:
    """The overnight flatten written as a time() session, including the weekday suffix."""
    compact = re.sub(r"\s+", "", body or "")
    return "1800-1645" in compact or "1800-1600" in compact


def _strip_futures_gates(source: str) -> tuple[str, list[str]]:
    """Remove time() flatten lines. The names come back so their uses can move onto in_session."""
    names = []

    def repl(match):
        if not _futures_window(match.group("body")):
            return match.group(0)
        names.append(match.group("name"))
        return ""

    text = _FUTURES_GATE.sub(repl, source or "")
    text = re.sub(
        r"\s*(?:and|or)\s+not\s+na\s*\(\s*time\s*\([^)\n]*(?:1800-1645|1800-1600)[^)\n]*\)\s*\)",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"not\s+na\s*\(\s*time\s*\([^)\n]*(?:1800-1645|1800-1600)[^)\n]*\)\s*\)\s*(?:and|or)\s+",
        "",
        text,
        flags=re.I,
    )
    for name in names:
        text = _drop_session_block(text, name)
    return text, names


def _drop_session_block(source: str, name: str) -> str:
    """Remove the old flatten if, including every line indented under it."""
    lines = source.splitlines(keepends=True)
    kept = []
    index = 0
    header = re.compile(rf"^[ \t]*if[ \t]+not[ \t]+{re.escape(name)}[ \t]*$")
    while index < len(lines):
        row = lines[index]
        if header.match(row.rstrip("\r\n")):
            indent = len(row) - len(row.lstrip(" "))
            index += 1
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    index += 1
                    continue
                if len(nxt) - len(nxt.lstrip(" ")) > indent:
                    index += 1
                    continue
                break
            continue
        kept.append(row)
        index += 1
    return "".join(kept)


def _rename_session_flags(source: str, names: list[str]) -> str:
    text = source
    for name in names:
        if name == "in_session":
            continue
        text = re.sub(rf"\b{re.escape(name)}\b", "in_session", text)
    return text


def _insert_session_block(source: str) -> str:
    """Add the standard inputs and the in_session test when the script does not already have them."""
    text = source or ""
    has_inputs = "sess_start_hr" in text
    has_flag = _IN_SESSION_DEF.search(text) is not None
    if has_inputs and has_flag:
        return text
    if has_flag and not has_inputs:
        return text
    parts = []
    if not has_inputs:
        parts.append(SESSION_INPUTS)
    if not has_flag:
        parts.append(SESSION_LOGIC)
    elif "session_ended" not in text and not re.search(r"(?m)^[ \t]*if[ \t]+not[ \t]+in_session\b", text):
        parts.append('if not in_session\n    strategy.close_all(comment="Session")')
    if not parts:
        return text
    return _insert_after_risk_or_strategy(text, "\n".join(parts))


def _insert_after_risk_or_strategy(source: str, block: str) -> str:
    marker = "trail_min_dollars"
    index = source.find(marker)
    if index >= 0:
        newline = source.find("\n", index)
        if newline >= 0:
            return source[: newline + 1] + "\n" + block + "\n" + source[newline + 1 :]
    return _insert_after_strategy(source, block)


def ensure_session(source: str) -> str:
    """Use the standard session inputs. A time() session with a weekday suffix is removed."""
    text = source or ""
    if "strategy.entry" not in text and "strategy.order" not in text:
        return text.replace("1800-1645:12345", "1800-1645").replace("1800-1600:12345", "1800-1600")
    stripped, names = _strip_futures_gates(text)
    renamed = _rename_session_flags(stripped, names)
    inserted = _insert_session_block(renamed)
    if names or inserted != renamed:
        inserted = _guard_entries(inserted)
    if not inserted.endswith("\n"):
        inserted += "\n"
    return inserted.replace("1800-1645:12345", "1800-1645").replace("1800-1600:12345", "1800-1600")


def _insert_after_strategy(source: str, line: str) -> str:
    start = source.find("strategy(")
    if start < 0:
        return line + "\n" + source
    depth = 0
    seen = False
    for index in range(start, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
            seen = True
        elif char == ")":
            depth -= 1
            if seen and depth == 0:
                newline = source.find("\n", index)
                if newline < 0:
                    return source + "\n" + line + "\n"
                return source[: newline + 1] + line + "\n" + source[newline + 1 :]
    return source + "\n" + line + "\n"


def _inside_session(rows: list[str], row: str) -> bool:
    """True when an ancestor if already requires in_session. The close block is not that ancestor."""
    indent = len(row) - len(row.lstrip(" "))
    for prev in reversed(rows):
        if not prev.strip():
            continue
        prev_indent = len(prev) - len(prev.lstrip(" "))
        if prev_indent >= indent:
            continue
        text = prev.strip()
        if text.startswith("if ") and re.search(r"\bin_session\b", text) and not re.search(r"\bnot\s+in_session\b", text):
            return True
        indent = prev_indent
    return False


def _guard_entries(source: str) -> str:
    """An entry does not open outside the session inputs."""
    rows = source.splitlines()
    out = []
    for row in rows:
        stripped = row.lstrip(" ")
        if stripped.startswith("strategy.entry(") and not _inside_session(out, row):
            indent = row[: len(row) - len(stripped)]
            out.append(f"{indent}if in_session")
            out.append(f"{indent}    {stripped}")
        else:
            out.append(row)
    return "\n".join(out) + ("\n" if source.endswith("\n") else "")


def _point_literal(point: float) -> str:
    number = float(point)
    if number.is_integer():
        return str(int(number))
    return str(number)


def risk_block(point: float = 50) -> str:
    """The shared risk block. Stops are whole dollars. The default point value is ES, $50."""
    shown = _point_literal(point)
    return "\n".join([
        "// ===== RISK =====",
        f'pv                = input.float({shown}, "Point Value ($)")',
        'stop_dollars      = input.int(250, "Stop ($)", minval=250, maxval=1500)',
        'tp_dollars        = input.int(500, "Take Profit ($)", minval=250, maxval=2000)',
        'use_trail         = input.bool(false, "Enable Trailing Stop")',
        'trail_step_amt    = input.int(500, "Trail: Tighten every ($)", minval=0, maxval=2000)',
        'trail_min_dollars = input.int(1000, "Trail: Min Profit ($)", minval=0, maxval=2000)',
        "long_stop = close - stop_dollars / pv",
        "long_limit = close + tp_dollars / pv",
        "short_stop = close + stop_dollars / pv",
        "short_limit = close - tp_dollars / pv",
        "if strategy.position_size != 0 and not na(stop_dollars)",
        "    if strategy.openprofit <= -stop_dollars",
        '        strategy.close_all(comment="Open P&L > SL Risk")',
    ])


# The block every new script is saved with, and every backtest writes into the file first.
RISK_BLOCK = risk_block()
_RISK_ASSIGN = re.compile(
    r"^(?:[\w.]+\s+)*(?:pv|stop_pct|tp_pct|stop_cash|tp_cash|stop_dollars|tp_dollars|use_trail|trail_pct|"
    r"trail_step_amt|trail_step_pct|trail_step_acct|trail_min_dollars|trail_min_pct)\s*=(?!=)"
)
_CAPITAL = re.compile(r"\binitial_capital\s*=\s*\d+(?:\.\d+)?")
_RISK_LINES = {
    "if stop_cash > 0",
    "stop_dollars := stop_cash",
    "if tp_cash > 0",
    "tp_dollars := tp_cash",
    "if strategy.position_size != 0 and strategy.openprofit <= -stop_dollars",
    "if strategy.position_size != 0 and not na(stop_dollars)",
    "if strategy.openprofit <= -stop_dollars",
    'strategy.close_all(comment="Dollar stop")',
    'strategy.close_all(comment="Open P&L > SL Risk")',
}
_PERCENT_ASSIGN = re.compile(
    r"^(?:[\w.]+\s+)*(?:stop_pct|tp_pct|stop_cash|tp_cash|trail_pct|trail_step_pct|trail_step_acct|trail_min_pct)\s*=(?!=)"
)
_PERCENT_FORMULA = re.compile(
    r"^(?:stop_dollars|tp_dollars|trail_step_amt|trail_min_dollars)\s*=\s*strategy\.initial_capital\b"
)
_DOLLAR_STOP_INPUT = re.compile(r"\bstop_dollars\s*=\s*input\.(?:int|float)\s*\(")
_OPEN_LOSS = re.compile(r"strategy\.openprofit\s*(?:<=|<)\s*-\s*stop_dollars")
_EXIT_PRICE = re.compile(r"^(?:long_stop|long_limit|short_stop|short_limit)\s*=")
_PV_INPUT = re.compile(r"(pv\s*=\s*input\.float\(\s*)[\d.]+")
_ATR_EXIT = re.compile(
    r"stop_mult|tp_mult|sl_mult|atr_mult_sl|atr_mult_tp|"
    r"Stop Mult|TP Mult|SL Multiplier|TP Multiplier|"
    r"\batr\w*\s*\*\s*\w*(?:mult|stop|tp)|\w*(?:mult|stop)\w*\s*\*\s*\batr",
    re.IGNORECASE,
)


def atr_exit(source: str) -> str:
    """The reason a script is refused when ATR sets the exit distance."""
    if _ATR_EXIT.search(source or ""):
        return (
            "This script exits with ATR times a multiplier. atr_len, stop_mult, and tp_mult are not accepted. "
            "atr * stop_mult is the loss, and changing the multiplier does not keep the loss inside the dollar stop. "
            "The exit is the dollar amount. While the trade is open, if that loss reaches the stop, the trade closes."
        )
    return ""


_EXIT_PRICE_LINES = (
    ("long_stop", "long_stop = close - stop_dollars / pv"),
    ("long_limit", "long_limit = close + tp_dollars / pv"),
    ("short_stop", "short_stop = close + stop_dollars / pv"),
    ("short_limit", "short_limit = close - tp_dollars / pv"),
)
_OPEN_LOSS_BLOCK = "\n".join([
    "if strategy.position_size != 0 and not na(stop_dollars)",
    "    if strategy.openprofit <= -stop_dollars",
    '        strategy.close_all(comment="Open P&L > SL Risk")',
])


def _missing_exit_prices(source: str) -> str:
    """The four exit prices, when this script does not already name them."""
    text = source or ""
    return "\n".join(formula for name, formula in _EXIT_PRICE_LINES if not re.search(rf"(?m)^\s*{name}\s*=", text))


def _insert_after_risk_inputs(source: str, block: str) -> str:
    """Place an exit line after pv and the dollar inputs, so Pine has already named them."""
    lines = (source or "").splitlines()
    pattern = re.compile(r"^\s*(?:pv|stop_dollars|tp_dollars|long_stop|long_limit|short_stop|short_limit)\s*=")
    last = -1
    for index, line in enumerate(lines):
        if pattern.match(line):
            last = index
    if last < 0:
        return _insert_after_strategy(source, block)
    lines[last + 1:last + 1] = block.splitlines()
    body = "\n".join(lines)
    if (source or "").endswith("\n"):
        body += "\n"
    return body


def _ensure_exit_prices(source: str) -> str:
    """Every script names the four dollar prices. An existing stop_dollars input stays."""
    lines = (source or "").splitlines()
    missing = []
    for name, formula in _EXIT_PRICE_LINES:
        pattern = re.compile(rf"^(\s*){name}\s*=")
        found = False
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if match is None:
                continue
            found = True
            replacement = f"{match.group(1)}{formula}"
            if lines[index] != replacement:
                lines[index] = replacement
            break
        if not found:
            missing.append(formula)
    body = "\n".join(lines)
    if (source or "").endswith("\n"):
        body += "\n"
    if missing:
        body = _insert_after_risk_inputs(body, "\n".join(missing))
    return body


def _ensure_open_loss(source: str) -> str:
    """The open loss closes the trade once. A second copy is not added."""
    if _OPEN_LOSS.search(source or ""):
        return source
    return _insert_after_risk_inputs(source, _OPEN_LOSS_BLOCK)


def _exit_side(call: str) -> str:
    named = re.search(r'from_entry\s*=\s*"([^"]*)"', call)
    quotes = re.findall(r'"([^"]*)"', call)
    label = named.group(1) if named else (quotes[1] if len(quotes) > 1 else "")
    if re.search(r"short", label, re.IGNORECASE):
        return "short"
    if re.search(r"long", label, re.IGNORECASE):
        return "long"
    if re.search(r"strategy\.short|\bshort\b", call, re.IGNORECASE):
        return "short"
    return "long"


def _split_exit_args(args: str) -> list[str]:
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
        if char == "(":
            depth += 1
        elif char == ")" and depth:
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


def _rewrite_one_exit(call: str) -> str:
    open_paren = call.find("(")
    side = _exit_side(call)
    stop_name = "short_stop" if side == "short" else "long_stop"
    limit_name = "short_limit" if side == "short" else "long_limit"
    rebuilt = []
    saw_stop = False
    saw_limit = False
    for part in _split_exit_args(call[open_paren + 1:-1]):
        kind = re.match(r"^(stop|limit|loss|profit)\s*=", part)
        if kind is None:
            rebuilt.append(part)
            continue
        if kind.group(1) in ("stop", "loss") and not saw_stop:
            rebuilt.append(f"stop={stop_name}")
            saw_stop = True
        elif kind.group(1) in ("limit", "profit") and not saw_limit:
            rebuilt.append(f"limit={limit_name}")
            saw_limit = True
    if not saw_stop:
        rebuilt.append(f"stop={stop_name}")
    if not saw_limit:
        rebuilt.append(f"limit={limit_name}")
    return call[: open_paren + 1] + ", ".join(rebuilt) + ")"


def _wire_exit_calls(source: str) -> str:
    """Point every strategy.exit stop and limit at the dollar prices."""
    text = source or ""
    out = []
    cursor = 0
    token = "strategy.exit"
    while True:
        start = text.find(token, cursor)
        if start < 0:
            out.append(text[cursor:])
            break
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].strip().startswith("//"):
            out.append(text[cursor:start + len(token)])
            cursor = start + len(token)
            continue
        paren = text.find("(", start)
        if paren < 0:
            out.append(text[cursor:])
            break
        depth = 0
        end = None
        for index in range(paren, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end is None:
            out.append(text[cursor:])
            break
        out.append(text[cursor:start])
        out.append(_rewrite_one_exit(text[start:end + 1]))
        cursor = end + 1
    return "".join(out)


def _apply_shared_exit(source: str) -> str:
    """The dollar prices, the open-loss close, and strategy.exit all use the same exit."""
    return _wire_exit_calls(_ensure_open_loss(_ensure_exit_prices(source)))


def _similar_risk(source: str) -> bool:
    """The script already stops out on whole dollars when the open loss reaches stop_dollars."""
    text = source or ""
    return _DOLLAR_STOP_INPUT.search(text) is not None and _OPEN_LOSS.search(text) is not None


def _open_loss_line(stripped: str) -> bool:
    if stripped in _RISK_LINES:
        return True
    if "strategy.openprofit" in stripped and "stop_dollars" in stripped:
        return True
    return 'comment="Open P&L > SL Risk"' in stripped or "comment='Open P&L > SL Risk'" in stripped


def _set_point(source: str, point: float) -> str:
    """Write this contract's point value into the pv input that is already there."""
    shown = _point_literal(point)
    if _PV_INPUT.search(source):
        return _PV_INPUT.sub(rf"\g<1>{shown}", source, count=1)
    return _insert_after_strategy(source, f'pv = input.float({shown}, "Point Value ($)")')


def _without_percent_risk(source: str) -> str:
    """Drop account-percent stops. A whole-dollar stop that is already in the script stays."""
    kept = []
    for line in source.splitlines():
        stripped = line.strip()
        if _PERCENT_ASSIGN.match(stripped) or _PERCENT_FORMULA.match(stripped):
            continue
        if stripped in {"if stop_cash > 0", "stop_dollars := stop_cash", "if tp_cash > 0", "tp_dollars := tp_cash"}:
            continue
        kept.append(line)
    body = "\n".join(kept)
    if source.endswith("\n"):
        body += "\n"
    return body


def ensure_risk_management(source: str, point: float | None = None) -> str:
    """Put the shared dollar exit on every script.

    point is the contract dollars per point. A script that already has a dollar
    stop keeps that stop and take profit. Percent inputs are removed either way.
    Every strategy.exit stop and limit uses the dollar prices.
    """
    text = source or ""
    if "strategy(" not in text:
        return text
    if _similar_risk(text):
        body = _without_percent_risk(text)
        body = _CAPITAL.sub("initial_capital=50000", body, count=1)
        if point is not None:
            body = _set_point(body, point)
        return _apply_shared_exit(body)
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {"// ===== RISK Management=====", "// ===== RISK ====="}:
            continue
        if "Point Value for" in line or "Defaulting to NQ" in line:
            continue
        if _RISK_ASSIGN.match(stripped) or _open_loss_line(stripped) or _EXIT_PRICE.match(stripped):
            continue
        kept.append(line)
    body = "\n".join(kept)
    if text.endswith("\n"):
        body += "\n"
    body = _CAPITAL.sub("initial_capital=50000", body, count=1)
    block = risk_block() if point is None else risk_block(point)
    return _apply_shared_exit(_insert_after_strategy(body, block))


def prepare_backtest(source: str, root: str) -> str:
    """The script the engine runs: standard session, and this contract's point value."""
    from futuresfund.risk_style import point_value

    try:
        point = point_value(root)
    except ValueError:
        point = 50.0
    return ensure_risk_management(ensure_session(source), point)


def _closes_before_1645(source: str) -> bool:
    """The script flats every position when the session ends."""
    text = source or ""
    if "strategy.close_all" not in text:
        return False
    if "sess_end_hr" in text or "session_ended" in text:
        return True
    return "1800-1645" in text or "1800-1600" in text


def has_stop(source: str) -> bool:
    """A protective stop the engine runs: strategy.exit with a stop or loss price."""
    for match in re.finditer(r"strategy\.exit\s*\(([^\n]*)", source or ""):
        arguments = match.group(1)
        if re.search(r"\b(stop|loss)\s*=", arguments):
            return True
    return False


def pine_problems(source: str) -> list[str]:
    """What is missing before a new script can reach the engine. The engine has to be able to run every line."""
    text = source or ""
    problems = []
    if "//@version" not in text:
        problems.append("The version line is missing.")
    if "strategy(" not in text:
        problems.append("strategy() is missing.")
    if "strategy.entry" not in text and "strategy.order" not in text:
        problems.append("An entry is missing.")
    if "strategy.close" not in text and "strategy.exit" not in text:
        problems.append("An exit is missing.")
    if "strategy.entry" in text and not _closes_before_1645(text):
        problems.append(
            "The trade is not closed at the session end. Close the open trade with strategy.close_all when in_session is false."
        )
    if "strategy.entry" in text and not has_stop(text):
        problems.append("A protective stop is missing. Every entry needs strategy.exit with stop= or loss= so the drawdown is capped.")
    refused = atr_exit(text)
    if refused:
        problems.append(refused)
    if re.search(r"\bmaxmax\b", text):
        problems.append("An input uses maxmax. The parameter name is maxval.")
    if re.search(r"\bminmin\b", text):
        problems.append("An input uses minmin. The parameter name is minval.")
    from futuresfund.research_rules import risk_problems

    problems.extend(risk_problems(text))
    if re.search(r"ta\.valuewhen\(\s*[A-Za-z_]\w*\s*=", text):
        problems.append(
            "ta.valuewhen was given an assignment. The first argument is a condition, such as not na(pivot_high), then the price, then the occurrence."
        )
    if not problems:
        from futuresfund.pine_runtime import PineUnsupported, compile_pine

        try:
            compile_pine(text)
        except PineUnsupported as exc:
            problems.append(f"The engine cannot run it: {exc}")
    return problems


DRY_RUN_BARS = 600


def runtime_problems(source: str, bars: list[dict], timeframe: str = CREATION_CHART) -> list[str]:
    """Run the script once on the last few hundred real bars, on engine 4, so a line that fails while running is caught before the attempts.

    Compiling catches an unsupported function. It does not catch a session input compared with a number, or a stop
    built from a value that is not a price. Those stop the engine on the first bar with no result at all.
    """
    if not source or len(bars or []) < 50:
        return []
    from futuresfund.backtrader_engine import run_script

    try:
        measured = run_script(source, list(bars[-DRY_RUN_BARS:]), timeframe, None, threading.Event(), "ES", "script check", CREATION_ENGINE) or {}
    except Exception as exc:  # the checker reports, it does not crash the round
        return [f"The engine stopped it on a dry run: {exc}"]
    if measured.get("fatal") or measured.get("error"):
        return [f"The engine stopped it on a dry run of {len(bars[-DRY_RUN_BARS:])} bars: {measured.get('error') or 'no result'}"]
    return []


def baseline_ready(rows: list[dict]) -> bool:
    """A first pass is usable when one chart produces a trade list and none fail to compile."""
    if not rows:
        return False
    if any(row.get("fatal") or row.get("error") for row in rows):
        return False
    return any(int(row.get("trades") or 0) >= 1 for row in rows)


def script_name(title: str, folder: Path | None = None) -> str:
    """A new file name that does not replace a script already in the folder."""
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "strategy").lower()).strip("_")[:40] or "strategy"
    folder = folder or CREATED
    name = f"{_CREATED}{slug}.pine"
    stem = f"{_CREATED}{slug}"
    number = 2
    while (folder / name).exists():
        name = f"{stem}_{number}.pine"
        number += 1
    return name


# The prompt the strategy developer writes from.


def team_wrote(name: str) -> bool:
    """True when this file is one the creation team saved in createdStrategies."""
    filename = Path(str(name or "")).name
    if not filename:
        return False
    if filename.startswith(_CREATED):
        return True
    stem = Path(filename).stem
    return (CREATED / f"{_CREATED}{stem}.pine").is_file() or (CREATED / filename).is_file()


def new_library_lines() -> tuple[str, list[str]]:
    """Backtested strategies this team has not yet used. Their own created scripts are left out."""
    from futuresfund.library import _load_untested, _rows

    fresh = []
    seen = set()
    for row in list(_rows()) + list(_load_untested()):
        ident = str(row.get("id") or "")
        if not ident or ident in _LIBRARY_SEEN or ident in seen:
            continue
        if team_wrote(str(row.get("file") or "")) or team_wrote(str(row.get("script") or "")):
            continue
        seen.add(ident)
        fresh.append(row)
    if not fresh:
        return "", []
    lines = ["New strategies in the library. Learn from these results, then write a different strategy from scratch:"]
    for row in fresh[:6]:
        lines.append(
            f"- {row.get('title')}: profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, "
            f"trades {row.get('trades')} on {row.get('timeframe')}"
        )
    return "\n".join(lines), [str(row.get("id")) for row in fresh if row.get("id")]


def _root_name(label: str) -> str:
    """The root the writer is allowed to use. The shelf still calls GC gold."""
    return {"Gold": "GC", "GOLD": "GC"}.get(label, label)


def backtested_catalog(limit: int = 5) -> str:
    """Successful backtests for the writers. Scripts this team created are left out."""
    from futuresfund.library import _rows

    lines = [
        "Strategy library. Each script keeps its most profitable run. A script this creation team wrote is not listed."
    ]
    grouped: dict[str, list[dict]] = {}
    for row in _rows():
        if team_wrote(str(row.get("file") or "")) or team_wrote(str(row.get("script") or "")):
            continue
        grouped.setdefault(str(row.get("contract_label") or ""), []).append(row)
    for label in ("ES", "Gold", "Nasdaq"):
        shelf = grouped.get(label) or []
        shown = _root_name(label)
        if not shelf:
            lines.append(f"{shown}: none yet.")
            continue
        lines.append(f"{shown}:")
        for row in shelf[:limit]:
            lines.append(
                f"- {row.get('title')}: {row.get('timeframe')}, profit {row.get('net_profit')}, "
                f"drawdown {row.get('max_drawdown')}, trades {row.get('trades')}"
            )
    for label, shelf in grouped.items():
        if label in {"ES", "Gold", "Nasdaq"}:
            continue
        lines.append(f"{_root_name(label)}:")
        for row in shelf[:limit]:
            lines.append(
                f"- {row.get('title')}: {row.get('timeframe')}, profit {row.get('net_profit')}, "
                f"drawdown {row.get('max_drawdown')}, trades {row.get('trades')}"
            )
    return "\n".join(lines)


def mark_library(ids: list[str]) -> None:
    """Those library rows have been shown to the writer. Later rows are the next lesson."""
    _LIBRARY_SEEN.update(str(item) for item in ids if item)


_candle_lock = threading.Lock()


def candle_session(hour: int, minute: int = 0) -> str:
    """The futures slice a candle sits in. The 16:45 halt is not a session to trade."""
    mark = int(hour) * 60 + int(minute)
    if 16 * 60 + 45 <= mark < 18 * 60:
        return "halt"
    if mark >= 18 * 60 or mark < 9 * 60 + 30:
        return "overnight"
    if mark < 12 * 60:
        return "morning"
    return "afternoon"


def record_candle(entry: dict, path: Path | None = None) -> None:
    """One replayed bar for the creation team: the 0.9 set, and whether the real candle was inside it."""
    target = path or CANDLE_NOTES
    row = _candle_row(entry)
    with _candle_lock:
        rows = _read_candles(target)
        rows.append(row)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"kind": "candle_probabilities", "rows": rows[-4000:]}, indent=1, default=str),
            encoding="utf-8",
        )


def candle_lines(path: Path | None = None, *, audience: str = "creation") -> str:
    """Measured replay notes. The creation team writes from them. The chart teams adjust from them and stay on one year."""
    chart = audience == "chart"
    who = "the chart teams" if chart else "the creation team"
    rows = _read_candles(path or CANDLE_NOTES)
    if not rows:
        if chart:
            return (
                f"Candle notes for {who}. No replay has been recorded yet. "
                "Do not invent how often the 0.9 set contained the real candle. "
                "Keep this chart on the last year of futures sessions, 260 sessions."
            )
        return (
            f"Candle notes for {who}. No replay has been recorded yet. "
            "Do not invent how often the 0.9 set contained the real candle."
        )
    groups: dict[tuple, list] = {}
    for row in rows:
        groups.setdefault(_candle_key(row), []).append(row)
    usable = []
    avoid = []
    for key, items in groups.items():
        hits = sum(1 for item in items if item.get("inside_90"))
        line = _candle_line(key, hits, len(items))
        if len(items) >= 20 and hits / len(items) >= 0.6 and key[4] == "strong" and key[5] == "peaked" and key[3] != "split":
            usable.append((hits / len(items), len(items), line))
        elif hits / len(items) < 0.5 or key[4] == "split" or key[5] == "flat" or key[3] == "split":
            avoid.append((len(items), line))
    usable.sort(key=lambda item: (item[0], item[1]), reverse=True)
    avoid.sort(key=lambda item: item[0], reverse=True)
    lines = [
        f"Candle notes for {who}. A replay keeps the candles that hold 0.9 of the probability for the next bar. "
        "Correct means the real next candle was one of those: its direction matched the agreed paths, and its high and low stayed inside that set. "
        "The count is the number of replayed bars in that circumstance, not a backtest profit.",
        f"Across {len(rows)} replayed bars, the real candle was inside the 0.9 set on {sum(1 for row in rows if row.get('inside_90'))} bars.",
    ]
    if usable:
        lines.append("Use one of these circumstances as an entry filter. Require the chart, the session, the path agreement, and the peaked next candle together:")
        lines.extend(f"- {line}" for _rate, _count, line in usable[:6])
    else:
        lines.append("No circumstance has 20 replayed bars with the real candle inside the 0.9 set on most of them. Do not invent one.")
    if avoid:
        lines.append("Do not enter on these. The 0.9 set missed the real candle, or the paths were split, or the next candle was flat:")
        lines.extend(f"- {line}" for _count, line in avoid[:4])
    if chart:
        lines.append(
            "Adjust the script you are studying from one circumstance above. "
            "Change the entry so it requires that session, that path agreement, and that next-candle confidence, then measure that one change. "
            "Keep the backtest on the last year of futures sessions, 260 sessions. Do not run this chart on the five-year download. "
            "A strong agreement is at least 8 of 10 paths the same way. Five of ten is not a trade. A flat next candle is not a trade. "
            "Leave out any circumstance that is not listed."
        )
    else:
        lines.append(
            "Write the filter from a line above. A strong agreement is at least 8 of 10 paths the same way. "
            "Five of ten is not a trade. A flat next candle is not a trade. Leave out any circumstance that is not listed."
        )
    return "\n".join(lines)


def _candle_row(entry: dict) -> dict:
    paths = int(entry.get("paths") or 0)
    agreed = int(entry.get("agreed") or 0)
    direction = str(entry.get("direction") or "split").lower()
    if direction not in {"up", "down", "split"}:
        direction = "split"
    if paths and agreed / paths <= 0.5:
        direction = "split"
    confidence = entry.get("confidence")
    if isinstance(confidence, str):
        peaked = confidence.strip().lower() == "peaked"
    else:
        try:
            peaked = float(confidence) >= 0.6
        except (TypeError, ValueError):
            peaked = False
    share = agreed / paths if paths else 0.0
    if share >= 0.8:
        band = "strong"
    elif share <= 0.5:
        band = "split"
    else:
        band = "leaning"
    return {
        "time": entry.get("time") or "",
        "instrument": str(entry.get("instrument") or "").upper(),
        "timeframe": str(entry.get("timeframe") or ""),
        "session": str(entry.get("session") or "unmeasured"),
        "paths": paths,
        "agreed": agreed,
        "direction": direction,
        "agreement": band,
        "confidence": "peaked" if peaked else "flat",
        "inside_90": bool(entry.get("inside_90")),
    }


def _candle_key(row: dict) -> tuple:
    return (
        row.get("instrument") or "",
        row.get("timeframe") or "",
        row.get("session") or "",
        row.get("direction") or "",
        row.get("agreement") or "",
        row.get("confidence") or "",
    )


def _candle_line(key: tuple, hits: int, count: int) -> str:
    instrument, timeframe, session, direction, agreement, confidence = key
    if agreement == "strong":
        agreed = "at least 8 of 10 paths"
    elif agreement == "split":
        agreed = "about half the paths each way"
    else:
        agreed = "a lean short of 8 of 10 paths"
    way = "up" if direction == "up" else "down" if direction == "down" else "with no agreed direction"
    return (
        f"{instrument} {timeframe}, {session}, {agreed} {way}, next candle {confidence}: "
        f"the real candle was inside the 0.9 set on {hits} of {count} bars."
    )


def _read_candles(path: Path | None = None) -> list[dict]:
    target = path or CANDLE_NOTES
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def prompt_for(cards: list[dict], lessons: list[dict] | None = None, *, brief_limit: int = 5200, chart: str | None = None, library_note: str = "") -> str:
    """Ask for one new script, written from scratch from successful backtests.

    The creation team learns from strategies that already passed a backtest. It does not
    learn from the scripts it wrote. Engine 4 develops the new script.
    """
    from futuresfund.exemplars import brief
    from futuresfund.indicators import creation_context
    from futuresfund.objective import acceptance_line, account_line, drawdown_share_text

    opening = (
        f"Write one new Pine v5 strategy from scratch for the {chart} chart. "
        "Do not copy a script already on file. Engine 4, the creation engine, develops it. "
        if chart else
        "Write one new Pine v5 strategy from scratch for this futures desk. "
        "Do not copy a script already on file. Engine 4, the creation engine, develops it. "
    )
    lines = [
        opening + "Learn from the successful backtested strategies below. Do not learn from a script this creation team wrote, and do not copy one. "
        "A backtest is filed on the Not tested on the full dataset list when it clears this bar. "
        f"{acceptance_line()} "
        "No more than 600 trades a year, and no single trade down more than $1,500. "
        "A script that takes more than 900 trades cannot be saved, so do not write one. "
        "A created script has to be profitable on the last 7 days, the last 30 days, the last 90 days, and the last 365 days. Write toward that bar. "
        "The candle notes below are for this creation team. Use one only when it lists at least 20 replayed bars and the real candle was inside the 0.9 set on most of them. "
        "Engine 4 develops the new script on the full five-year download. The file stays in createdStrategies.",
        "",
        brief(brief_limit),
        "",
        "Strategy library (profitable scripts already filed, by contract):",
        backtested_catalog(5),
        "",
        creation_context(1800),
        "",
    ]
    if library_note:
        lines.append(library_note)
        lines.append("")
    lines.append(candle_lines())
    lines.append("")
    ready = [card for card in measured_cards(cards) if not team_wrote(str(card.get("file") or ""))]
    if ready:
        lines.append(
            "Cards: measured one-variable changes from the studies, best under the objective first. "
            "Steadier means the result moved toward the profit target, or, once there, the drawdown fell or the profit rose inside the same drawdown band."
        )
        for card in ready[:4]:
            best = card.get("best") or {}
            head = f"Card: {card['title']} ({card['file']})"
            if best.get("net_profit") is not None:
                head += f" best profit {best.get('net_profit')}, drawdown {best.get('max_drawdown')}, trades {best.get('trades')}"
            lines.append(head)
            for change in card["changes"][:3]:
                lines.append(
                    f"- {change['change']}: profit {change['net_profit']}, drawdown {change['max_drawdown']}, "
                    f"trades {change['trades']}. {change['impact']}"
                )
        lines.append("")

    lines.extend([
        "Guideline 1, and it is not optional: the session is the standard inputs, start 18:00 and end 16:00 New York. "
        "Enter only while in_session. When in_session is false, strategy.close_all(comment=\"Session\"). "
        "Do not write time() with a weekday suffix. 12345 is not seconds and it shuts Friday. "
        "The researcher may change that window from the measured result. Do not move that close to add trades. "
        f"{account_line()} Do not write a script whose drawdown reaches {drawdown_share_text()}.",
        "Rules for the script:",
        "1. Line 1 is //@version=5. Line 2 is a comment: // Built from: <the lesson or card the new idea came from>, <the indicators used>. The body is a new strategy, not a copy.",
        "2. strategy(\"<a new title>\", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1, initial_capital=50000).",
        "3. Every value that can be tuned is an input.int or input.float with minval and maxval: lengths, stop, target, cooldown, session. The parameter is maxval. Never write maxmax or minmin. "
        "A stop is at least one tick and its default sits inside minval and maxval. On NQ one point is $20, so one tick is $5. "
        "The default stop on one contract must keep one trade inside 3 percent of the account. "
        "On NQ that is at most 75 points. On ES that is at most 30 points. On GC that is at most 15 points. On MNQ that is at most 750 points. "
        "The instrument roots are ES, GC, NQ, and MNQ. MNQ is $2 a point. Gold is GC. GOLD is not a root. "
        "The search may move that stop, and it reverses the move when one trade loses more than 3 percent of the account. "
        "The take profit is points times the point value, larger than the stop, and a distance one session can reach before the trade is closed at the session end. "
        "Do not write a stop of a fraction of a cent or a take profit of many hundreds of points on one contract.",
        "4. Every entry has strategy.exit(\"...\", from_entry=\"...\", stop=<price>, limit=<price>). "
        "Fixed prices only: no trail_ arguments, no limit or stop price on strategy.entry, no pyramiding. "
        "After strategy(), write this block and use it for every stop and limit. "
        "Every script uses this exit, including a script that is already being researched. "
        "If the script already has stop_dollars as input.int and closes when strategy.openprofit is at or below minus stop_dollars, do not write this block again. "
        "The stop and the take profit are whole dollars, input.int, not a percent of the account. "
        "// ===== RISK ===== "
        "pv = input.float(50, \"Point Value ($)\"). "
        "On ES the point value is 50. On NQ it is 20. On GC it is 100. The engine writes that value into pv before the backtest. "
        "On MNQ the point value is 2. "
        "stop_dollars = input.int(250, \"Stop ($)\", minval=250, maxval=1500). "
        "tp_dollars = input.int(500, \"Take Profit ($)\", minval=250, maxval=2000). "
        "use_trail = input.bool(false, \"Enable Trailing Stop\"). "
        "trail_step_amt = input.int(500, \"Trail: Tighten every ($)\"). "
        "trail_min_dollars = input.int(1000, \"Trail: Min Profit ($)\"). "
        "Write these prices and use them on strategy.exit: "
        "long_stop = close - stop_dollars / pv. long_limit = close + tp_dollars / pv. "
        "short_stop = close + stop_dollars / pv. short_limit = close - tp_dollars / pv. "
        "A created script that shows no profit after 20 attempts is scrapped, and a new script is written. "
        "Do not put a created script in the learning folder until it has a profit. "
        "The stop is not larger than 3 percent of the account and the take profit is not larger than 4 percent of the account. use_trail stays false. "
        "Do not write atr_len, stop_mult, tp_mult, atr_mult_sl, or atr_mult_tp. Do not write atr * stop_mult. "
        "A script with those lines is not accepted. ATR times a multiplier is not the exit, and changing the multiplier does not keep the loss inside the dollar stop. "
        "On every bar, if strategy.position_size is not zero and stop_dollars is not na and strategy.openprofit is at or below minus stop_dollars, strategy.close_all(comment=\"Open P&L > SL Risk\"). "
        "The search does not stop at a $1,000 stop and a $1,500 take profit. It measures stops of $250, $500, $750, $1,000, $1,250, and $1,500, and the take profit on those same steps through $2,000. "
        "The stop is checked on every price of the bar. One trade cannot lose more than the dollar stop, and not more than $1,500. "
        "Do not write a script that takes more than 900 trades. It cannot be saved. "
        "The script has to show a profit on the last 7 days, the last 30 days, the last 90 days, and the last 365 days.",
        "5. An entry needs at least two agreeing conditions: a trend or structure filter and a trigger, built from the indicator library.",
        "6. Only the built-in indicators listed above and plain if/else; no request.security, arrays, tables, plots, or alerts. "
        "ta.valuewhen takes a condition, then the series, then the occurrence: ta.valuewhen(not na(pivot_high), high, 1). "
        "Never write ta.valuewhen(pivot_high = pivot_high, high, 1). A named argument cannot be followed by a bare argument.",
        "7. The session from guideline 1 is required on every script. A cooldown in bars may be added as well, so the script does not trade every bar. "
        "Write this block and use in_session on every entry. "
        "// ===== SESSION ===== "
        "use_session = input.bool(true, \"Use Session Filter\"). "
        "sess_start_hr = input.int(18, \"Session Start Hour (ET)\"). "
        "sess_start_mn = input.int(0, \"Session Start Minute\"). "
        "sess_end_hr = input.int(16, \"Session End Hour (ET)\"). "
        "sess_end_mn = input.int(0, \"Session End Minute\"). "
        "in_session is true from the start hour and minute until the end hour and minute, New York, including when the end is the next day. "
        "Enter only while in_session. When in_session is false, strategy.close_all(comment=\"Session\"). "
        "Do not write a time() session and do not add a weekday suffix. Friday is included. "
        "The researcher may change that window from the measured result. Do not move that close to add trades. "
        "How to change a finished script is in researchNotes/how_to_research.md. Read that script before changing it. "
        "A cooldown is written with bar_index and a var int that stores the bar of the last entry. "
        "Do not use input.time, input.session, ta.barssince on the session, or compare time with a string.",
        "8. Under 80 lines. Reply with the script only, inside one ```pine block.",
    ])
    return "\n".join(lines).strip()


# Lessons: every created script and how it did.


def creation_lessons(limit: int = 6) -> list[dict]:
    rows = _read_lessons()
    return rows[-limit:] if limit else rows


def lesson_text(item: dict) -> str:
    result = (
        f"profit {item.get('net_profit')}, drawdown {item.get('max_drawdown')}, trades {item.get('trades')} on the 5-minute ES chart"
        if item.get("net_profit") is not None else "no result"
    )
    verdict = item.get("verdict") or ("met the target" if item.get("accepted") else "did not meet the target")
    text = f"{item.get('title') or item.get('name')}: {result}; {verdict}."
    if item.get("built_from"):
        text += f" Built from: {item['built_from']}."
    if item.get("indicators"):
        text += " Indicators: " + ", ".join(item["indicators"][:5]) + "."
    return text


def record_lesson(entry: dict, path: Path | None = None) -> None:
    target = path or LESSONS_PATH
    with _lessons_lock:
        rows = _read_lessons(target)
        rows.append(entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A dict, not a bare list: every reader of researchNotes/*.json expects a dict at the top.
        target.write_text(json.dumps({"kind": "creation_lessons", "lessons": rows[-200:]}, indent=1, default=str), encoding="utf-8")


def _read_lessons(path: Path | None = None) -> list[dict]:
    target = path or LESSONS_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("lessons") if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def built_from(source: str) -> str:
    match = re.search(r"//\s*Built from:\s*(.+)", source or "")
    return match.group(1).strip()[:200] if match else ""


# The round: write, check, test, confirm, promote, record.


def finished_chart_studies(learning: Path | None = None, notes: Path | None = None) -> list[str]:
    """Scripts whose 2-minute, 5-minute, and 15-minute studies are finished on one instrument."""
    from futuresfund.learn import RESEARCH_CHARTS, _chart_finished, research_instruments

    folder = learning or LEARNING
    store = notes or NOTES
    found = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.pine")):
        if not path.is_file() or path.name.startswith(_CREATED):
            continue
        record = _note_for(store, path)
        for root in research_instruments():
            if all(_chart_finished(record, root, timeframe) for timeframe in RESEARCH_CHARTS):
                found.append(path.name)
                break
    return found


def finished_on_chart(timeframe: str, learning: Path | None = None, notes: Path | None = None) -> list[str]:
    """Scripts that have finished this one chart on at least one instrument."""
    from futuresfund.learn import _chart_finished, research_instruments

    folder = learning or LEARNING
    store = notes or NOTES
    found = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.pine")):
        if not path.is_file() or path.name.startswith(_CREATED):
            continue
        record = _note_for(store, path)
        if any(_chart_finished(record, root, timeframe) for root in research_instruments()):
            found.append(path.name)
    return found


def _read_queue(path: Path | None = None) -> list[str]:
    target = path or QUEUE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("scripts") if isinstance(data, dict) else data
    return [str(row) for row in rows if str(row).strip()] if isinstance(rows, list) else []


def enqueue_script(name: str, path: Path | None = None) -> None:
    """Engine 4 develops this file next. Writers do not wait for that backtest."""
    target = path or QUEUE_PATH
    with _create_lock:
        rows = _read_queue(target)
        rows.append(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"kind": "creation_queue", "scripts": rows}, indent=1), encoding="utf-8")


def take_queued_script(path: Path | None = None) -> str:
    """The oldest script waiting for engine 4. Empty when the writers have not finished one yet."""
    target = path or QUEUE_PATH
    with _create_lock:
        rows = _read_queue(target)
        if not rows:
            return ""
        name = rows.pop(0)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"kind": "creation_queue", "scripts": rows}, indent=1), encoding="utf-8")
        return name


def run_chart_creation(board, pause, desk: dict) -> None:
    """Write new scripts from scratch the whole time research is running."""
    from futuresfund.learn import _sleep, _wait

    developer = desk["strategy_developer"]
    label = desk["label"]
    rejects = 0
    announced = False
    while not board.cancel.is_set():
        if not _wait(board, pause):
            return
        if board.holding_new_scripts():
            if not announced:
                board.post(
                    developer,
                    f"The {label} strategy developer is not writing a new strategy. "
                    "Each chart finishes the learning folder, then studies createdStrategies.",
                    kind="log",
                    channel=developer,
                )
                announced = True
            board.set_activity(developer, f"Not writing a new {label} strategy.")
            if not _sleep(board, pause, 5):
                return
            continue
        announced = False
        board.set_status(developer, "working")
        board.set_task(developer, f"Writing a new {label} strategy from scratch", working=True)
        ready = [card for card in measured_cards(saved_cards()) if not team_wrote(str(card.get("file") or ""))]
        library_note, library_ids = new_library_lines()
        source_name = "the successful backtests" if library_note else "from scratch"
        board.post(
            developer,
            f"Writing a new {label} strategy from scratch from {source_name}. Engine 4 will develop it.",
            kind="log",
            channel=developer,
        )
        outcome = handoff(board, ready, source_name, desk, library_note=library_note)
        mark_library(library_ids)
        if board.cancel.is_set() or outcome == "stopped":
            return
        if outcome == "sent_back":
            rejects += 1
            if rejects >= MAX_SENT_BACK:
                board.post(
                    SCRIPT_CHECKER,
                    f"The script for the {label} chart was sent back {MAX_SENT_BACK} times. The {label} strategy developer will try a different script.",
                    kind="report",
                    channel=SCRIPT_CHECKER,
                )
                rejects = 0
                if not _sleep(board, pause, 60):
                    return
            continue
        rejects = 0
        if not _sleep(board, pause, 1):
            return


def handoff(board, cards: list[dict], source_name: str, desk: dict, library_note: str = "") -> str:
    """Write one script and queue it for engine 4. The chart engines study it after a profitable run."""
    from futuresfund.learn import file_indicators, pine_facts

    developer = desk["strategy_developer"]
    label = desk["label"]
    reply = _ask(
        board,
        developer,
        f"Writing one Pine script for the {label} chart after {source_name}",
        prompt_for(cards, chart=label, library_note=library_note),
    )
    if board.cancel.is_set():
        return "stopped"
    source = repair_pine(extract_pine(reply))
    problems = pine_problems(source)
    title = _title_from(source) if source else "No script"
    if problems:
        board.post(
            SCRIPT_CHECKER,
            f"The script for the {label} chart was sent back. " + " ".join(problems),
            kind="report",
            channel=SCRIPT_CHECKER,
        )
        record_lesson({
            "time": _now(),
            "name": "",
            "title": title,
            "built_from": built_from(source),
            "indicators": [],
            "net_profit": None,
            "max_drawdown": None,
            "trades": None,
            "accepted": False,
            "verdict": f"sent back by the script checker before engine 4 could develop it: " + " ".join(problems),
        })
        return "sent_back"
    board.post(
        SCRIPT_CHECKER,
        f"The script for the {label} chart has a version line, an entry, an exit, and a protective stop. Engine 4 will develop it.",
        kind="log",
        channel=SCRIPT_CHECKER,
    )
    name = _store_created(title, source)
    enqueue_script(name)
    facts = pine_facts(source) if source else {}
    from futuresfund.exemplars import indicator_names

    used = indicator_names(source)
    file_indicators(board, name, source, developer)
    verdict = (
        f"Queued {name} for engine 4. The {label} strategy developer saved it in createdStrategies. "
        "Engine 4 develops it on the full five-year download. "
        "The three backtest engines file a one-year result on the Not tested on the full dataset list when it clears the library acceptance."
    )
    record_lesson({
        "time": _now(),
        "name": name,
        "title": title or facts.get("title") or name,
        "built_from": built_from(source),
        "indicators": used,
        "net_profit": None,
        "max_drawdown": None,
        "trades": None,
        "accepted": False,
        "promoted": "",
        "verdict": verdict,
    })
    board.post(LESSON_WRITER, f"Recorded {name}. {verdict}", kind="report", channel=LESSON_WRITER)
    board.set_status(developer, "working")
    return "queued"


def run_creation(board, pause) -> None:
    """Run one strategy developer for each chart. A chart does not wait on the other two."""
    from futuresfund.roster import CHART_DESKS

    threads = []
    for desk in CHART_DESKS:
        thread = threading.Thread(
            target=run_chart_creation,
            args=(board, pause, desk),
            daemon=True,
            name=desk["strategy_developer"],
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()


def develop_one(board, pause) -> str:
    """Develop the next queued script on engine 4. The chart engines file a result that clears the library acceptance."""
    name = take_queued_script()
    if not name:
        board.set_task(CREATION_TESTER, "Waiting for the next script to develop on engine 4", working=True)
        return "waiting"
    from futuresfund.learn import clear_developing, mark_developing

    mark_developing(name)
    try:
        return _develop_queued(board, pause, name)
    finally:
        clear_developing(name)


def _develop_queued(board, pause, name: str) -> str:
    """Develop one created script. The caller keeps the charts off this file until this returns."""
    from futuresfund.backtrader_engine import finish_study
    from futuresfund.exemplars import indicator_names
    from futuresfund.learn import (
        ACCOUNT_SIZE,
        ATTEMPT_LIMIT,
        _engine_execute,
        _load_frames,
        _pine_params,
        _search,
        _with_contract_point,
        pine_facts,
        promote_fixed_exits,
        promote_fixed_pivots,
    )
    from futuresfund.objective import consistent_spans, research_success

    path = CREATED / name
    if not path.is_file():
        return "missing"
    source = path.read_text(encoding="utf-8", errors="replace")
    prepared = promote_fixed_pivots(promote_fixed_exits(source))
    promoted = prepare_backtest(prepared, "ES")
    if promoted != source:
        path.write_text(promoted, encoding="utf-8")
        if promote_fixed_pivots(source) != source:
            board.post(
                CREATION_TESTER,
                f"{name} had a fixed pivot length. It is an input now, so the search can measure it.",
                kind="log",
                channel=CREATION_TESTER,
            )
        if promoted != prepared:
            board.post(
                CREATION_TESTER,
                f"{name} now uses the shared risk block: a $50 stop, a $500 take profit, and the trail off.",
                kind="log",
                channel=CREATION_TESTER,
            )
        source = promoted
    facts = pine_facts(source) if source else {"title": path.stem, "inputs": [], "named": {}}
    frames = _load_frames("ES")
    board.set_status(CREATION_TESTER, "working")
    board.set_task(CREATION_TESTER, f"Developing {name} on engine 4", working=True)
    board.post(
        CREATION_TESTER,
        f"Engine 4 is developing {name} on the full five-year download. The script stays in createdStrategies. The chart engines file a one-year result that clears the library acceptance.",
        kind="report",
        channel=CREATION_TESTER,
    )
    execute = _engine_execute(source, frames, facts, board, "ES", CREATION_TESTER, CREATION_CHART, CREATION_ENGINE, full=True)
    try:
        trials = _search(
            _with_contract_point(_pine_params(facts), "ES"),
            execute,
            ACCOUNT_SIZE,
            ATTEMPT_LIMIT,
            board,
            path,
            pause,
            facts,
            extras=False,
            developer=CREATION_TESTER,
            researcher=STRATEGY_DEVELOPER,
            accept=lambda row: research_success(row, ACCOUNT_SIZE) and consistent_spans(row.get("spans")),
            slot=CREATION_ENGINE,
        )
    finally:
        finish_study(CREATION_ENGINE)
    if board.cancel.is_set():
        return "stopped"
    if getattr(trials, "flat_stop", False):
        return "cannot_run"
    if getattr(trials, "scrapped", False):
        note = (trials[-1].get("note") if trials else "") or f"{name} was scrapped after 20 attempts with no profit."
        record_lesson({
            "time": _now(),
            "name": name,
            "title": facts.get("title") or name,
            "built_from": built_from(source),
            "indicators": indicator_names(source),
            "net_profit": None,
            "max_drawdown": None,
            "trades": None,
            "accepted": False,
            "verdict": note,
        })
        return "scrapped"
    if getattr(trials, "skipped", False):
        record_lesson({
            "time": _now(),
            "name": name,
            "title": facts.get("title") or name,
            "built_from": built_from(source),
            "indicators": indicator_names(source),
            "net_profit": None,
            "max_drawdown": None,
            "trades": None,
            "accepted": False,
            "verdict": f"{name} was skipped on engine 4. The next queued script is next.",
        })
        board.post(CREATION_TESTER, f"{name} was skipped. Taking the next script.", kind="log", channel=CREATION_TESTER)
        return "skipped"
    proved = next((row for row in trials if research_success(row, ACCOUNT_SIZE) and consistent_spans(row.get("spans"))), None)
    if proved:
        promoted = ""
        verdict = (
            f"Profit {proved.get('net_profit')}, drawdown {proved.get('max_drawdown')}, trades {proved.get('trades')}. "
            f"{name} stays in createdStrategies. Engine 4 measured it on the full five-year download. "
            "The three backtest engines file a one-year result on the Not tested on the full dataset list "
            "when it clears the library acceptance."
        )
        accepted = False
    else:
        promoted = ""
        note = (trials[-1].get("note") if trials else "") or ""
        verdict = note or f"{name} did not find a profit on the full five-year download on engine 4. It stays in createdStrategies. The next script is next."
        accepted = False
    record_lesson({
        "time": _now(),
        "name": name,
        "title": facts.get("title") or name,
        "built_from": built_from(source),
        "indicators": indicator_names(source),
        "net_profit": None if not proved else proved.get("net_profit"),
        "max_drawdown": None if not proved else proved.get("max_drawdown"),
        "trades": None if not proved else proved.get("trades"),
        "accepted": accepted,
        "promoted": promoted,
        "verdict": verdict,
    })
    board.post(LESSON_WRITER, f"Recorded {name}. {verdict}", kind="report", channel=LESSON_WRITER)
    board.post(CREATION_TESTER, f"{name}: {verdict}", kind="report", channel=CREATION_TESTER)
    return "studied" if proved else "unproven"


def run_creation_engine(board, pause) -> None:
    """Keep engine 4 developing a new script whenever research is running."""
    from futuresfund.backtrader_engine import clear_restart, clear_skip, restarted
    from futuresfund.learn import _sleep, _wait, engine4_should_study_learning, study_learning_on_engine_4

    announced = False
    while not board.cancel.is_set():
        if not _wait(board, pause):
            return
        if engine4_should_study_learning() or restarted(CREATION_ENGINE):
            announced = False
            clear_restart(CREATION_ENGINE)
            clear_skip(CREATION_ENGINE)
            outcome = study_learning_on_engine_4(board, pause)
            if outcome == "none" and not _sleep(board, pause, 5):
                return
            continue
        if board.holding_new_scripts():
            outcome = study_learning_on_engine_4(board, pause)
            if outcome == "none":
                if not announced:
                    board.post(
                        CREATION_TESTER,
                        "Engine 4 is not developing a new script. The learning folder has nothing waiting on a chart.",
                        kind="log",
                        channel=CREATION_TESTER,
                    )
                    announced = True
                board.set_task(CREATION_TESTER, "Not developing a new script.", working=False)
                if not _sleep(board, pause, 5):
                    return
            else:
                announced = False
            continue
        announced = False
        outcome = develop_one(board, pause)
        if outcome == "stopped" or board.cancel.is_set():
            return
        if outcome == "waiting" and not _sleep(board, pause, 2):
            return


def create_once(board, pause, cards: list[dict], source_name: str) -> str:
    """One creation round. Returns sent_back, blocked, or done."""
    from futuresfund.learn import (
        ACCOUNT_SIZE,
        ATTEMPT_LIMIT,
        _engine_execute,
        _load_frames,
        _pine_params,
        _rank,
        _search,
        _with_contract_point,
        file_indicators,
        pine_facts,
    )
    from futuresfund.exemplars import drawdown_cap, meets_target, profit_target
    from futuresfund.objective import consistent_spans, pace_text

    reply = _ask(
        board,
        STRATEGY_DEVELOPER,
        f"Writing one Pine script after {source_name}",
        prompt_for(cards),
    )
    source = repair_pine(extract_pine(reply))
    problems = pine_problems(source)
    frames = _load_frames("ES") if not problems else {}
    if not problems:
        problems = runtime_problems(source, frames.get(CREATION_CHART) or [])
    if problems:
        board.post(
            SCRIPT_CHECKER,
            "The script was sent back. " + " ".join(problems),
            kind="report",
            channel=SCRIPT_CHECKER,
        )
        record_lesson({
            "time": _now(),
            "name": "",
            "title": _title_from(source) if source else "No script",
            "built_from": built_from(source),
            "indicators": [],
            "net_profit": None,
            "max_drawdown": None,
            "trades": None,
            "accepted": False,
            "verdict": "sent back by the script checker: " + " ".join(problems),
        })
        return "sent_back"
    board.post(
        SCRIPT_CHECKER,
        f"The script has a version line, an entry, an exit, and a protective stop, and it ran on a slice of real bars. Engine 4 will run up to {ATTEMPT_LIMIT} attempts "
        f"on the full five-year download until it hits the ${profit_target():,.0f} profit target inside the drawdown cap of ${drawdown_cap():,.0f} at {pace_text()}.",
        kind="log",
        channel=SCRIPT_CHECKER,
    )
    title = _title_from(source)
    name = _store_created(title, source)
    created = CREATED / name
    facts = pine_facts(source) if source else {"title": title, "inputs": [], "named": {}}
    indicators = [row["name"] for row in file_indicators(board, name, source, CREATION_TESTER) or []]
    from futuresfund.exemplars import indicator_names

    used = indicator_names(source)
    from futuresfund.backtrader_engine import _bars_from
    from futuresfund.entry_rules import ensure_entry_rule

    if ensure_entry_rule(board, CREATION_TESTER, source, _bars_from(frames.get(CREATION_CHART) or []), wait=True) is not True:
        record_lesson({
            "time": _now(), "name": name, "title": title, "built_from": built_from(source), "indicators": used,
            "net_profit": None, "max_drawdown": None, "trades": None, "accepted": False,
            "verdict": "the engine could not enter with this script and the entry rule was not written",
        })
        return "blocked"
    board.post(
        CREATION_TESTER,
        f"Engine 4 is backtesting {name} on the 5-minute ES chart over the full five-year download, up to {ATTEMPT_LIMIT} attempts. "
        f"The file stays in createdStrategies. Target: profit ${profit_target():,.0f} inside the drawdown cap of ${drawdown_cap():,.0f}, at {pace_text()}.",
        kind="report",
        channel=CREATION_TESTER,
    )
    execute = _engine_execute(source, frames, facts, board, "ES", CREATION_TESTER, CREATION_CHART, CREATION_ENGINE, full=True)
    trials = _search(
        _with_contract_point(_pine_params(facts), "ES"),
        execute,
        ACCOUNT_SIZE,
        ATTEMPT_LIMIT,
        board,
        created,
        pause,
        facts,
        extras=False,
        developer=CREATION_TESTER,
        researcher=STRATEGY_DEVELOPER,
        accept=lambda row: meets_target(row) and consistent_spans(row.get("spans")),
        slot=CREATION_ENGINE,
    )
    if board.cancel.is_set():
        return "stopped"
    if getattr(trials, "flat_stop", False):
        return "cannot_run"
    if getattr(trials, "scrapped", False):
        note = (trials[-1].get("note") if trials else "") or f"{name} was scrapped after 20 attempts with no profit."
        record_lesson({
            "time": _now(), "name": name, "title": title, "built_from": built_from(source), "indicators": used,
            "net_profit": None, "max_drawdown": None, "trades": None, "accepted": False,
            "verdict": note,
        })
        board.post(CREATION_TESTER, note, kind="log", channel=CREATION_TESTER)
        return "scrapped"
    if getattr(trials, "skipped", False):
        record_lesson({
            "time": _now(), "name": name, "title": title, "built_from": built_from(source), "indicators": used,
            "net_profit": None, "max_drawdown": None, "trades": None, "accepted": False,
            "verdict": f"{name} was skipped. A script with more than 900 trades cannot be saved.",
        })
        board.post(CREATION_TESTER, f"{name} was skipped. Taking the next script.", kind="log", channel=CREATION_TESTER)
        return "skipped"
    best = max(trials, key=_rank) if trials else {}
    accepted = bool(best) and meets_target(best) and consistent_spans(best.get("spans"))
    confirmations: list[dict] = []
    green = 0
    inside = False
    if accepted:
        confirmations = confirm(source, frames, facts, board, best.get("params") or {})
        green = 1 + sum(1 for row in confirmations if _chart_green(row))
        inside = len(confirmations) == len(CONFIRM_CHARTS) and all(_chart_inside(row) for row in confirmations)
    promoted = name if accepted and inside else ""
    verdict = _verdict(best, accepted, confirmations, green, promoted, len(trials))
    board.post(CREATION_TESTER, f"{name}: {verdict}", kind="report", channel=CREATION_TESTER)
    lesson = {
        "time": _now(),
        "name": name,
        "title": title,
        "built_from": built_from(source),
        "indicators": used or indicators,
        "attempts": len(trials),
        "net_profit": best.get("net_profit"),
        "max_drawdown": best.get("max_drawdown"),
        "trades": best.get("trades"),
        "params": best.get("params") or {},
        "confirmations": [
            {"timeframe": row.get("timeframe"), "net_profit": row.get("net_profit"), "max_drawdown": row.get("max_drawdown"), "trades": row.get("trades")}
            for row in confirmations
        ],
        "green_charts": green if accepted else 0,
        "accepted": accepted,
        "promoted": promoted,
        "verdict": verdict,
    }
    record_lesson(lesson)
    board.post(
        LESSON_WRITER,
        f"Recorded {name}. {verdict}",
        kind="report",
        channel=LESSON_WRITER,
    )
    return "done"


def confirm(source: str, frames: dict, facts: dict, board, params: dict) -> list[dict]:
    """Run the accepted settings once on each of the other ES charts, on engine 4."""
    from futuresfund.learn import _engine_execute

    rows = []
    for timeframe in CONFIRM_CHARTS:
        if board is not None and board.cancel.is_set():
            break
        execute = _engine_execute(source, frames, facts, board, "ES", CREATION_TESTER, timeframe, CREATION_ENGINE, full=True)
        measured = execute(dict(params)) or {}
        measured["timeframe"] = timeframe
        rows.append(measured)
    return rows


def _chart_inside(row: dict) -> bool:
    """The promotion gate: the chart ran, and its max drawdown stayed within the one-year cap."""
    from futuresfund.exemplars import drawdown_cap

    if not isinstance(row, dict) or row.get("error") or row.get("fatal") or row.get("blocked") or row.get("breached"):
        return False
    try:
        drawdown = float(row.get("max_drawdown") or 0)
        float(row.get("net_profit"))
    except (TypeError, ValueError):
        return False
    return drawdown <= drawdown_cap()


def _chart_green(row: dict) -> bool:
    """Reported next to a confirmation, not the gate: profitable, keeps more than it gives back, inside the trailing limit."""
    from futuresfund.exemplars import CHART_TRADES, trailing_limit

    if not isinstance(row, dict) or row.get("error") or row.get("fatal"):
        return False
    try:
        profit = float(row.get("net_profit"))
        drawdown = float(row.get("max_drawdown") or 0)
        trades = int(row.get("trades") or 0)
    except (TypeError, ValueError):
        return False
    return profit > 0 and profit > drawdown and drawdown <= trailing_limit() and trades >= CHART_TRADES


def promote(board, name: str, source: str, params: dict, folder: Path | None = None) -> str:
    """Write the accepted settings onto the created script. It stays in createdStrategies."""
    from futuresfund.strategy import _apply_inputs

    folder = folder or CREATED
    folder.mkdir(parents=True, exist_ok=True)
    tuned = source
    if params:
        try:
            tuned = _apply_inputs(source, params)
        except (TypeError, ValueError, re.error):
            tuned = source
    filename = Path(name).name
    if not filename.startswith(_CREATED):
        filename = f"{_CREATED}{Path(filename).stem}.pine"
    target = folder / filename
    target.write_text(tuned if tuned.endswith("\n") else tuned + "\n", encoding="utf-8")
    if board is not None:
        from futuresfund.roster import CHART_DESKS

        for desk in CHART_DESKS:
            board.post(
                desk["developer"],
                f"{CREATION_TESTER} kept {target.name} in createdStrategies with the accepted settings. A chart studies it after that chart finishes the learning folder.",
                kind="log",
                channel=desk["developer"],
            )
        board.post(
            "Trading Analyst",
            f"{target.name} stayed in createdStrategies after engine 4 hit the profit target on the full five-year download and the drawdown stayed inside the cap. "
            "A chart studies it after that chart finishes the learning folder. It reaches the strategy library only after they finish it.",
            kind="report",
            channel="Trading Analyst",
        )
    return target.name


def _verdict(best: dict, accepted: bool, confirmations: list[dict], green: int, promoted: str, attempts: int) -> str:
    from futuresfund.exemplars import drawdown_cap, profit_target
    from futuresfund.objective import pace_text, paced, trades_per_week

    if not best:
        return f"Engine 4 recorded no result in {attempts} attempts."
    if best.get("error") or best.get("fatal"):
        return f"The engine stopped it after {attempts} attempts on the 5-minute ES chart: {best.get('error') or 'no result'} It stays in createdStrategies."
    rate = trades_per_week(best)
    pace_note = f", {rate:g} trades a week" if rate is not None else ""
    head = f"Profit {best.get('net_profit')}, drawdown {best.get('max_drawdown')}, trades {best.get('trades')}{pace_note} after {attempts} attempts on the 5-minute ES chart."
    if not accepted:
        why = f" The pace was outside {pace_text()}." if paced(best) is False else ""
        return (
            f"{head} It did not meet the target (profit ${profit_target():,.0f} without crossing the ${drawdown_cap():,.0f} trailing drawdown, "
            f"at {pace_text()}).{why} It stays in createdStrategies."
        )
    charts = "; ".join(
        f"{row.get('timeframe')} profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}"
        for row in confirmations
    )
    if promoted:
        return (
            f"{head} Met the target. Confirmation: {charts}. The drawdown stayed inside ${drawdown_cap():,.0f} on every ES chart "
            f"(green on {green} of 3). Engine 4 measured the full five-year download. It stays in createdStrategies as {promoted}. "
            "The three backtest engines file a one-year result on the Not tested on the full dataset list when it clears the library acceptance."
        )
    return (
        f"{head} Met the target on the 5-minute chart, but the drawdown went past ${drawdown_cap():,.0f} on another chart ({charts}). "
        f"Green on {green} of 3. It stays in createdStrategies."
    )


def _row_is_profitable(row: dict) -> bool:
    from futuresfund.learn import _row_profitable

    return _row_profitable(row)


def _next_source() -> str | None:
    seen = _seen_sources()
    for name in finished_chart_studies():
        if name not in seen:
            return name
    return None


def _seen_sources() -> set[str]:
    if not SEEN_PATH.is_file():
        return set()
    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    found = data.get("seen") if isinstance(data, dict) else None
    return {str(item) for item in found} if isinstance(found, list) else set()


def _remember_source(name: str) -> None:
    seen = _seen_sources()
    seen.add(name)
    NOTES.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps({"seen": sorted(seen)}, indent=2), encoding="utf-8")


def _store_created(title: str, source: str) -> str:
    """Save a new script in createdStrategies. A chart studies that folder after it finishes the learning folder."""
    CREATED.mkdir(parents=True, exist_ok=True)
    name = script_name(title, CREATED)
    text = source if source.endswith("\n") else source + "\n"
    (CREATED / name).write_text(text, encoding="utf-8")
    return name


def _note_for(store: Path, path: Path) -> dict:
    file = store / f"{path.stem.replace(' ', '_')}.json"
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


CARD_CHANGES = 12


def _changes(record: dict) -> list[dict]:
    """The measured changes worth reading: enough trades to mean something, steadiest first."""
    from futuresfund.exemplars import CHART_TRADES

    rows = []
    seen = set()
    for trial in _trials(record):
        change = str(trial.get("change") or "").strip()
        if not change or change in {"Original", "Starting settings", "Same settings"} or change in seen:
            continue
        if trial.get("error") or trial.get("fatal") or trial.get("net_profit") is None:
            continue
        try:
            trades = int(trial.get("trades") or 0)
        except (TypeError, ValueError):
            trades = 0
        if trades < CHART_TRADES:
            continue
        from futuresfund.objective import trade_loss_ok

        try:
            profit = float(trial.get("net_profit"))
        except (TypeError, ValueError):
            continue
        if profit <= 0 or not trade_loss_ok(trial):
            continue
        seen.add(change)
        rows.append({
            "change": change,
            "impact": str(trial.get("impact") or "").strip(),
            "net_profit": trial.get("net_profit"),
            "max_drawdown": trial.get("max_drawdown"),
            "trades": trades,
        })
    rows.sort(key=_change_rank, reverse=True)
    return rows[:CARD_CHANGES]


def _change_rank(change: dict) -> tuple:
    """Changes that made the book steadier and profitable first, then the research objective."""
    from futuresfund.objective import rank

    try:
        profit = float(change.get("net_profit") or 0)
    except (TypeError, ValueError):
        profit = 0.0
    return ("steadier" in str(change.get("impact") or "") and profit > 0,) + rank(change)


def _trials(record: dict) -> list[dict]:
    found = []
    instruments = record.get("instruments") if isinstance(record.get("instruments"), dict) else {}
    for item in instruments.values():
        if not isinstance(item, dict):
            continue
        charts = item.get("charts") if isinstance(item.get("charts"), dict) else {}
        if charts:
            for chart in charts.values():
                if isinstance(chart, dict):
                    found.extend(row for row in chart.get("trials") or [] if isinstance(row, dict))
        else:
            found.extend(row for row in item.get("trials") or [] if isinstance(row, dict))
    if not found:
        found.extend(row for row in record.get("trials") or [] if isinstance(row, dict))
    return found


def _ask(board, agent: str, activity: str, prompt: str) -> str:
    """The developer writes from the prompt alone, on this developer's own thread."""
    from futuresfund.llm import complete, use_agent

    if board is not None:
        board.log(agent, activity)
    with use_agent(agent):
        reply = complete(prompt, agent, fresh=True)
    return reply or ""


def _title_from(source: str) -> str:
    match = re.search(r'strategy\s*\(\s*"([^"]+)"', source or "")
    if match:
        return match.group(1)
    return "strategy"


def _now() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")
