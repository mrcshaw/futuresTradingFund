"""One meeting: three proposals, then the manager picks one of them."""

from __future__ import annotations

import re
from dataclasses import dataclass

from research_department.inputs import script_inputs
from research_department.stages import profit_goal_for


@dataclass(frozen=True)
class Proposal:
    variable: str
    value: object
    direction: str
    consistent_day: bool
    higher_win_rate: bool
    justification: str


@dataclass(frozen=True)
class MeetingResult:
    order: Proposal | None = None
    resubmit: bool = False
    refused: str = ""
    consumes_attempt: bool = False


def _complete(proposal: Proposal) -> bool:
    return bool(
        proposal.variable
        and str(proposal.justification).strip()
        and proposal.consistent_day
        and proposal.higher_win_rate
    )


def _same(left: Proposal, right: Proposal) -> bool:
    return (
        left.variable == right.variable
        and left.value == right.value
        and left.direction == right.direction
    )


def _pick(proposals: list[Proposal], choice: int | Proposal) -> Proposal | None:
    if isinstance(choice, int):
        if 0 <= choice < len(proposals):
            return proposals[choice]
        return None
    for proposal in proposals:
        if _same(proposal, choice):
            return proposal
    return None


def hold_meeting(proposals: list[Proposal], closed: set[tuple[str, str]], choice: int | Proposal) -> MeetingResult:
    """Accept one of the three proposals, or send the researchers back inside this attempt.

    A closed pair is the variable and the direction already tried at this stage.
    """
    if len(proposals) != 3:
        raise ValueError("The meeting needs three proposals.")
    if any(not _complete(proposal) for proposal in proposals):
        return MeetingResult(resubmit=True, consumes_attempt=False)
    picked = _pick(proposals, choice)
    if picked is None:
        return MeetingResult(refused="fourth change", consumes_attempt=False)
    if (picked.variable, picked.direction) in closed:
        return MeetingResult(refused="closed", consumes_attempt=False)
    return MeetingResult(order=picked, consumes_attempt=True)


def apply_change(source: str, variable: str, value) -> str:
    """Replace one input default. Every other input stays as written."""
    pattern = rf"({re.escape(variable)}\s*=\s*input(?:\.\w+)?\()\s*-?\d+(?:\.\d+)?"
    updated, count = re.subn(pattern, rf"\g<1>{value}", source, count=1)
    if count != 1:
        raise ValueError(f"{variable} is not an input in this script.")
    return updated


# Dollar value of one point. ES, NQ, and MNQ do not share a point value.
MARKET_POINT_VALUE = {"ES": 50, "NQ": 20, "MNQ": 2}


def scales_price(item) -> bool:
    """Point value turns a dollar stop into a price distance. It is not a trade decision."""
    name = item.name.lower().replace("_", "")
    title = (item.title or "").lower()
    return name in {"pv", "pointvalue"} or "point value" in title


def step_limit(item) -> float:
    """The largest move from the current value. One meeting takes one step."""
    size = abs(float(item.default))
    if item.kind == "int":
        return float(max(1, int(round(size * 0.20))))
    return max(0.1, round(size * 0.20, 4))


def tried_key(name, value) -> tuple:
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        number = int(round(number))
    return (str(name), number)


def priced_source(source: str, instrument: str) -> str:
    """Use the market's point value for this run. The saved script is left as written."""
    point = MARKET_POINT_VALUE.get((instrument or "").upper())
    if point is None:
        return source
    for item in script_inputs(source):
        if not scales_price(item) or abs(float(item.default) - point) < 1e-9:
            continue
        value = int(point) if item.kind == "int" else point
        try:
            return apply_change(source, item.name, value)
        except ValueError:
            return source
    return source


def proposal_fault(proposal, inputs, last, tried, goal_win, profit_goal, daily_loss) -> str:
    """Why a finished proposal cannot be run. An empty string means it can."""
    known = {item.name: item for item in inputs}
    item = known.get(proposal.variable)
    if item is None or proposal.value is None:
        return ""
    try:
        value = float(proposal.value)
    except (TypeError, ValueError):
        return "The new value has to be a number."
    if scales_price(item):
        return (
            f"{item.name} is the point value. The backtest uses $50 on ES, $20 on NQ, and $2 on MNQ. "
            "Changing it does not decide which trades win. "
            "Choose a stop, a target, a trail, the volume delta, or the pivot."
        )
    limit = step_limit(item)
    if abs(value - float(item.default)) > limit + 1e-6:
        return (
            f"{item.name} is {item.default:g}. One step is {limit:g}. "
            f"{value:g} is larger than one step."
        )
    key = tried_key(item.name, value)
    if any(tried_key(name, old) == key for name, old in (tried or ())):
        return f"{item.name} {key[1]} was already tried and reverted. Choose a different value."
    name = item.name.lower()
    title = (item.title or "").lower()
    if proposal.direction == "up" and ("stop" in name or "stop" in title):
        return (
            f"Do not raise {item.name}. A larger stop makes the losing trade bigger. "
            f"Lower a stop that is wider than the ${float(daily_loss):,.0f} day, or tighten the entry."
        )
    profit = None if not last else float(last.get("profit") or 0)
    is_target = "tp" in name or "target" in name or "take profit" in title
    if is_target and proposal.direction == "down" and (profit is None or profit < float(profit_goal)):
        return (
            f"Do not lower {item.name} while profit is short of the goal. "
            "A smaller target cashes the trade in before the day can make $500."
        )
    quiet = int((last or {}).get("quiet_sessions") or 0)
    win = float((last or {}).get("win_percentage") or 0)
    trades = int((last or {}).get("trade_count") or 0)
    if "delta" in name and proposal.direction == "up" and quiet:
        return "A session had no trade. Lower the volume delta for more trades. Raising it takes fewer trades."
    if (
        "delta" in name
        and proposal.direction == "down"
        and last
        and quiet == 0
        and win + 1e-12 < float(goal_win)
        and trades >= 3
    ):
        return (
            "The sessions are already trading and the win percentage is short. "
            "Lowering the volume delta adds trades and usually adds losses. Raise it, or lower the stop."
        )
    return ""


