"""The four backtest stages and the day that counts as a success."""

from __future__ import annotations

from dataclasses import dataclass, field


STAGE_DAYS = (7, 30, 90, 365)
DEFAULT_SUCCESS_RATE = 0.60
DEFAULT_ATTEMPTS = 20
DAILY_TARGET = 500
DAILY_LOSS = 400
MAX_SESSION_LOSSES = 2
MIN_TRADES = 3


@dataclass(frozen=True)
class Day:
    profit: float
    worst_loss: float
    wins: int = 0
    trades: int = 0
    losses: int = 0


@dataclass(frozen=True)
class StageRow:
    script: str
    instrument: str
    timeframe: str
    change: str
    justification: str
    days: int
    success_rate: float
    win_percentage: float
    worst_day: float
    trade_count: int
    attempt: int
    profit: float = 0.0
    profit_goal: float = 0.0


def resolve_settings(success_rate: float | None = None, attempts: int | None = None) -> tuple[float, int]:
    """Empty settings use the department defaults."""
    rate = DEFAULT_SUCCESS_RATE if success_rate is None else float(success_rate)
    count = DEFAULT_ATTEMPTS if attempts is None else int(attempts)
    return rate, count


def day_succeeded(day: Day) -> bool:
    """A day with a trade that reached $500, stayed inside the loss limit, and took at most two losses."""
    return (
        day.profit >= DAILY_TARGET
        and day.worst_loss <= DAILY_LOSS
        and day.trades >= 1
        and day.losses <= MAX_SESSION_LOSSES
    )


def profit_total(days: int, daily: float = DAILY_TARGET) -> float:
    """$500 for every day in the test."""
    return round(float(daily) * int(days), 2)


def profit_goal_for(days: int, daily: float = DAILY_TARGET) -> float:
    """The profit goal is 50% of $500 times the days tested."""
    return round(profit_total(days, daily) * 0.5, 2)


def _move(previous, current) -> str:
    """A higher number moves toward the goal. A lower number moves away."""
    if current is None or previous is None:
        return "waiting"
    if current > previous + 1e-12:
        return "toward"
    if current + 1e-12 < previous:
        return "away"
    return "held"


def reward_reading(previous, current, goal, profit=None, previous_profit=None, days: int = 7, daily: float = DAILY_TARGET) -> dict:
    """The win-rate goal is the setting. The profit scale starts at $0 and its goal is half of $500 times the days."""
    goal_value = float(goal)
    current_value = None if current is None else float(current)
    previous_value = None if previous is None else float(previous)
    profit_value = None if profit is None else round(float(profit), 2)
    earlier_profit = None if previous_profit is None else round(float(previous_profit), 2)
    total = profit_total(days, daily)
    profit_goal = profit_goal_for(days, daily)
    return {
        "goal": goal_value,
        "win_percentage": current_value,
        "previous": previous_value,
        "profit": profit_value,
        "profit_previous": earlier_profit,
        "profit_goal": profit_goal,
        "profit_total": total,
        "days": int(days),
        "direction": _move(previous_value, current_value),
        "profit_direction": _move(earlier_profit, profit_value),
        "at_goal": current_value is not None and current_value + 1e-12 >= goal_value,
        "profit_at_goal": profit_value is not None and profit_value + 1e-9 >= profit_goal,
        "profitable": bool(profit_value is not None and profit_value > 0),
    }


def reward_note(reading: dict) -> str:
    """The sentence the meeting answers about the reward scale."""
    goal = float(reading.get("goal") or 0)
    rule = (
        "The script has to reach the profit goal and meet the win percentage at the same time. "
        "The reward scale's goal is that win percentage. "
        "A change that raises the win percentage moves the scale toward the goal. "
        "A change that lowers the win percentage moves the scale away from the goal."
    )
    current = reading.get("win_percentage")
    if current is None:
        return f"Reward scale: the goal is {goal:.0%}. No reading yet. {rule} {_profit_sentence(reading)}"
    profit = reading.get("profit")
    money = f" Profit ${float(profit):,.0f}." if profit is not None else ""
    profit_text = "The script is profitable." if reading.get("profitable") else "The script is not profitable."
    direction = reading.get("direction")
    if direction == "toward":
        move = "The last change raised the win percentage, so the scale moved toward the goal."
    elif direction == "away":
        move = "The last change lowered the win percentage, so the scale moved away from the goal."
    elif direction == "held":
        move = "The last change left the win percentage in place."
    else:
        move = "This is the first reading on the scale."
    place = " The win percentage is at the goal." if reading.get("at_goal") else " The win percentage is short of the goal."
    return (
        f"Reward scale: win percentage {float(current):.0%}, goal {goal:.0%}. "
        f"{move}{place}{money} {profit_text} {rule} {_profit_sentence(reading)}"
    )


