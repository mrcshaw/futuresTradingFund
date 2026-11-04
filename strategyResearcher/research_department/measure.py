"""Turn closed trades into the days the stage gate scores."""

from __future__ import annotations

from datetime import datetime

from research_department.charts import ET, session_day
from research_department.stages import DAILY_LOSS, DAILY_TARGET, MAX_SESSION_LOSSES, Day, day_succeeded


def measure(trades: list[dict], dates: list) -> list[Day]:
    """One day per session. A session needs a trade, stops once it is up $500, and stops after two losses."""
    grouped = {day: [] for day in dates}
    for trade in trades or []:
        day = _exit_day(trade.get("exit_time"))
        if day in grouped:
            grouped[day].append(trade)
    return [_day(grouped[day]) for day in dates]


def summarize(days: list[Day]) -> dict:
    count = len(days)
    wins = sum(day.wins for day in days)
    trades = sum(day.trades for day in days)
    return {
        "days": count,
        "success_rate": (sum(1 for day in days if day_succeeded(day)) / count) if count else 0.0,
        "win_percentage": (wins / trades) if trades else 0.0,
        "worst_day": min((day.profit for day in days), default=0.0),
        "trade_count": trades,
        "quiet_sessions": sum(1 for day in days if day.trades < 1),
        "profit": round(sum(day.profit for day in days), 2),
    }


def _day(trades: list[dict]) -> Day:
    running = 0.0
    worst = 0.0
    wins = 0
    losses = 0
    counted = 0
    for trade in trades:
        try:
            profit = float(trade.get("pnl"))
        except (TypeError, ValueError):
            continue
        running += profit
        if running < 0:
            worst = max(worst, -running)
        counted += 1
        if profit > 0:
            wins += 1
        elif profit < 0:
            losses += 1
        if running >= DAILY_TARGET or losses >= MAX_SESSION_LOSSES or running <= -DAILY_LOSS:
            break
    return Day(round(running, 2), round(worst, 2), wins, counted, losses)


def _exit_day(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        when = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    except ValueError:
        return None
    return session_day(int(when.timestamp()))
