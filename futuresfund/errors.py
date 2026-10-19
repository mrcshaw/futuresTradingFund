"""The error journal and the workflow error report.

Every error raised while the workflow runs is written down here: an error a desk posts to the board, a
backtest the engine could not finish, an email that did not go out, and a request the server could not
serve. The Systems Administrator compiles the report from the journal, by desk, with the errors that
keep coming back, and hands it to the Portfolio Manager, who emails it with ``mailer.send_report`` to
the fund address. The report goes out once a day at 5:05pm ET when there are new errors, and whenever
the Portfolio Manager is asked for it in chat. Nothing in the report is invented: each line is an
error as it was raised, with the desk that raised it, when, and how often.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
JOURNAL = Path(__file__).resolve().parent / "researchNotes" / "errors.json"
LIMIT = 500                 # the journal keeps this many distinct errors; older ones fall off
REPEAT_WINDOW = 600         # seconds; the same error from the same desk inside this window is one entry, repeated
REPORT_HOUR = 17            # the daily report is compiled after 5:05pm ET
REPORT_MINUTE = 5
BOOK_KEY = "last_error_report"

SYSTEMS = "Systems Administrator"
PORTFOLIO_MANAGER = "Portfolio Manager"
REPORT_KEY = "systems_report"
SUBJECT = "Futures desk error report"

_lock = threading.RLock()


def _now() -> datetime:
    return datetime.now(ET)


def _stamp(when: datetime | None = None) -> str:
    return (when or _now()).isoformat(timespec="seconds")


def _shown(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone(ET).strftime("%Y-%m-%d %I:%M %p ET")
    except (TypeError, ValueError):
        return str(stamp or "")


def _parse(stamp: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=ET)


def _load() -> dict:
    try:
        data = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    rows = data.get("errors")
    data["kind"] = "error_journal"
    data["errors"] = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not isinstance(data.get("last_report"), dict):
        data["last_report"] = {}
    return data


def _save(data: dict) -> None:
    data["errors"] = data["errors"][-LIMIT:]
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass


def _signature(text: str) -> str:
    """The message without its numbers and paths, so one error raised with different figures groups as one."""
    folded = re.sub(r"[A-Za-z]:\\[^\s]+|/[^\s]+", "<path>", text)
    folded = re.sub(r"\d+(\.\d+)?", "#", folded)
    return re.sub(r"\s+", " ", folded).strip().lower()[:300]


def record(desk: str, text: str, *, source: str = "board", detail: str = "") -> dict | None:
    """Write one error down. The same desk raising the same message again inside the window counts as a repeat."""
    text = " ".join(str(text or "").split()).strip()
    desk = str(desk or "").strip() or "Desk"
    if not text:
        return None
    text = text[:600]
    with _lock:
        data = _load()
        rows = data["errors"]
        now = _now()
        for row in reversed(rows[-50:]):
            if row.get("desk") != desk or row.get("text") != text:
                continue
            last = _parse(row.get("last") or row.get("time"))
            if last is not None and (now - last).total_seconds() <= REPEAT_WINDOW:
                row["repeats"] = int(row.get("repeats") or 1) + 1
                row["last"] = _stamp(now)
                _save(data)
                return dict(row)
            break
        entry = {
            "time": _stamp(now),
            "last": _stamp(now),
            "desk": desk,
            "text": text,
            "source": str(source or "board"),
            "repeats": 1,
        }
        if detail:
            entry["detail"] = str(detail)[:600]
        rows.append(entry)
        _save(data)
        return dict(entry)


def journal_post(author: str, text: str, channel: str | None = None) -> None:
    """The board hook: an error posted on the board goes into the journal. It never raises."""
    try:
        record(author, text, source="board")
    except Exception:
        pass


def entries(since: datetime | str | None = None) -> list[dict]:
    """The errors on record, oldest first, from ``since`` on when given. A repeat inside the window counts by its last time."""
    with _lock:
        rows = _load()["errors"]
    if since is None:
        return [dict(row) for row in rows]
    start = _parse(since) if isinstance(since, str) else since
    if start is None:
        return [dict(row) for row in rows]
    kept = []
    for row in rows:
        last = _parse(row.get("last") or row.get("time"))
        if last is not None and last > start:
            kept.append(dict(row))
    return kept


def last_report() -> dict:
    with _lock:
        return dict(_load()["last_report"])


def _mark_reported(count: int, sent: bool, reason: str, to: str) -> None:
    with _lock:
        data = _load()
        data["last_report"] = {"time": _stamp(), "errors": int(count), "sent": bool(sent), "reason": str(reason or ""), "to": str(to or "")}
        _save(data)


def report(rows: list[dict], since: str | None = None, *, written_by: str = SYSTEMS) -> str:
    """The report text, by desk, with the errors that keep coming back. Every line is an error as it was raised."""
    header = f"Workflow error report for the futures desk. Compiled by the {written_by} {_now().strftime('%Y-%m-%d %I:%M %p ET')}."
    window = f"Errors since the last report ({_shown(since)})." if since else "Every error on record; no error report has been sent before."
    if not rows:
        return f"{header}\n{window}\nNo errors were recorded in the workflow in that time."
    total = sum(int(row.get("repeats") or 1) for row in rows)
    by_desk: dict[str, list[dict]] = {}
    for row in rows:
        by_desk.setdefault(str(row.get("desk") or "Desk"), []).append(row)
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(_signature(str(row.get("text") or "")), []).append(row)
    lines = [
        header,
        window,
        f"{total} errors on {len(by_desk)} desks, {len(groups)} distinct messages.",
        "",
    ]
    for desk, items in sorted(by_desk.items(), key=lambda pair: -sum(int(row.get('repeats') or 1) for row in pair[1])):
        count = sum(int(row.get("repeats") or 1) for row in items)
        lines.append(f"{desk}: {count} error{'s' if count != 1 else ''}")
        for row in sorted(items, key=lambda row: str(row.get("last") or row.get("time")), reverse=True)[:12]:
            repeats = int(row.get("repeats") or 1)
            times = f"x{repeats}, last {_shown(row.get('last') or row.get('time'))}" if repeats > 1 else _shown(row.get("time"))
            source = str(row.get("source") or "board")
            lines.append(f"  - [{times}; {source}] {row.get('text')}")
            if row.get("detail"):
                lines.append(f"      {row['detail']}")
        if len(items) > 12:
            lines.append(f"  and {len(items) - 12} more on this desk.")
    recurring = [
        (key, items) for key, items in groups.items() if sum(int(row.get("repeats") or 1) for row in items) > 1
    ]
    if recurring:
        lines.append("")
        lines.append("Recurring errors, most frequent first:")
        recurring.sort(key=lambda pair: -sum(int(row.get("repeats") or 1) for row in pair[1]))
        for _key, items in recurring[:8]:
            count = sum(int(row.get("repeats") or 1) for row in items)
            desks = sorted({str(row.get("desk") or "Desk") for row in items})
            lines.append(f"  - {count} times on {', '.join(desks)}: {items[-1].get('text')}")
    lines.append("")
    lines.append(
        "Each line is the error as the desk raised it, with the time and the source: board (posted by a desk), engine (a backtest "
        "that did not finish), mail (an email that did not go out), or request (a page request the server could not serve)."
    )
    return "\n".join(lines)


def summary_lines() -> list[str]:
    """Plain facts about the error journal for the desks and the chat."""
    last = last_report()
    since = last.get("time")
    rows = entries(since)
    total = sum(int(row.get("repeats") or 1) for row in rows)
    lines = []
    if since:
        sent = "emailed" if last.get("sent") else "not emailed"
        lines.append(
            f"The last error report was compiled {_shown(since)} with {last.get('errors', 0)} errors and was {sent}"
            + (f" to {last.get('to')}." if last.get("sent") and last.get("to") else (f": {last.get('reason')}" if not last.get("sent") and last.get("reason") else "."))
        )
    else:
        lines.append("No error report has been sent yet.")
    window = "since then" if since else "so far"
    if rows:
        desks = sorted({str(row.get("desk") or "Desk") for row in rows})
        lines.append(f"Errors recorded {window}: {total} on {len(desks)} desks ({', '.join(desks)}). The latest:")
        for row in rows[-5:]:
            repeats = int(row.get("repeats") or 1)
            lines.append(f"  {_shown(row.get('last') or row.get('time'))} {row.get('desk')}: {row.get('text')}" + (f" (x{repeats})" if repeats > 1 else ""))
    else:
        lines.append(f"No workflow error has been recorded {window}.")
    lines.append(
        "The Systems Administrator compiles the error report from the journal and the Portfolio Manager emails it at 5:05pm ET on days "
        "with new errors, or when asked to send the error report."
    )
    return lines


def deliver(board, *, force: bool = False) -> dict:
    """The Systems Administrator compiles the report and hands it to the Portfolio Manager, who emails it.

    Returns {"sent", "reason", "errors", "report", "to"}. With nothing new since the last report and no
    ``force``, nothing is emailed and the Systems Administrator says so on the board.
    """
    last = last_report()
    since = last.get("time")
    rows = entries(since)
    count = sum(int(row.get("repeats") or 1) for row in rows)
    if not rows and not force:
        text = f"No workflow errors since the last error report ({_shown(since)}). Nothing to email." if since else "No workflow error has been recorded. Nothing to email."
        if board is not None:
            board.post(SYSTEMS, text, kind="log", channel=SYSTEMS)
        return {"sent": False, "reason": text, "errors": 0, "report": "", "to": ""}
    text = report(rows, since)
    if board is not None:
        board.set_report(REPORT_KEY, text)
        board.post(SYSTEMS, text, kind="report", channel=SYSTEMS)
        board.post(
            SYSTEMS,
            f"Portfolio Manager, the workflow error report is ready: {count} error{'s' if count != 1 else ''} "
            + (f"since {_shown(since)}. " if since else "on record. ")
            + "Please email it.",
            kind="speech",
            channel=PORTFOLIO_MANAGER,
        )
    from futuresfund.mailer import send_report

    try:
        result = send_report(text, subject=f"{SUBJECT}: {count} error{'s' if count != 1 else ''}")
    except Exception as exc:  # the mailer records its own failures; this one is ours
        result = {"sent": False, "reason": f"The error report was not emailed: {exc}", "to": ""}
    sent = bool(result.get("sent"))
    to = str(result.get("to") or "")
    reason = str(result.get("reason") or "")
    _mark_reported(count, sent, reason, to)
    if board is not None:
        if sent:
            board.post(PORTFOLIO_MANAGER, f"Emailed the workflow error report from the Systems Administrator ({count} errors) to {to}.", kind="report", channel=PORTFOLIO_MANAGER)
        else:
            board.post(
                PORTFOLIO_MANAGER,
                f"The workflow error report ({count} errors) was not emailed: {reason} The report is on the Systems Administrator's channel.",
                kind="report",
                channel=PORTFOLIO_MANAGER,
            )
    return {"sent": sent, "reason": reason, "errors": count, "report": text, "to": to}


def due(now: datetime | None = None, book: dict | None = None) -> bool:
    """Is it time for today's report? After 5:05pm ET, before 6:00pm, and not yet done today."""
    et = now.astimezone(ET) if now is not None and now.tzinfo else (now.replace(tzinfo=ET) if now is not None else _now())
    if et.hour != REPORT_HOUR or et.minute < REPORT_MINUTE:
        return False
    today = et.strftime("%Y-%m-%d")
    return not str((book or {}).get(BOOK_KEY, "")).startswith(today)


def scheduled(board, now: datetime | None = None) -> dict | None:
    """Called by the desk clock. Compiles and emails today's report once, when it is due."""
    from futuresfund.book import load, save

    book = load()
    if not due(now, book):
        return None
    et = (now or _now()).astimezone(ET)
    book[BOOK_KEY] = et.strftime("%Y-%m-%d %I:%M %p ET")
    save(book)
    return deliver(board)
