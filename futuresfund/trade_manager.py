"""The floor trader manages an open trade from the strategy, with the account's performance first."""

from __future__ import annotations

from futuresfund import ninjatrader
from futuresfund.contracts import POINT_VALUE, root_of
from futuresfund.crosstrade import send, send_flatten
from futuresfund.prop_rules import for_account


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
    tier = for_account(account_size)
    limit = float(tier["max_drawdown"])
    if trail_limit is not None:
        limit = min(limit, float(trail_limit))
    if loss >= limit:
        return (
            f"Open loss ${loss:,.2f} has reached the ${limit:,.0f} trailing drawdown. "
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
        if plan["action"] == "close":
            return _close(signal)
        cap = contract_cap(account_size)
        if int(signal["qty"]) > cap:
            return {"sent": False, "reason": f"qty {signal['qty']} is above the prop contract cap of {cap}."}
        rating = "Buy" if plan.get("side") == "BUY" else "Sell"
        return send(signal, rating)
    except Exception as exc:
        return {"sent": False, "side": plan.get("side"), "reason": str(exc)}


def contract_cap(account_size: float) -> int:
    """The tighter of the Apex tier and a contract cap the prop firm has stated."""
    from futuresfund.book import load

    cap = int(for_account(account_size)["max_contracts"])
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