def _profit_sentence(reading: dict) -> str:
    days = int(reading.get("days") or 7)
    total = float(reading.get("profit_total") or profit_total(days))
    goal = float(reading.get("profit_goal") or profit_goal_for(days))
    rule = (
        f"The profit scale starts at $0. The total is ${total:,.0f}, which is $500 times {days} days. "
        f"The goal is 50% of that total, ${goal:,.0f}. "
        "A change that raises the profit moves that scale toward the goal. "
        "A change that lowers the profit moves that scale away from the goal."
    )
    profit = reading.get("profit")
    if profit is None:
        return f"Profit scale: no reading yet. {rule}"
    direction = reading.get("profit_direction")
    if direction == "toward":
        move = "The last change raised the profit, so the profit scale moved toward the goal."
    elif direction == "away":
        move = "The last change lowered the profit, so the profit scale moved away from the goal."
    elif direction == "held":
        move = "The last change left the profit in place."
    else:
        move = "This is the first reading on the profit scale."
    place = " The profit is at the goal." if reading.get("profit_at_goal") else " The profit is short of the goal."
    return f"Profit scale: ${float(profit):,.0f}, goal ${goal:,.0f}. {move}{place} {rule}"


def required_trades(days: int) -> int:
    """A 7-day window needs at least 3 trades before it can move on to 30 days."""
    return MIN_TRADES


def window_rate(days: list[Day], count: int) -> float:
    window = list(days)[:count]
    if len(window) < count:
        raise ValueError(f"The script needs {count} days and has {len(window)}.")
    return sum(1 for day in window if day_succeeded(day)) / count


@dataclass
class StageBook:
    success_rate: float = DEFAULT_SUCCESS_RATE
    attempts: int = DEFAULT_ATTEMPTS
    stage_index: int = 0
    attempt: int = 1
    daily_target: float = DAILY_TARGET
    stopped: bool = False
    passed_all: bool = False
    rows: dict[int, list[StageRow]] = field(default_factory=lambda: {count: [] for count in STAGE_DAYS})

    @property
    def days(self) -> int:
        return STAGE_DAYS[self.stage_index]

    def submit(
        self,
        days: list[Day],
        *,
        script: str,
        instrument: str,
        timeframe: str,
        change: str,
        justification: str,
    ) -> str:
        """Record one attempt on the current stage. A pass moves to the next stage at attempt 1."""
        if self.stopped or self.passed_all:
            raise RuntimeError("This job is already finished.")
        count = self.days
        window = list(days)[:count]
        if len(window) < count:
            raise ValueError(f"The script needs {count} days and has {len(window)}.")
        rate = sum(1 for day in window if day_succeeded(day)) / count
        wins = sum(day.wins for day in window)
        trades = sum(day.trades for day in window)
        win_percentage = (wins / trades) if trades else 0.0
        profit = round(sum(day.profit for day in window), 2)
        goal = profit_goal_for(count, self.daily_target)
        # The saved success rate is the required win percentage.
        # Fewer than 3 trades in the 7-day window cannot move on, even at a high win percentage.
        # The profit has to reach 50% of $500 times the days tested.
        if (
            trades >= required_trades(count)
            and win_percentage + 1e-12 >= self.success_rate
            and profit + 1e-9 >= goal
        ):
            self.rows[count].append(StageRow(
                script=script,
                instrument=instrument,
                timeframe=timeframe,
                change=change,
                justification=justification,
                days=count,
                success_rate=rate,
                win_percentage=win_percentage,
                worst_day=min(day.profit for day in window),
                trade_count=trades,
                attempt=self.attempt,
                profit=profit,
                profit_goal=goal,
            ))
            if self.stage_index == len(STAGE_DAYS) - 1:
                self.passed_all = True
            else:
                self.stage_index += 1
                self.attempt = 1
            return "passed"
        if self.attempt >= self.attempts:
            self.stopped = True
            return "stopped"
        self.attempt += 1
        return "failed"
