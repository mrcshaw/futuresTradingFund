"""The portfolio manager writes the fund report and emails it when SMTP is configured."""

from __future__ import annotations

import json
import os
import smtplib
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

MAIL_DIR = Path(__file__).resolve().parent / "researchNotes" / "mail"
MAIL_LOG = MAIL_DIR / "_sent.json"
_log_lock = threading.Lock()


def factual_report(book: dict, trials: list | None = None) -> str:
    """Numbers come from the book and the engine file. Nothing here is estimated."""
    from futuresfund.session import meeting_note

    lines = [
        "Futures desk report from the portfolio manager.",
        meeting_note(),
    ]
    rules = book.get("account_rules") or {}
    if isinstance(rules, dict) and rules.get("size"):
        kind = "trailing" if rules.get("trailing", True) else "fixed"
        lines.append(
            f"Account {rules.get('account') or 'unnamed'}: size {rules.get('size')}, "
            f"profit target {rules.get('profit_target')}, {kind} drawdown {rules.get('max_drawdown')}, "
            f"max contracts {rules.get('max_contracts')}."
        )
    else:
        lines.append("No account rules have been entered.")
    paper = book.get("paper") or {}
    if isinstance(paper, dict) and paper:
        lines.append(
            f"Paper book: realized {paper.get('realized')}, peak {paper.get('peak')}."
        )
    positions = book.get("positions") or []
    if isinstance(positions, dict):
        positions = list(positions.values())
    if not positions:
        lines.append("No trade is open.")
    for position in positions:
        lines.append(
            f"Open trade: {position.get('account')} {position.get('instrument')} "
            f"{position.get('contracts')} from {position.get('average_price')}."
        )
    active = book.get("active_strategies") or []
    if active:
        lines.append("Strategies on the floor: " + "; ".join(
            f"{item.get('title') or 'Untitled'} on {item.get('account') or 'paper'}" for item in active
        ) + ".")
    else:
        lines.append("No strategy is saved on the floor.")
    strategies = book.get("strategies") or []
    if not strategies:
        lines.append("No strategies are on the book.")
    for item in strategies:
        backtest = item.get("backtest") or {}
        state = "active" if item.get("active") else "not active"
        if item.get("proven") and item.get("accepted"):
            risk = "passed risk"
        else:
            risk = "has not passed risk"
        lines.append(
            f"{item.get('title') or 'Untitled'}: {state}, {risk}, "
            f"profit {backtest.get('net_profit', 'not recorded')}, "
            f"drawdown {backtest.get('max_drawdown', 'not recorded')}, "
            f"trades {backtest.get('trades', 'not recorded')}."
        )
    orders = book.get("orders") or []
    since = str(book.get("previous_meeting") or "")
    since_orders = [order for order in orders if not since or str(order.get("time") or "") >= since]
    lines.append(f"Orders on the book: {len(orders)}.")
    if since:
        lines.append(f"Orders since the last meeting ({since}): {len(since_orders)}.")
    else:
        lines.append("No earlier meeting time is stored, so the latest orders are listed.")
    shown = since_orders if since else orders
    if not shown:
        lines.append("No orders were sent since the last meeting.")
    for order in shown[-8:]:
        lines.append(
            f"Order {order.get('time', 'time not recorded')}: "
            f"{order.get('side') or 'side not recorded'} {order.get('instrument', '')} {order.get('reason', '')}".rstrip()
        )
    rows = trials or []
    lines.append(f"Engine tests recorded: {len(rows)}.")
    for row in rows[-5:]:
        lines.append(
            f"Test {row.get('title') or row.get('name') or 'Untitled'}: "
            f"profit {row.get('net_profit', 'not recorded')}, "
            f"drawdown {row.get('max_drawdown', 'not recorded')}, "
            f"trades {row.get('trades', 'not recorded')}, "
            f"passed {bool(row.get('proven'))}."
        )
    rules = book.get("headquarters_rules") or {}
    if isinstance(rules, dict) and rules.get("text"):
        lines.append("Prop-account rules recorded for headquarters:")
        lines.append(str(rules["text"]))
    return "\n".join(lines)


