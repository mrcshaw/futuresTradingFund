"""Patterns a researcher names from a chart.

A pattern is a behavior counted more than once on the last year of one chart.
The desk does not decide which behavior matters. It counts sequences of bar
direction inside a New York session and keeps the ones that repeat. The
researcher names one of those counts. A later test records whether a script
that tried to capture it traded and made money.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from futuresfund.learn import NOTES

PATH = NOTES / "chart_patterns.json"
NAMED_PATH = NOTES / "named_pattern.json"
_LOCK = threading.Lock()
_NY = ZoneInfo("America/New_York")
_OPEN = "profitable"
_CLOSED = {"no_trades", "unprofitable"}


def repeats(bars, instrument: str, timeframe: str) -> list[dict]:
    """Direction pairs that show up more than once on the last year of this chart.

    A pair seen once is left out. The words are only a description of the bars,
    so the researcher can name the pattern. Nothing here is a strategy.
    """
    rows = [_bar(bar) for bar in bars or []]
    rows = [row for row in rows if row is not None]
    rows = _last_year(rows)
    counts: dict[tuple[str, str, str], int] = {}
    for left, right in zip(rows, rows[1:]):
        if left["session"] == "halt" or left["session"] != right["session"]:
            continue
        if left["direction"] == "flat" or right["direction"] == "flat":
            continue
        key = (left["session"], left["direction"], right["direction"])
        counts[key] = counts.get(key, 0) + 1
    found = []
    for (session, first, second), count in counts.items():
        if count < 2:
            continue
        ident = f"{session}-{first}-{second}"
        found.append({
            "id": ident,
            "instrument": instrument,
            "timeframe": timeframe,
            "session": session,
            "count": count,
            "description": (
                f"{instrument} {timeframe}, {session}: a {first} bar followed by a {second} bar. "
                f"Counted {count} times on the last year of this chart."
            ),
        })
    found.sort(key=lambda row: (-int(row["count"]), row["id"]))
    return found[:8]


def load(path=None) -> list[dict]:
    target = path or PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("patterns") if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def record(entry: dict, path=None) -> None:
    """Keep one row per pattern. A later test replaces the earlier row."""
    target = path or PATH
    ident = str(entry.get("id") or "")
    with _LOCK:
        rows = [row for row in load(target) if str(row.get("id") or "") != ident]
        rows.append(dict(entry))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"kind": "chart_patterns", "patterns": rows}, indent=1), encoding="utf-8")


def has_profitable(path=None) -> bool:
    return any(row.get("status") == _OPEN for row in load(path))


def named_text(path=None) -> str:
    """The pattern the researcher typed. Empty until they save one."""
    target = path or NAMED_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("text") or "").strip()


def save_named(text: str, path=None) -> str:
    """Keep the researcher's words. The next new script takes that entry."""
    target = path or NAMED_PATH
    cleaned = str(text or "").replace("\r\n", "\n").strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"kind": "named_pattern", "text": cleaned}, indent=1), encoding="utf-8")
    return cleaned


