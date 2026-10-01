"""The floor trader manages an open trade from the strategy, with the account's performance first."""

from __future__ import annotations

from futuresfund import ninjatrader
from futuresfund.contracts import POINT_VALUE, root_of
from futuresfund.crosstrade import send, send_flatten
from futuresfund.prop_rules import for_account


def plan_from_alert(signal: dict, held: int, qty: int) -> dict | None:
    """Use the trade already named in the TradingView alert. None when the alert has no trade."""
    from futuresfund.research import trade_plan

    action = signal.get("hinted_action")
    if action not in {"BUY", "SELL"}:
        return None
    size = int(signal.get("qty") or 0) or int(qty or 1)
    if size < 1:
        size = 1
    sign = 1 if action == "BUY" else -1
    held_n = int(held or 0)
    # A sell and a later buy are one round trip. The opposite alert closes it.
    if held_n and ((held_n > 0 and sign < 0) or (held_n < 0 and sign > 0)):
        return {
            "action": "close",
            "side": "BUY" if held_n < 0 else "SELL",
            "qty": abs(held_n),
            "flatten_first": False,
            "target": 0,
            "from_alert": True,
        }
    plan = trade_plan(sign, held_n, size)
    plan["from_alert"] = True
    return plan


def reconcile_position(signal: dict, held: int, entry: float | None) -> tuple[int, float | None]:
    """Prefer the fresh NinjaTrader position over the desk's copy."""
    live = ninjatrader.cached_position(signal["account"], signal["instrument"])
    contracts = ninjatrader.signed_contracts(live)
    if contracts is None:
        return held, entry
    average = None if live is None else live.get("averagePrice", live.get("average_price"))
    if average is None:
        return int(contracts), entry
    return int(contracts), float(average)


def open_loss(held: int, entry: float | None, price: float | None, point: float, unrealized: float | None = None) -> float | None:
    """Dollars the open trade is down. A NinjaTrader unrealized figure wins when the stream has one."""
    if unrealized is not None:
        return round(max(0.0, -float(unrealized)), 2)
    if not held or entry is None or price is None:
        return None
    pnl = (float(price) - float(entry)) * int(held) * float(point)
    return round(max(0.0, -pnl), 2)


def liquidation_reason(
    account_size: float,
    held: int,
    entry: float | None,
    price: float | None,
    point: float,
    unrealized: float | None = None,
    daily_limit: float | None = None,
    trail_limit: float | None = None,
) -> str | None:
    """Tell the floor to close when the open loss can liquidate the account."""
    if int(held or 0) == 0:
        return None
    loss = open_loss(held, entry, price, point, unrealized)
    if loss is None:
        return None
    from futuresfund.prop_rules import active_limits

    rules = active_limits(account_size)
    tier = for_account(account_size)
    limit = float(trail_limit if trail_limit is not None else tier["max_drawdown"])
    kind = "trailing drawdown" if rules.get("trailing", True) else "drawdown"
    if loss >= limit:
        return (
            f"Open loss ${loss:,.2f} has reached the ${limit:,.0f} {kind}. "
            "Close the trade. Leaving it open can liquidate the account."
        )
    if daily_limit is not None and loss >= float(daily_limit):
        return (
            f"Open loss ${loss:,.2f} has reached the ${float(daily_limit):,.0f} daily loss limit stated by the prop firm. "
            "Close the trade. Leaving it open can liquidate the account."
        )
    return None


def deliver(signal: dict, plan: dict, account_size: float) -> dict:
    """Send the strategy order through the CrossTrade webhook. NinjaTrader flattens only if that webhook did not send."""
    if plan["action"] == "hold":
        return {"sent": False, "reason": "Holding. The strategy is already at this position."}
    try:
        if _paper_only(signal.get("account") or ""):
            return _paper_result(signal, plan)
        if plan["action"] == "close" and not plan.get("side"):
            return _close(signal)
        cap = contract_cap(account_size)
        if int(signal["qty"]) > cap:
            return {"sent": False, "reason": f"qty {signal['qty']} is above the prop contract cap of {cap}."}
        rating = "Buy" if plan.get("side") == "BUY" else "Sell"
        return send(signal, rating)
    except Exception as exc:
        return {"sent": False, "side": plan.get("side"), "reason": str(exc)}


def contract_cap(account_size: float) -> int:
    """The contract cap entered for this account. Mail can only tighten it."""
    from futuresfund.book import load
    from futuresfund.prop_rules import active_limits

    cap = int(active_limits(account_size)["max_contracts"])
    stated = (load().get("prop_updates") or {}).get("max_contracts")
    if stated is None:
        return cap
    return min(cap, int(stated))


def live_unrealized(signal: dict) -> float | None:
    return ninjatrader.unrealized(ninjatrader.cached_position(signal["account"], signal["instrument"]))


def point_value(instrument: str) -> float:
    try:
        return POINT_VALUE[root_of(instrument)]
    except (ValueError, KeyError):
        return 50.0


def _paper_only(account: str) -> bool:
    """A dry-run account that is not configured is traded on the paper book."""
    from futuresfund.config import dry_run, prop_accounts

    name = str(account or "").strip()
    if not dry_run() or not name or name in prop_accounts():
        return False
    if name.lower() == "paper":
        return True
    from futuresfund.book import load

    for row in load().get("running") or []:
        if str(row.get("account") or "").strip() == name:
            return True
    return False


def _paper_result(signal: dict, plan: dict) -> dict:
    side = plan.get("side") or ""
    command = "FLATTEN" if plan.get("action") == "close" else "PLACE"
    lines = [
        "key=***;",
        f"command={command.lower()};",
        f"account={signal.get('account')};",
        f"instrument={signal.get('instrument')};",
    ]
    if side:
        lines.append(f"action={side.lower()};")
    lines.append(f"qty={signal.get('qty')};")
    lines.append("order_type=market;")
    lines.append("tif=day;")
    return {
        "sent": False,
        "dry_run": True,
        "side": side or None,
        "preview": "\n".join(lines),
        "reason": "DRY_RUN is on. The paper order was not sent.",
    }


def _close(signal: dict) -> dict:
    result = send_flatten(signal)
    if result.get("sent") or result.get("dry_run"):
        return result
    backup = ninjatrader.flatten_position(signal["account"], signal["instrument"])
    if backup.get("ok"):
        return {
            "sent": True,
            "dry_run": False,
            "side": None,
            "reason": "The webhook did not send. NinjaTrader flattened the position and cancelled working orders.",
        }
    return result
