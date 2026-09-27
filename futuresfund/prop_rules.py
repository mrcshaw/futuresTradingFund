"""Apex intraday trailing performance-account limits. A rule that breaches them cannot be used."""

from __future__ import annotations

# Size, max intraday trailing drawdown, max contracts. Daily loss, scaling, and inactivity are on for every tier.
TIERS = (
    {"size": 25000, "max_drawdown": 1000, "max_contracts": 2},
    {"size": 50000, "max_drawdown": 2000, "max_contracts": 4},
    {"size": 100000, "max_drawdown": 3000, "max_contracts": 6},
    {"size": 150000, "max_drawdown": 4000, "max_contracts": 10},
)
LOCK_BUFFER = 100


def for_account(account_size: float) -> dict:
    """The published tier closest to this account size."""
    return min(TIERS, key=lambda tier: abs(tier["size"] - float(account_size)))


def passes(account_size: float, *, net_profit: float, max_drawdown: float, trades: int, breached: bool = False, qty: int = 1) -> bool:
    """A rule that loses money, trades too many contracts, or draws down past the tier cannot be used."""
    tier = for_account(account_size)
    if breached or int(qty) < 1 or int(qty) > int(tier["max_contracts"]):
        return False
    return float(net_profit) > 0 and float(max_drawdown) <= float(tier["max_drawdown"]) and int(trades) >= 1


def gate(item: dict, account_size: float) -> bool:
    """Mark one stored rule eligible or not. An ineligible rule cannot stay recommended or live."""
    tier = for_account(account_size)
    backtest = item.setdefault("backtest", {})
    backtest["drawdown_limit"] = tier["max_drawdown"]
    ok = passes(
        account_size,
        net_profit=float(backtest.get("net_profit") or 0),
        max_drawdown=float(backtest.get("max_drawdown") or 0),
        trades=int(backtest.get("trades") or 0),
        breached=bool(backtest.get("breached")),
        qty=int(item.get("qty") or 1),
    )
    item["proven"] = ok
    if not ok:
        item["recommended"] = False
        item["active"] = False
        item["held"] = False
    return ok


def describe(account_size: float) -> str:
    tier = for_account(account_size)
    lock = tier["max_drawdown"] + LOCK_BUFFER
    return (
        f"Apex intraday trailing account ${tier['size']:,.0f}: "
        f"the balance, including open profit, may not touch a floor "
        f"${tier['max_drawdown']:,.0f} under the peak. "
        f"The floor stops rising once profit reaches ${lock:,.0f}, and then sits $100 above the start. "
        f"Max contracts {tier['max_contracts']}. "
        f"Daily loss limit, contract scaling, and the inactivity rule are on. "
        f"A breach liquidates the account."
    )