def next_allowed_change(inputs, last, tried, goal_win, profit_goal, daily_loss, preferred: str = ""):
    """One legal step when a reply does not finish. Point value and closed values are skipped."""
    reason = (
        "The meeting reply did not finish within 10 minutes. "
        "This is one step on the next open input. "
        "It keeps the $500 day more consistent by staying inside the session rules, "
        "and it raises the win percentage without a wider stop."
    )
    names = []
    if preferred:
        names.append(preferred)
    names.extend(item.name for item in inputs if item.name not in names)
    known = {item.name: item for item in inputs}
    for name in names:
        item = known.get(name)
        if item is None or scales_price(item):
            continue
        limit = step_limit(item)
        default = float(item.default)
        down = default - limit
        up = default + limit
        if item.kind == "int":
            down = int(round(down))
            up = int(round(up))
        for direction, value in (("down", down), ("up", up)):
            if float(value) == default:
                continue
            proposal = Proposal(item.name, value, direction, True, True, reason)
            if proposal_fault(proposal, inputs, last, tried, goal_win, profit_goal, daily_loss) == "":
                return proposal
    return None


def change_kept(previous: dict | None, current: dict | None) -> bool:
    """A change stays only when the best profit and the best win percentage both hold, and one of them rises."""
    if not previous or not current:
        return True
    if previous.get("best_win") is None or current.get("best_win") is None:
        return True
    win_ok = float(current["best_win"]) + 1e-12 >= float(previous["best_win"])
    profit_ok = float(current["best_profit"]) + 1e-9 >= float(previous["best_profit"])
    better = (
        float(current["best_win"]) > float(previous["best_win"]) + 1e-12
        or float(current["best_profit"]) > float(previous["best_profit"]) + 1e-9
    )
    return win_ok and profit_ok and better


def board_marks(board: list[dict]) -> dict | None:
    """The highest win percentage and the highest profit on the board. They can be different markets."""
    if not board:
        return None
    enough = [row for row in board if int(row.get("trade_count") or 0) >= 3]
    pool = enough or list(board)

    def win(row):
        return float(row.get("win_percentage") or 0)

    def profit(row):
        return float(row.get("profit") or 0)

    best_win = max(pool, key=win)
    best_profit = max(pool, key=profit)

    def where(row) -> str:
        return f"{row.get('instrument')} {row.get('timeframe')}".strip()

    return {
        "best_win": win(best_win),
        "best_win_profit": profit(best_win),
        "best_win_where": where(best_win),
        "best_profit": profit(best_profit),
        "best_profit_win": win(best_profit),
        "best_profit_where": where(best_profit),
    }


def result_note(board, marks, days: int, daily: float, rate: float) -> str:
    """What the meeting has to beat, including both the best win percentage and the best profit."""
    count = int(days or 7)
    near = round(1000 * count / 7)
    goal = profit_goal_for(count, daily)
    text = (
        f"The first profit to clear is about ${near:,.0f}, which is $1,000 for every 7 days. "
        f"The stage goal is ${goal:,.0f}, 50% of ${float(daily):,.0f} times {count} days. "
        f"The win percentage goal is {float(rate):.0%}. "
        "A change is kept only when the best profit does not fall and the best win percentage does not fall. "
        "If either one falls, that change is reverted."
    )
    if not marks:
        return text
    text += (
        f" Highest win percentage: {marks['best_win_where']} at {float(marks['best_win']):.0%} "
        f"with profit ${float(marks['best_win_profit']):,.0f}."
        f" Highest profit: {marks['best_profit_where']} at ${float(marks['best_profit']):,.0f} "
        f"with win {float(marks['best_profit_win']):.0%}."
        " One result has to reach both."
    )
    if float(marks["best_profit"]) + 1e-9 >= goal and float(marks["best_profit_win"]) + 1e-12 < float(rate):
        text += (
            f" {marks['best_profit_where']} already clears the profit goal. "
            "Raise that win percentage without giving the profit back."
        )
    elif float(marks["best_win"]) + 1e-12 >= float(rate) and float(marks["best_win_profit"]) + 1e-9 < goal:
        text += (
            f" {marks['best_win_where']} already meets the win percentage. "
            "Raise that profit without giving the win percentage back."
        )
    elif float(marks["best_profit"]) + 1e-9 < near:
        text += " Profit is still under about $1,000 for every 7 days. Cut the loss or let the target reach the $500 day."
    rows = []
    for row in board or []:
        rows.append(
            f"{row.get('instrument')} {row.get('timeframe')} "
            f"profit {float(row.get('profit') or 0):,.0f} "
            f"win {float(row.get('win_percentage') or 0):.0%} "
            f"trades {row.get('trade_count')} worst {row.get('worst_day')}"
        )
    if rows:
        text += " Results: " + "; ".join(rows) + "."
    return text


def revert_note(order, previous, rejected) -> str:
    def side(marks) -> str:
        if not marks:
            return "no reading"
        return f"win {float(marks['best_win']):.0%} and profit ${float(marks['best_profit']):,.0f}"

    return (
        f"Reverted {order.variable} {order.value}. "
        f"Before the change: {side(previous)}. After the change: {side(rejected)}. "
        "The best profit and the best win percentage both have to hold or improve. "
        "The script is back to the previous inputs, and that value stays closed."
    )