def write_result(facts: str, writer=None) -> str:
    """The portfolio manager writes this result from the desk. A model failure keeps the facts.

    The facts say whether the result is on the strategy library. A reply that files a result the
    facts left off the shelf is discarded, and the facts are sent instead.
    """
    if writer is None:
        from futuresfund.llm import complete

        writer = lambda prompt: complete(prompt, agent="Portfolio Manager", fresh=True)
    not_filed = "on the strategy library: no" in (facts or "").lower()
    text = writer(
        "You are the portfolio manager. One chart just finished on this desk. "
        "Write a new email about this result. Read the facts below. They were taken from the application just now. "
        "Include the strategy, the chart, the profit, the drawdown, the trades, the trades per day, and the profit ratio. "
        "The profit ratio is the average winning trade divided by the average losing trade. "
        "The acceptance line in the facts is the only acceptance bar on this desk. It is the strategy library page: the profit, the profit factor, the win rate, and the trade count saved there. "
        "Judge this run against those four numbers. Quote them. Do not apply any other profit, profit factor, win rate, or trade count. "
        "Say it is on the strategy library only when the facts say it is. "
        "When the facts say it is not on the library, say it was not filed and give only the reason written there. "
        "Do not use a template or numbered sections. "
        "Do not invent a profit, a drawdown, a trade count, or a filing.\n\n"
        f"{facts}"
    )
    if not text or "could not answer" in text.lower():
        return facts
    if not_filed and _claims_a_filing(text):
        return facts
    return text.strip()


def _claims_a_filing(text: str) -> bool:
    """A line that says the result was filed, rather than that it was not."""
    for line in (text or "").splitlines():
        lowered = line.lower()
        if "not filed" in lowered or "not on the" in lowered or "was not added" in lowered or "left off" in lowered:
            continue
        if "filed in the" in lowered or "stored in the strategy library" in lowered:
            return True
        if "strategy library" in lowered and " is in the " in f" {lowered}":
            return True
    return False


def write_report(facts: str, writer=None) -> str:
    """The portfolio manager writes the report. A model failure keeps the facts."""
    if writer is None:
        from futuresfund.llm import complete

        writer = lambda prompt: complete(prompt, agent="Portfolio Manager")
    text = writer(
        "You are the portfolio manager. Write a short report of what this futures fund has been doing. "
        "Start with the account and whether any trade was sent since the last meeting. "
        "If no trade is open and no order was sent, say that. A research backtest is not a live trade. "
        "Use only the facts below. Do not invent a profit, a drawdown, or a trade count.\n"
        f"{facts}"
    )
    if not text or "could not answer" in text:
        return facts
    return text


REPORT_ADDRESS = "thefutureoffuturestrading@gmail.com"


def report_recipients() -> list[str]:
    raw = os.environ.get("REPORT_TO", "").strip() or REPORT_ADDRESS
    sender = os.environ.get("SMTP_USER", "").strip().lower()
    recipients = []
    for part in raw.split(","):
        address = part.strip()
        if not address or address.lower() == sender:
            continue
        recipients.append(address)
    return recipients or [REPORT_ADDRESS]


def _now() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")


def mail_log() -> list[dict]:
    """Every email attempt the desk has made, oldest first."""
    try:
        rows = json.loads(MAIL_LOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _record(subject: str, result: dict) -> None:
    entry = {
        "time": _now(),
        "subject": subject,
        "sent": bool(result.get("sent")),
        "to": result.get("to") or ", ".join(report_recipients()),
        "reason": result.get("reason") or "",
    }
    with _log_lock:
        rows = mail_log()
        rows.append(entry)
        try:
            MAIL_DIR.mkdir(parents=True, exist_ok=True)
            MAIL_LOG.write_text(json.dumps(rows[-200:], indent=1), encoding="utf-8")
        except OSError:
            pass
    if not entry["sent"] and "SMTP_PASSWORD is empty" not in entry["reason"]:
        # An email the desk tried to send and could not is a workflow error. A desk with no mail set up is not.
        try:
            from futuresfund.errors import record

            record("Portfolio Manager", f"Email not sent: {subject}. {entry['reason']}", source="mail")
        except Exception:
            pass


def _smtp_settings() -> tuple[str, int, str, str, str]:
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or "587")
    except ValueError:
        port = 587
    user = os.environ.get("SMTP_USER", "").strip()
    sender = os.environ.get("REPORT_FROM", "").strip() or user
    return host, port, user, os.environ.get("SMTP_PASSWORD", ""), sender


