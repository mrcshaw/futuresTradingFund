"""Prop-firm mail is handled by the systems administrator and read into the 5:00pm meeting."""

from __future__ import annotations

import re
from datetime import datetime

from futuresfund.board import Board
from futuresfund.book import load, save
from futuresfund.prop_rules import describe
from futuresfund.strategy import get_strategy


def parse_account_update(text: str) -> dict:
    """Pull only figures the prop firm actually stated. A bare number is ignored."""
    found = {}
    daily = _labeled_money(text, r"daily loss")
    if daily is not None:
        found["daily_loss_limit"] = daily
    trail = _labeled_money(text, r"(?:trailing drawdown|drawdown limit)")
    if trail is not None:
        found["max_drawdown"] = trail
    contracts = re.search(r"max(?:imum)? contracts(?:\s+is|\s+of|\s*[:=])?\s*([0-9]+)", text, re.I)
    if contracts:
        found["max_contracts"] = int(contracts.group(1))
    account = re.search(r"\b(APEX[0-9A-Z]+|PA-[A-Z0-9-]+)\b", text)
    if account:
        found["account"] = account.group(1)
    return found


def accept_mail(board: Board, payload: dict) -> dict:
    """Store one inbound prop-firm email and hand it to compliance."""
    sender = str(payload.get("from") or payload.get("sender") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    text = str(payload.get("text") or payload.get("body") or "").strip()
    if not subject and not text:
        return {"ok": False, "reason": "The email needs a subject or a body."}
    parsed = parse_account_update(f"{subject}\n{text}")
    book = load()
    notice = {
        "time": _now(),
        "sender": sender,
        "subject": subject,
        "text": text[:4000],
        "parsed": parsed,
    }
    book.setdefault("notices", []).append(notice)
    book["notices"] = book["notices"][-50:]
    if parsed:
        updates = book.setdefault("prop_updates", {})
        updates.update(parsed)
        updates["updated"] = notice["time"]
    save(book)
    summary = _mail_summary(sender, subject, parsed)
    board.post("Systems Administrator", summary, kind="report", channel="Systems Administrator")
    board.post(
        "Compliance & Operations",
        "A prop-firm account update is on file. It will be enforced with the trailing drawdown and raised at the 5:00pm meeting.",
        kind="report",
        channel="Compliance & Operations",
    )
    return {"ok": True, "parsed": parsed, "reason": summary}


def rules_text(account_size: float | None = None) -> str:
    """The prop rules headquarters can keep. Dollar amounts come from the tier or from mail that stated them."""
    size = float(account_size if account_size is not None else _account_size())
    book = load()
    updates = book.get("prop_updates") or {}
    lines = [
        describe(size),
        "The systems administrator and compliance enforce these rules together.",
    ]
    if updates.get("daily_loss_limit") is not None:
        lines.append(f"The prop firm stated a daily loss limit of ${float(updates['daily_loss_limit']):,.0f}.")
    else:
        lines.append(
            "No prop-firm mail has stated a daily loss dollar amount. "
            "Compliance enforces the trailing drawdown and the contract cap."
        )
    if updates.get("max_drawdown") is not None:
        lines.append(f"The prop firm stated a trailing drawdown of ${float(updates['max_drawdown']):,.0f}.")
    if updates.get("max_contracts") is not None:
        lines.append(f"The prop firm stated a maximum of {int(updates['max_contracts'])} contracts.")
    notices = book.get("notices") or []
    if notices:
        last = notices[-1]
        lines.append(
            f"Latest prop-firm mail: {last.get('subject') or 'no subject'} from {last.get('sender') or 'unknown sender'}."
        )
    else:
        lines.append("No prop-firm mail has arrived.")
    lines.append("A breach can liquidate the account. Open trades are closed at the trailing drawdown and at 4:45pm ET.")
    return "\n".join(lines)


def brief_prop_accounts(board: Board) -> str:
    """The 5:00pm meeting. The portfolio manager records the rules on headquarters."""
    text = rules_text()
    board.post(
        "Systems Administrator",
        "Prop-account mail and the published rules are what compliance will enforce.\n" + text,
        kind="report",
        channel="Systems Administrator",
    )
    board.post(
        "Compliance & Operations",
        "These prop-account rules are in force with the systems administrator.\n" + text,
        kind="report",
        channel="Compliance & Operations",
    )
    mention = "The 5:00pm meeting records the prop-account rules for headquarters.\n" + text
    board.post("Portfolio Manager", mention, kind="decision", channel="headquarters")
    book = load()
    book["headquarters_rules"] = {"time": _now(), "text": mention}
    save(book)
    return mention


def _mail_summary(sender: str, subject: str, parsed: dict) -> str:
    who = sender or "an unknown sender"
    title = subject or "no subject"
    if not parsed:
        return (
            f"Prop-firm mail from {who}: {title}. "
            "It did not state a daily loss, a trailing drawdown, or a contract cap. "
            "The published account rules stay in force and will be raised at the 5:00pm meeting."
        )
    parts = []
    if parsed.get("daily_loss_limit") is not None:
        parts.append(f"daily loss ${parsed['daily_loss_limit']:,.0f}")
    if parsed.get("max_drawdown") is not None:
        parts.append(f"trailing drawdown ${parsed['max_drawdown']:,.0f}")
    if parsed.get("max_contracts") is not None:
        parts.append(f"max contracts {parsed['max_contracts']}")
    if parsed.get("account"):
        parts.append(f"account {parsed['account']}")
    stated = ", ".join(parts)
    return (
        f"Prop-firm mail from {who}: {title}. Stated {stated}. "
        "Compliance will enforce this with the trailing drawdown. It will be raised at the 5:00pm meeting."
    )


def _labeled_money(text: str, label: str) -> float | None:
    match = re.search(
        label + r"(?:\s+limit)?(?:\s+is|\s+of|\s*[:=])?\s*\$?\s*([0-9][0-9,]*(?:\.\d+)?)",
        text,
        re.I,
    )
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _account_size() -> float:
    strategy = get_strategy() or {}
    try:
        return float(strategy.get("account_size") or 50000)
    except (TypeError, ValueError):
        return 50000.0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
