"""The portfolio manager writes the fund report and emails it when SMTP is configured."""

from __future__ import annotations

import os
import smtplib
import time
from email.message import EmailMessage


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


def send_report(body: str, subject: str = "Futures desk report") -> dict:
    """Send the report. A missing password does not stall the strategy queue."""
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    recipients = report_recipients()
    if not os.environ.get("SMTP_PASSWORD", "").strip():
        return {
            "sent": False,
            "reason": f"SMTP_PASSWORD is empty, so the report was kept on the desk and not sent to {', '.join(recipients)}.",
        }
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or "587")
    except ValueError:
        port = 587
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("REPORT_FROM", "").strip() or user
    if not sender:
        return {"sent": False, "reason": "Email needs REPORT_FROM or SMTP_USER."}
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)
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
