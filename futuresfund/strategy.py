"""The live futures strategy: one timeframe, one direction, and whether alerts may trade it."""

from __future__ import annotations

from futuresfund.book import load, save
from futuresfund.contracts import root_of
from futuresfund.crosstrade import trade_side
from futuresfund.research import active_rules

TIMEFRAMES = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "1D": "1 day",
}

_ALIASES = {
    "1": "1m", "1m": "1m", "1min": "1m", "1minute": "1m",
    "5": "5m", "5m": "5m", "5min": "5m",
    "15": "15m", "15m": "15m", "15min": "15m",
    "30": "30m", "30m": "30m", "30min": "30m",
    "60": "1h", "60m": "1h", "1h": "1h", "1hr": "1h", "1hour": "1h",
    "240": "4h", "240m": "4h", "4h": "4h", "4hr": "4h",
    "d": "1D", "1d": "1D", "1day": "1D", "day": "1D", "daily": "1D",
}


def normalize_timeframe(value: str) -> str:
    key = str(value or "").strip().lower().replace(" ", "")
    found = _ALIASES.get(key)
    if not found:
        raise ValueError("Timeframe must be 1m, 5m, 15m, 30m, 1h, 4h, or 1D.")
    return found


def same_direction(left: str, right: str) -> bool:
    return trade_side(left or "") == trade_side(right or "")


def meeting_decision(current: str, new_rating: str) -> str:
    """Keep when the new rating has the same side. Adjust when it flips or goes flat."""
    return "keep" if same_direction(current, new_rating) else "adjust"


def get_strategy() -> dict | None:
    strategy = load().get("strategy")
    return strategy if isinstance(strategy, dict) else None


def lead_script() -> dict | None:
    """The most profitable proven rule, as Pine, for headquarters and the desks."""
    rows = [
        item for item in (get_strategy() or {}).get("strategies") or []
        if item.get("proven") and item.get("pine")
    ]
    if not rows:
        return None
    active = [item for item in rows if item.get("active")]
    recommended = [item for item in rows if item.get("recommended")]
    pool = active or recommended or rows
    best = max(pool, key=lambda item: float((item.get("backtest") or {}).get("net_profit") or 0))
    backtest = best.get("backtest") or {}
    return {
        "id": best.get("id"),
        "title": best.get("title"),
        "formula": best.get("formula") or "",
        "pine": best.get("pine") or "",
        "net_profit": backtest.get("net_profit"),
        "max_drawdown": backtest.get("max_drawdown"),
        "trades": backtest.get("trades"),
    }


def store_strategy(strategy: dict) -> dict:
    book = load()
    book["strategy"] = strategy
    save(book)
    return strategy


def series_match(strategy: dict | None, signal: dict) -> tuple[bool, str]:
    """An alert may add its bar only for the account, contract, and timeframe of this book."""
    if not strategy or not (strategy.get("strategies") or strategy.get("rule") or strategy.get("account")):
        return False, "No tested strategy yet. Upload the bar file and run the initial analysis first."
    alert_frame = signal.get("timeframe")
    book_frame = strategy.get("timeframe")
    if alert_frame and book_frame:
        from futuresfund.timeframe import bar_minutes

        if bar_minutes(alert_frame) > bar_minutes(book_frame):
            return False, (
                f"Alert timeframe {alert_frame} is coarser than the strategy timeframe {book_frame}. "
                "A finer alert can watch the stop while the strategy candle is open."
            )
    try:
        if strategy.get("contract") and root_of(signal["instrument"]) != root_of(str(strategy.get("contract") or "")):
            return False, "Alert contract does not match the strategy contract."
    except ValueError as exc:
        return False, str(exc)
    account = str(strategy.get("account") or "")
    if not account:
        return False, "The strategy has no assigned account, so this alert was not sent."
    if signal.get("account") != account:
        return False, f"Alert account does not match the assigned account {account}."
    return True, "The alert matches the assigned account and timeframe."


def accept_alert(strategy: dict | None, signal: dict) -> tuple[bool, str]:
    """An interval alert may trade only when a proven strategy is active for this book."""
    matched, detail = series_match(strategy, signal)
    if not matched:
        return False, detail
    if not active_rules(strategy):
        return False, "No active strategy is armed. The bar was still added for the next meeting."
    return True, detail
