"""The trading analyst asks NinjaTrader whether the webhook order filled."""

from __future__ import annotations

import re
import threading

from futuresfund import ninjatrader
from futuresfund.contracts import root_of

_FILLED = {"filled"}
_REJECTED = {"rejected", "cancelled", "canceled"}


def request_confirmation(board, signal: dict, result: dict, *, closing: bool) -> dict:
    """Post the confirmation. A sent order is checked off the alert thread."""
    if not result.get("sent"):
        reason = (
            "DRY_RUN is on. The order was not sent, so NinjaTrader has nothing to confirm."
            if result.get("dry_run")
            else "The order was not sent, so NinjaTrader has nothing to confirm."
        )
        board.post("Trading Analyst", reason, kind="report", channel="Trading Analyst")
        return {"confirmed": False, "status": "not_sent", "reason": reason}
    reason = "The trading analyst is asking NinjaTrader whether this order filled."
    board.post("Trading Analyst", reason, kind="report", channel="Trading Analyst")
    threading.Thread(
        target=_confirm_later,
        args=(board, signal, result, closing),
        daemon=True,
        name="trade-confirmation",
    ).start()
    return {"confirmed": False, "status": "pending", "reason": reason}


def judge(signal: dict, result: dict, executions: list[dict], status: str | None = None, *, closing: bool) -> dict:
    """Decide confirmed, rejected, or still open from NinjaTrader's own records."""
    if not result.get("sent"):
        return {
            "confirmed": False,
            "status": "not_sent",
            "reason": "The order was not sent, so NinjaTrader has nothing to confirm.",
        }
    kind = _status_kind(status)
    if kind == "confirmed":
        return {"confirmed": True, "status": "confirmed", "reason": f"NinjaTrader reports the order {status}."}
    if kind == "rejected":
        return {
            "confirmed": False,
            "status": "rejected",
            "reason": f"NinjaTrader refused the order: {status}. The trade is not confirmed.",
        }
    fill = _match_execution(signal, executions, closing=closing, order_id=_order_id(result))
    if fill is not None:
        price = fill.get("price")
        quantity = fill.get("quantity")
        return {
            "confirmed": True,
            "status": "confirmed",
            "reason": f"NinjaTrader filled {quantity} at {price}. The trade is confirmed.",
        }
    if kind == "pending":
        return {
            "confirmed": False,
            "status": "pending",
            "reason": f"NinjaTrader has the order as {status}. That is not a fill.",
        }
    return {
        "confirmed": False,
        "status": "unconfirmed",
        "reason": "NinjaTrader has not reported a fill for this order.",
    }


def _confirm_later(board, signal: dict, result: dict, closing: bool) -> None:
    try:
        executions = ninjatrader.list_executions(signal["account"])
        rows = executions.get("executions") or ninjatrader.cached_executions(signal["account"])
        status = None
        order_id = _order_id(result)
        if order_id:
            looked = ninjatrader.order_status(signal["account"], order_id)
            status = looked.get("status")
        if not executions.get("ok") and not rows:
            reason = executions.get("reason") or "NinjaTrader is not connected, so the fill is not confirmed."
            board.post("Trading Analyst", reason, kind="report", channel="Trading Analyst")
            return
        verdict = judge(signal, result, rows, status, closing=closing)
    except Exception:
        verdict = {
            "confirmed": False,
            "status": "unconfirmed",
            "reason": "NinjaTrader confirmation failed. The fill is not confirmed.",
        }
    board.post("Trading Analyst", verdict["reason"], kind="report", channel="Trading Analyst")


def _match_execution(signal: dict, executions: list[dict], *, closing: bool, order_id: str | None) -> dict | None:
    try:
        root = root_of(signal["instrument"])
    except ValueError:
        return None
    account = signal["account"]
    ranked = sorted(executions, key=lambda row: int(row.get("epoch") or 0), reverse=True)
    for row in ranked:
        if order_id and str(row.get("orderId") or "") == order_id:
            return row
        if row.get("account") and row.get("account") != account:
            continue
        try:
            if root_of(str(row.get("instrument") or "")) != root:
                continue
        except ValueError:
            continue
        if closing and not row.get("isExit"):
            continue
        if not closing and row.get("isExit"):
            continue
        return row
    return None


def _status_kind(status: str | None) -> str:
    text = str(status or "").strip().lower()
    if text in _FILLED:
        return "confirmed"
    if text in _REJECTED:
        return "rejected"
    if text:
        return "pending"
    return "unconfirmed"


def _order_id(result: dict) -> str | None:
    body = str(result.get("body") or "")
    match = re.search(r'"orderId"\s*:\s*"([^"]+)"', body) or re.search(r"orderId=([^;\s]+)", body)
    if not match:
        return None
    return match.group(1)
