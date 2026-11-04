"""Reports from the research manager to the futures desk Gmail account.

The transport is replaceable so a test can record the letter without opening SMTP.
"""

from __future__ import annotations

from pathlib import Path


ADDRESS = "thefutureoffuturestrading@gmail.com"


class MailService:
    def __init__(self, transport=None, phone=None) -> None:
        self.transport = transport
        self.phone = phone or (lambda: "")

    def send_success(self, strategy: dict) -> dict:
        try:
            url = str(self.phone() or "")
        except Exception:
            url = ""
        message = {
            "to": ADDRESS,
            "subject": f"Research manager: {strategy.get('script') or 'strategy'} passed",
            "body": report_body(strategy, url),
        }
        if self.transport is not None:
            return self.transport(message)
        return smtp_send(message)


def report_body(strategy: dict, url: str) -> str:
    lines = []
    table = strategy.get("table")
    if table:
        lines.append(f"Added to the {table} table.")
    lines.extend([
        f"{strategy.get('script') or 'A strategy'} passed on {strategy.get('instrument') or ''} {strategy.get('timeframe') or ''}.".strip(),
        f"File: {strategy.get('file') or ''}",
        f"Change: {strategy.get('change') or ''}",
        f"Justification: {strategy.get('justification') or ''}",
        f"Days: {strategy.get('days')}",
        f"Success rate: {_percent(strategy.get('success_rate'))}",
        f"Win percentage: {_percent(strategy.get('win_percentage'))}",
        f"Profit: {_money(strategy.get('profit'))}",
        f"Profit goal: {_money(strategy.get('profit_goal'))}" if strategy.get("profit_goal") else "",
        f"Worst day: {strategy.get('worst_day')}",
        f"Trades: {strategy.get('trade_count')}",
        f"Attempt: {strategy.get('attempt')}",
        "",
        f"Open the application: {url}" if url else "The phone address is not up yet.",
        f"Open the research department on your phone: {url}" if url else "",
    ])
    return "\n".join(line for line in lines if line is not None)


def smtp_send(message: dict, connect=None, load: bool = True) -> dict:
    """Send one letter. An empty password is recorded and does not open a connection."""
    import os
    import smtplib
    from email.message import EmailMessage

    if load:
        _load_desk_env()
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or "587")
    except ValueError:
        port = 587
    user = os.environ.get("SMTP_USER", "").strip()
    sender = os.environ.get("REPORT_FROM", "").strip() or user
    password = os.environ.get("SMTP_PASSWORD", "")
    if not str(password).strip():
        return {
            "sent": False,
            "to": ADDRESS,
            "reason": f"SMTP_PASSWORD is empty, so the report was kept and not sent to {ADDRESS}.",
        }
    if not sender:
        return {"sent": False, "to": ADDRESS, "reason": "Email needs REPORT_FROM or SMTP_USER."}
    letter = EmailMessage()
    letter["From"] = sender
    letter["To"] = ADDRESS
    letter["Subject"] = message.get("subject") or "Research manager report"
    letter.set_content(message.get("body") or "")
    opener = connect or smtplib.SMTP
    try:
        with opener(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(letter)
    except (OSError, smtplib.SMTPException) as exc:
        return {"sent": False, "to": ADDRESS, "reason": f"{host} did not accept the report: {exc}"}
    return {"sent": True, "to": ADDRESS, "reason": f"Sent to {ADDRESS}."}


def _load_desk_env() -> None:
    import sys

    fund = str(Path(__file__).resolve().parents[2])
    if fund not in sys.path:
        sys.path.insert(0, fund)
    from futuresfund.config import load_env

    load_env()


def _money(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return f"${number:,.0f}"
    return f"${number:,.2f}"


def _percent(value) -> str:
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return ""