def check_mail() -> dict:
    """Log in to the mail server without sending anything, so a question about email gets a real answer."""
    host, port, user, password, sender = _smtp_settings()
    if not password.strip():
        return {"ok": False, "reason": "SMTP_PASSWORD is empty."}
    if not sender:
        return {"ok": False, "reason": "Email needs REPORT_FROM or SMTP_USER."}
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if user:
                smtp.login(user, password)
        return {"ok": True, "reason": f"{host} accepted the login for {sender} just now."}
    except (OSError, smtplib.SMTPException) as exc:
        return {"ok": False, "reason": f"{host} did not accept the login: {exc}"}


def mail_status(*, probe: bool = False) -> list[str]:
    """Plain facts about report email for the desks. No password is shown."""
    from futuresfund.config import load_env

    load_env()
    _, _, user, password, sender = _smtp_settings()
    recipients = ", ".join(report_recipients())
    lines = []
    if not password.strip():
        lines.append(
            f"Report email is not configured: SMTP_PASSWORD is empty. Reports are addressed to {recipients} and are only saved on the desk."
        )
    else:
        lines.append(f"Report email is configured. Reports go from {sender or user} to {recipients}.")
    lines.append(
        "A research report is emailed when a chart developer finishes a script on one chart, after all of that chart's attempts. "
        "The meeting report is emailed after a strategy meeting or when someone asks the Portfolio Manager to send the report. "
        f"Every report is also saved in researchNotes/mail ({len(list(MAIL_DIR.glob('*.txt'))) if MAIL_DIR.is_dir() else 0} saved)."
    )
    rows = mail_log()
    if rows:
        sent = [row for row in rows if row.get("sent")]
        failed = [row for row in rows if not row.get("sent")]
        lines.append(f"Email attempts recorded: {len(rows)}, {len(sent)} sent, {len(failed)} not sent.")
        if sent:
            last = sent[-1]
            lines.append(f"Last email sent {last['time']}: {last['subject']} to {last['to']}.")
        if failed:
            last = failed[-1]
            lines.append(f"Last failed attempt {last['time']}: {last['subject']}. {last['reason']}")
    else:
        saved = sorted(MAIL_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime) if MAIL_DIR.is_dir() else []
        if saved:
            stamp = datetime.fromtimestamp(saved[-1].stat().st_mtime, ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p ET")
            lines.append(f"No email attempt has been logged yet. The last report saved on the desk was {saved[-1].stem} at {stamp}.")
        else:
            lines.append("No email attempt has been logged and no report has been saved yet.")
    if probe:
        check = check_mail()
        lines.append(check["reason"] if check["ok"] else f"Mail check: {check['reason']}")
    return lines


def _with_phone(body: str) -> str:
    """Every report carries the current phone address. A report that already names it is left as written."""
    try:
        from futuresfund.tunnel import view_line
    except Exception:
        return body
    line = view_line()
    if not line:
        return body
    url = line.rsplit(" ", 1)[-1]
    if url in (body or ""):
        return body
    return (body or "").rstrip() + "\n\n" + line


def send_report(body: str, subject: str = "Futures desk report") -> dict:
    """Send the report. A missing password does not stall the strategy queue."""
    result = _send_report(body, subject)
    _record(subject, result)
    return result


def _send_report(body: str, subject: str) -> dict:
    host, port, user, password, sender = _smtp_settings()
    recipients = report_recipients()
    if not password.strip():
        return {
            "sent": False,
            "reason": f"SMTP_PASSWORD is empty, so the report was kept on the desk and not sent to {', '.join(recipients)}.",
        }
    if not sender:
        return {"sent": False, "reason": "Email needs REPORT_FROM or SMTP_USER."}
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(_with_phone(body))
    last_error = "The report was not emailed."
    for attempt in range(3):
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if user:
                    smtp.login(user, password)
                refused = smtp.send_message(message)
            if refused:
                last_error = "Gmail refused " + ", ".join(refused)
                time.sleep(2)
                continue
            return {"sent": True, "reason": "sent", "to": ", ".join(recipients)}
        except (OSError, smtplib.SMTPException) as exc:
            last_error = f"The report was not emailed: {exc}"
            time.sleep(2)
    return {"sent": False, "reason": last_error}


def trade_email(signal: dict, plan: dict, result: dict, held: int, book: dict) -> tuple[str, str]:
    """A short summary the portfolio manager can send after a trade action."""
    side = str(plan.get("side") or "").upper() or "ORDER"
    action = plan.get("action") or ""
    target = plan.get("target")
    qty = signal.get("qty")
    instrument = signal.get("instrument") or ""
    account = signal.get("account") or ""
    price = signal.get("price")
    closing = action == "close" or target == 0
    if closing:
        verb = "Closed"
    elif int(held or 0) == 0:
        verb = "Opened"
    else:
        verb = "Changed"
    subject = f"Trade {verb.lower()}: {side} {qty} {instrument}"
    lines = [
        "Portfolio manager trade summary.",
        f"{verb} {side} {qty} {instrument} on {account} at {price}.",
        "The order was sent." if result.get("sent") else "The order was not sent.",
    ]
    if result.get("dry_run"):
        lines.append("Dry run is on. CrossTrade did not receive the order.")
    strategy = signal.get("strategy")
    if strategy:
        lines.append(f"Strategy: {strategy}.")
    if closing:
        last = None
        for trade in reversed(book.get("trades") or []):
            if trade.get("instrument") == instrument and trade.get("account") in {None, "", account}:
                last = trade
                break
        if last:
            lines.append(
                f"Round trip: {last.get('side')} {last.get('qty')} from {last.get('entry')} "
                f"to {last.get('exit')}. P&L {last.get('pnl')}."
            )
    position = (book.get("positions") or {}).get(f"{account}|{instrument}")
    if position and position.get("contracts"):
        lines.append(f"Position now: {position.get('contracts')} from {position.get('average_price')}.")
    else:
        lines.append("Position now: flat.")
    from futuresfund.book import paper_status

    for row in paper_status(book).get("accounts") or []:
        if row.get("account") == account:
            lines.append(
                f"Equity {row.get('equity')}. P&L {row.get('pnl')}. Drawdown {row.get('drawdown')}."
            )
    return subject, "\n".join(lines)


def notify_trade(signal: dict, plan: dict, result: dict, held: int, book: dict) -> dict:
    """Email the trade summary. The sender is never a recipient."""
    subject, body = trade_email(signal, plan, result, held, book)
    sent = send_report(body, subject)
    sent["subject"] = subject
    sent["body"] = body
    return sent


def deliver_report() -> str:
    """Write the report from the saved book and email it when the desk can."""
    from futuresfund.book import load
    from futuresfund.lab import load_report

    book = load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    payload = dict(strategy)
    payload["orders"] = book.get("orders") or []
    payload["headquarters_rules"] = book.get("headquarters_rules")
    payload["account_rules"] = book.get("account_rules")
    payload["positions"] = list((book.get("positions") or {}).values())
    payload["paper"] = book.get("paper")
    payload["active_strategies"] = book.get("active_strategies") or []
    payload["previous_meeting"] = book.get("previous_meeting") or ""
    trials = (load_report() or {}).get("trials") or []
    written = write_report(factual_report(payload, trials))
    result = send_report(written)
    tail = (
        f"Emailed to {result['to']}."
        if result.get("sent")
        else result["reason"]
    )
    return f"{written}\n\n{tail}"
