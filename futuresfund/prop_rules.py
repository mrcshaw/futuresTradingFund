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


def active_limits(account_size: float | None = None) -> dict:
    """Rules entered for this firm. The published tier is used until an account is saved."""
    tier = for_account(float(account_size or 50000))
    saved = _saved_rules()
    if not saved:
        return {
            "account": "",
            "size": float(tier["size"]),
            "profit_target": 3000.0,
            "max_drawdown": float(tier["max_drawdown"]),
            "trailing": True,
            "max_contracts": int(tier["max_contracts"]),
            "source": "tier",
        }
    return {
        "account": str(saved.get("account") or ""),
        "size": float(saved.get("size") or tier["size"]),
        "profit_target": float(saved.get("profit_target") or 3000),
        "max_drawdown": float(saved.get("max_drawdown")),
        "trailing": bool(saved.get("trailing", True)),
        "max_contracts": int(saved.get("max_contracts") or tier["max_contracts"]),
        "source": "entered",
    }


def rules_sentence(account_size: float | None = None) -> str:
    """The line the floor trader reads before an order."""
    rules = active_limits(account_size)
    kind = "trailing drawdown" if rules["trailing"] else "drawdown"
    name = rules["account"] or "This account"
    return (
        f"{name}: ${rules['size']:,.0f} account, profit target ${rules['profit_target']:,.0f}, "
        f"{kind} ${rules['max_drawdown']:,.0f}, max contracts {rules['max_contracts']}."
    )


def _saved_rules() -> dict:
    from futuresfund.book import load

    raw = load().get("account_rules")
    if isinstance(raw, dict) and raw.get("max_drawdown") not in (None, ""):
        return raw
    return {}


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
    rules = active_limits(account_size)
    kind = "trailing" if rules["trailing"] else "fixed"
    name = f" {rules['account']}" if rules["account"] else ""
    return (
        f"Account{name}: ${rules['size']:,.0f}. "
        f"Profit target ${rules['profit_target']:,.0f}. "
        f"The {kind} drawdown is ${rules['max_drawdown']:,.0f}. "
        f"Max contracts {rules['max_contracts']}. "
        "No daily loss dollar amount is on file unless the prop firm stated one. "
        "A breach can liquidate the account."
    )
