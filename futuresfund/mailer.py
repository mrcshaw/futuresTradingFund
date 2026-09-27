"""The portfolio manager writes the fund report and emails it when SMTP is configured."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def factual_report(book: dict, trials: list | None = None) -> str:
    """Numbers come from the book and the engine file. Nothing here is estimated."""
    from futuresfund.session import meeting_note

    lines = [
        "Futures desk report from the portfolio manager.",
        meeting_note(),
    ]
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
    lines.append(f"Orders on the book: {len(orders)}.")
    for order in orders[-5:]:
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
    return "\n".join(lines)


def write_report(facts: str, writer=None) -> str:
    """The portfolio manager writes the report. A model failure keeps the facts."""
    if writer is None:
        from futuresfund.llm import complete

        writer = complete
    text = writer(
        "You are the portfolio manager. Write a short report of what this futures fund has been doing. "
        "Use only the facts below. Do not invent a profit, a drawdown, or a trade count.\n"
        f"{facts}"
    )
    if not text or "could not answer" in text:
        return facts
    return text


REPORT_ADDRESS = "thefutureoffuturestrading@gmail.com"


def report_recipients() -> list[str]:
    raw = os.environ.get("REPORT_TO", "").strip() or REPORT_ADDRESS
    return [part.strip() for part in raw.split(",") if part.strip()]


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
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        return {"sent": False, "reason": f"The report was not emailed: {exc}"}
    return {"sent": True, "reason": "sent", "to": ", ".join(recipients)}


def deliver_report() -> str:
    """Write the report from the saved book and email it when the desk can."""
    from futuresfund.book import load
    from futuresfund.lab import load_report

    book = load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    payload = dict(strategy)
    payload["orders"] = book.get("orders") or []
    trials = (load_report() or {}).get("trials") or []
    written = write_report(factual_report(payload, trials))
    result = send_report(written)
    tail = (
        f"Emailed to {result['to']}."
        if result.get("sent")
        else result["reason"]
    )
    return f"{written}\n\n{tail}"
