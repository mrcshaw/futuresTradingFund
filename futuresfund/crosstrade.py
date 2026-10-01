"""Build a CrossTrade text payload and POST it. The secret key is added here, never taken from the alert."""

from __future__ import annotations

import urllib.error
import urllib.request

from futuresfund.config import crosstrade_key, crosstrade_url, dry_run, max_qty, prop_accounts

_BUY = {"Buy", "Overweight"}
_SELL = {"Sell", "Underweight"}


def trade_side(rating: str) -> str | None:
    """Buy and Overweight send a buy. Sell and Underweight send a sell. Hold and REVIEW send nothing."""
    if rating in _BUY:
        return "BUY"
    if rating in _SELL:
        return "SELL"
    return None


def execution_account(signal: dict) -> str:
    """The account named on the alert. It is sent even when it is not the stored name."""
    named = str(signal.get("account") or "").strip()
    if named:
        return named
    allowed = prop_accounts()
    if allowed:
        return sorted(allowed)[0]
    raise ValueError("The alert has no account.")


def build_payload(signal: dict, side: str) -> str:
    account = execution_account(signal)
    if int(signal["qty"]) > max_qty():
        raise ValueError(f"qty {signal['qty']} is above FUTURES_MAX_QTY ({max_qty()}).")
    key = crosstrade_key() or ("dry-run" if dry_run() else "")
    if not key:
        raise ValueError("CROSSTRADE_KEY is not set.")
    lines = [
        f"key={key};",
        "command=place;",
        f"account={account};",
        f"instrument={signal['instrument']};",
        f"action={side.lower()};",
        f"qty={signal['qty']};",
        f"order_type={(signal.get('order_type') or 'market').lower()};",
        f"tif={(signal.get('tif') or 'day').lower()};",
    ]
    if signal.get("limit_price") is not None:
        lines.append(f"limit_price={signal['limit_price']};")
    if signal.get("stop_price") is not None:
        lines.append(f"stop_price={signal['stop_price']};")
    if signal.get("take_profit"):
        lines.append(f"take_profit={signal['take_profit']};")
    if signal.get("stop_loss"):
        lines.append(f"stop_loss={signal['stop_loss']};")
    if signal.get("atm_strategy"):
        lines.append(f"atm_strategy={signal['atm_strategy']};")
    if signal.get("sync_strategy"):
        lines.append("sync_strategy=true;")
        lines.append(f"market_position={'long' if side == 'BUY' else 'short'};")
        previous = signal.get("prev_market_position")
        if previous:
            lines.append(f"prev_market_position={previous};")
        if signal.get("out_of_sync"):
            lines.append(f"out_of_sync={signal['out_of_sync']};")
    return "\n".join(lines)


def redact(payload: str) -> str:
    lines = []
    for line in payload.splitlines():
        if line.startswith("key="):
            lines.append("key=***;")
        else:
            lines.append(line)
    return "\n".join(lines)


def send(signal: dict, rating: str) -> dict:
    """Send the order for a finished rating. Hold and a stopped run never get here."""
    side = trade_side(rating)
    if side is None:
        return {"sent": False, "reason": f"{rating or 'REVIEW'} does not place an order."}
    payload = build_payload(signal, side)
    preview = redact(payload)
    if dry_run():
        return {"sent": False, "dry_run": True, "side": side, "preview": preview, "reason": "DRY_RUN is on. The order was not sent."}
    url = crosstrade_url()
    if not url:
        raise ValueError("CROSSTRADE_WEBHOOK_URL is not set.")
    status, body = _post(url, payload)
    ok = 200 <= status < 300
    return {
        "sent": ok,
        "dry_run": False,
        "side": side,
        "status": status,
        "body": body,
        "preview": preview,
        "reason": "CrossTrade accepted the order." if ok else f"CrossTrade returned {status}.",
    }


def send_flatten(signal: dict) -> dict:
    """Close the prop position. A hold never calls this."""
    payload = _flatten_payload(signal)
    preview = redact(payload)
    if dry_run():
        return {"sent": False, "dry_run": True, "side": None, "preview": preview, "reason": "DRY_RUN is on. The position was not closed."}
    url = crosstrade_url()
    if not url:
        raise ValueError("CROSSTRADE_WEBHOOK_URL is not set.")
    status, body = _post(url, payload)
    ok = 200 <= status < 300
    return {
        "sent": ok,
        "dry_run": False,
        "side": None,
        "preview": preview,
        "reason": "CrossTrade flattened the position." if ok else f"CrossTrade returned {status}.",
    }


def _flatten_payload(signal: dict) -> str:
    account = execution_account(signal)
    key = crosstrade_key() or ("dry-run" if dry_run() else "")
    if not key:
        raise ValueError("CROSSTRADE_KEY is not set.")
    return "\n".join([
        f"key={key};",
        "command=flatten;",
        f"account={account};",
        f"instrument={signal['instrument']};",
    ])


def _post(url: str, payload: str) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", errors="replace")[:500]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return exc.code, detail