def named_row(text: str, instrument: str, timeframe: str) -> dict | None:
    """One pattern row for the text the researcher saved. The desk does not rewrite it."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None
    ident = "named-" + hashlib.sha1(cleaned.casefold().encode()).hexdigest()[:10]
    return {
        "id": ident,
        "instrument": instrument,
        "timeframe": timeframe,
        "session": "named",
        "source": "named",
        "description": str(text or "").strip(),
        "name": cleaned,
    }


def offered_patterns(instrument: str = "ES", timeframe: str = "") -> list[dict]:
    """Patterns a researcher may choose. A counted repeat is a separate choice."""
    return [{
        "id": "large-move-rejection",
        "instrument": instrument,
        "timeframe": timeframe,
        "session": "offered",
        "source": "offered",
        "name": "Large move rejection",
        "description": (
            "Do not measure the size of the move until price moves more than the impulse-tick input inside the impulse-minute input. "
            "The script starts at more than 250 ticks in 15 minutes on the 2-minute ES chart. "
            "The move must finish within the move-minutes input after that burst. The script starts at 45 minutes. A new extreme after that window is not traded. "
            "move_points is how many points the move must be and retest_points is how far from that price the bounce may be. "
            "A move up that comes back within retest_points of the high and closes down is a short. "
            "A move down that bounces within retest_points of the low and closes up is a buy. "
            "After a trade closes, cooldown bars must pass before the next entry. "
            "Realized session profit at the daily profit cap, or a realized session loss at the daily loss cap, stops new trades until the next session. "
            "The script on file is Large Move Rejection ES.txt. USE that file when the entry already matches. Do not copy it."
        ),
    }]


def prompt_lines(measured: list[dict], saved: list[dict] | None = None) -> str:
    """What the next script may use. A failed test stays on the record and is not named again."""
    rows = list(saved if saved is not None else load())
    closed = {str(row.get("id") or "") for row in rows if row.get("status") in _CLOSED}
    open_rows = [row for row in measured if str(row.get("id") or "") not in closed]
    offered = [row for row in open_rows if row.get("source") == "offered"]
    counted = [row for row in open_rows if row.get("source") != "offered"]
    lines = [
        "Name one pattern with PATTERN: and its id. The researcher chooses. The application does not choose it.",
    ]
    if offered:
        lines.append("Offered patterns. Choose one of these, a measured repeat, or a seed script:")
        lines.extend(f"- {row['id']}: {row['description']}" for row in offered)
    lines.append(
        "Measured repeats on this chart. Each line was counted more than once on the last year. "
        "A behavior that was counted once is not listed."
    )
    if counted:
        lines.extend(f"- {row['id']}: {row['description']}" for row in counted)
    else:
        lines.append("No open repeat is left on this chart.")
    profitable = [row for row in rows if row.get("status") == _OPEN]
    if profitable:
        lines.append("Patterns that traded and made money. The next script may vary one input of one of these:")
        for row in profitable:
            lines.append(
                f"- {row.get('id')}: {row.get('name') or row.get('description')} "
                f"on {row.get('instrument')} {row.get('timeframe')} {row.get('session')}, "
                f"counted {row.get('count')} times, test profit {row.get('net_profit')}, trades {row.get('trades')}."
            )
    failed = [row for row in rows if row.get("status") in _CLOSED]
    if failed:
        lines.append(
            f"{len(failed)} tested patterns did not trade or did not make money. "
            "They stay on the record and are not the next idea."
        )
    return "\n".join(lines)


def _bar(bar):
    if bar is None:
        return None
    if isinstance(bar, dict):
        opened = bar.get("o", bar.get("open"))
        closed = bar.get("c", bar.get("close"))
        epoch = bar.get("epoch", bar.get("t"))
    else:
        opened = getattr(bar, "o", None)
        closed = getattr(bar, "c", None)
        epoch = getattr(bar, "epoch", None)
    try:
        opened = float(opened)
        closed = float(closed)
    except (TypeError, ValueError):
        return None
    when = _when(epoch)
    if closed > opened:
        direction = "up"
    elif closed < opened:
        direction = "down"
    else:
        direction = "flat"
    return {"session": _session(when), "direction": direction, "epoch": epoch}


def _when(epoch):
    try:
        stamp = float(epoch)
    except (TypeError, ValueError):
        return None
    if stamp > 10_000_000_000:
        stamp = stamp / 1000
    return datetime.fromtimestamp(stamp, _NY)


def _session(when) -> str:
    if when is None:
        return "unmeasured"
    mark = when.hour * 60 + when.minute
    if 16 * 60 + 45 <= mark < 18 * 60:
        return "halt"
    if mark >= 18 * 60 or mark < 9 * 60 + 30:
        return "overnight"
    if mark < 12 * 60:
        return "morning"
    return "afternoon"


def _last_year(rows: list[dict]) -> list[dict]:
    stamps = []
    for row in rows:
        try:
            stamps.append(float(row["epoch"]))
        except (TypeError, ValueError, KeyError):
            continue
    if not stamps:
        return rows
    latest = max(stamps)
    if latest > 10_000_000_000:
        latest = latest / 1000
    start = latest - timedelta(days=365).total_seconds()
    kept = []
    for row in rows:
        try:
            stamp = float(row["epoch"])
        except (TypeError, ValueError, KeyError):
            kept.append(row)
            continue
        if stamp > 10_000_000_000:
            stamp = stamp / 1000
        if stamp >= start:
            kept.append(row)
    return kept
