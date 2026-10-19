"""The research objective: drawdown under 5 percent of the account, then the trade count.

Every desk that ranks a backtest reads this module, so the researchers, the library, headquarters,
the meetings, and the creation team all pull in the same direction.

A script is acceptable only when it hits the profit target set on headquarters and the drawdown on a
one-year test stays within $35,000, and it trades 130 to 600 times a year across
the tested range. Fewer than 130 is too quiet and more than 600 is too many trades. Two or three trades is not a
strategy that can be tried in the market, so it does not count however large the number. Among acceptable
results the smaller max drawdown wins; drawdowns inside the same band count as equal, and then the larger
profit decides.
Until the target is hit, the larger profit wins, so the search climbs toward the target instead of
shrinking into a tiny, flat result. A profitable result outside the pace band ranks below every paced
result and climbs toward the band. Trade count breaks a tie and nothing else. Consistency across
charts is reported, not optimized. A result that never trades has no drawdown and no place in the ranking.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BAND_SHARE = 0.05         # a drawdown band is this share of the headquarters drawdown ($125 at $2,500)
PACE_WEEK_MIN = 4         # left in the objective stamp; the rewarded count is the year band below
LIBRARY_PACE_WEEK = 3     # 3 a week still files when the year count is inside the band and the factor is strong
PACE_WEEK_MAX = 7         # left in the objective stamp; more than 600 trades a year is not rewarded
TRADES_YEAR_MIN = 130     # rewarded trade count on a 260-session year
TRADES_YEAR_MAX = 600     # more trades than this are not rewarded
PACE_SLACK = 0            # under 4 a week does not count; 2 or 3 trades total is not a strategy
PACE_MIN = 4 / 5          # the same band, in trades a trading day, for older comparisons
PACE_MAX = 7 / 5
MIN_TRADES = 4            # two or three trades cannot be tried in the market
YEAR_DRAWDOWN_CAP = 35_000.0  # one-year research on a $50,000 account. $25,000 to $35,000 is close enough to learn from.
FIRM_PROFIT = 50_000.0    # the profit the portfolio manager files and emails
FIRM_DRAWDOWN = 22_000.0  # the drawdown beside that profit
FIRM_BUFFER = 10_000.0    # profit may be this much lower and still count
RATIO_FLOOR = 1.5         # profit at least this many times the drawdown counts as a successful run
PROFIT_CUSHION = 3_000.0  # above this profit, the 5 percent drawdown cap is lifted
PROFIT_RATIO_MIN = 1.12   # average winner over average loser. Above this, with a profit and the trade band, a run is filed and emailed

TIER_NONE = 0             # error, no trades, or nothing measured
TIER_LOSING = 1           # lost money, crossed the trailing drawdown, or drew down past the limit
TIER_OFF_PACE = 2         # made money but traded outside the weekly pace
TIER_SHORT = 3            # made money at pace but did not reach the profit target
TIER_COUNTS = 4           # hit the target inside the drawdown at pace: acceptable

ET = ZoneInfo("America/New_York")
_SESSION_SHIFT = timedelta(hours=6)   # a futures day opens at 6pm New York, so 6pm bars belong to the next date

_cache: dict = {"key": None, "value": None}
_days: dict = {}
_days_lock = threading.Lock()


def _account_size() -> float:
    try:
        from futuresfund.learn import ACCOUNT_SIZE

        return float(ACCOUNT_SIZE)
    except Exception:
        return 50000.0


def limits() -> dict:
    """The headquarters account rules: size, profit target, and the trailing drawdown. Read again when the book changes."""
    from futuresfund.prop_rules import active_limits

    size = _account_size()
    try:
        from futuresfund.book import BOOK_PATH

        stamp = (str(BOOK_PATH), BOOK_PATH.stat().st_mtime_ns if BOOK_PATH.is_file() else 0)
    except Exception:
        stamp = ("", 0)
    key = (size, stamp)
    if _cache["key"] != key or _cache["value"] is None:
        _cache["value"] = active_limits(size)
        _cache["key"] = key
    return dict(_cache["value"])


def profit_target() -> float:
    return float(limits()["profit_target"])


def drawdown_limit() -> float:
    return float(limits()["max_drawdown"])


def band_width() -> float:
    return max(round(drawdown_limit() * BAND_SHARE, 2), 1.0)


def numbers(row: dict | None) -> tuple[float, float, int] | None:
    """Profit, drawdown, and trades as numbers, or None when the result cannot be read."""
    if not isinstance(row, dict) or not row:
        return None
    if row.get("error") or row.get("fatal") or row.get("blocked"):
        return None
    try:
        profit = float(row.get("net_profit"))
        drawdown = float(row.get("max_drawdown") or 0.0)
        trades = int(row.get("trades") or 0)
    except (TypeError, ValueError):
        return None
    return profit, drawdown, trades


def band(drawdown: float) -> int:
    """The drawdown band a result sits in. Band 0 is the smallest drawdown."""
    return int(max(float(drawdown), 0.0) // band_width())


RESEARCH_SESSIONS = 260      # one year: 52 weeks of five futures sessions. The file on disk is still the five-year download.


def _session_day(epoch: int):
    when = datetime.fromtimestamp(epoch, timezone.utc).astimezone(ET)
    return (when + _SESSION_SHIFT).date()


def session_days(epochs) -> int:
    """How many futures trading days these bar times cover. A day runs from 6pm New York to 5pm the next afternoon."""
    found = set()
    for epoch in epochs:
        try:
            found.add(_session_day(int(epoch)))
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return len(found)


def window_start(epochs, sessions: int | None = None) -> int | None:
    """First bar of the research window. The default keeps the last year of futures sessions.

    Pass a session count to keep only the last that many futures days. A chart shorter than that is kept whole.
    """
    if sessions is None:
        sessions = RESEARCH_SESSIONS
    first_of_day: dict = {}
    for epoch in epochs:
        try:
            stamp = int(epoch)
            day = _session_day(stamp)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        current = first_of_day.get(day)
        if current is None or stamp < current:
            first_of_day[day] = stamp
    if not first_of_day:
        return None
    days = sorted(first_of_day)
    if sessions is None or len(days) <= sessions:
        return first_of_day[days[0]]
    return first_of_day[days[-sessions]]


def research_days(root: str, timeframe: str) -> int | None:
    """Trading days in the research window of one chart, from the bars on disk. None when there are none."""
    key = (str(root or "ES").upper(), str(timeframe or ""))
    with _days_lock:
        if key in _days:
            return _days[key]
    try:
        from futuresfund.research_bars import research_series

        bars = research_series(key[0], key[1])
        start_epoch = window_start(int(bar["t"]) for bar in bars)
        counted = bars if start_epoch is None else [bar for bar in bars if int(bar["t"]) >= start_epoch]
        count = session_days(int(bar["t"]) for bar in counted) or None
    except Exception:
        count = None
    with _days_lock:
        _days[key] = count
    return count


def forget_days() -> None:
    """Drop the cached day counts, so the next question reads the charts again."""
    with _days_lock:
        _days.clear()


_CHART_ROOT = {"MGC": "GC", "MES": "ES", "MNQ": "NQ", "QO": "GC"}   # the research chart that stands for a contract


def _root_of(row: dict) -> str:
    instrument = str(row.get("instrument") or "").strip()
    contract = str(row.get("contract") or "").strip()
    if instrument:
        root = instrument.upper()
    elif contract:
        try:
            from futuresfund.contracts import root_of

            root = root_of(contract)
        except Exception:
            root = contract.upper().replace("1!", "")
    else:
        root = "ES"
    return _CHART_ROOT.get(root, root)


def days_for(row: dict | None) -> float | None:
    """Trading days the result was tested over. Measured results carry it; older notes take it from the research chart."""
    if not isinstance(row, dict):
        return None
    try:
        days = float(row.get("days")) if row.get("days") is not None else None
    except (TypeError, ValueError):
        days = None
    if days is not None and days > 0:
        return days
    timeframe = str(row.get("timeframe") or "")
    if not timeframe:
        return None
    count = research_days(_root_of(row), timeframe)
    return float(count) if count else None


def pace(row: dict | None) -> float | None:
    """Trades a trading day, or None when the days are unknown."""
    found = numbers(row)
    days = days_for(row)
    if found is None or not days:
        return None
    return round(found[2] / days, 2)


def trades_per_week(row: dict | None) -> float | None:
    """Trades a week. A futures week is five trading sessions."""
    rate = pace(row)
    if rate is None:
        return None
    return round(rate * 5, 2)


def trades_per_year(row: dict | None) -> float | None:
    """Trades on a 260-session year. None when the span is unknown."""
    found = numbers(row)
    days = days_for(row)
    if found is None or not days:
        return None
    return round(found[2] / days * RESEARCH_SESSIONS, 2)


def in_trade_band(yearly: float) -> bool:
    return TRADES_YEAR_MIN - 1e-9 <= yearly <= TRADES_YEAR_MAX + 1e-9


def paced(row: dict | None) -> bool | None:
    """130 to 600 trades on a 260-session year.

    Fewer than 130 is too quiet. More than 600 is too many trades. None when the
    span is unknown, which no desk holds against a result.
    """
    yearly = trades_per_year(row)
    if yearly is None:
        return None
    return in_trade_band(yearly)


def tradable(row: dict | None) -> bool:
    """A profit the desk can try: at least four trades, and 130 to 600 trades a year.

    Two or three trades is not a strategy. More than 600 trades a year is too many.
    Unknown days do not count as that pace.
    """
    found = numbers(row)
    if found is None or found[2] < MIN_TRADES:
        return False
    return paced(row) is True


def pace_gap(row: dict | None) -> float:
    """How far the yearly trade count sits outside 130 to 600. Zero inside the band, or when unknown."""
    yearly = trades_per_year(row)
    if yearly is None or in_trade_band(yearly):
        return 0.0
    if yearly < TRADES_YEAR_MIN:
        return round(TRADES_YEAR_MIN - yearly, 2)
    return round(yearly - TRADES_YEAR_MAX, 2)


def hit_target(row: dict | None) -> bool:
    """The profit target set on headquarters was reached, or the engine stopped the test there."""
    found = numbers(row)
    if found is None:
        return False
    if "Profit target" in str(row.get("stop_reason") or ""):
        return found[2] >= 1
    return found[0] >= profit_target()


def research_drawdown_cap(account_size: float) -> float:
    """The most drawdown a one-year test may have and still count.

    On a $50,000 account that is $35,000, the top of the $25,000 to $35,000 band.
    A lower drawdown is better. A run inside the band is still worth learning from.
    The headquarters trailing drawdown is a different number and is not this cap.
    """
    size = max(float(account_size), 0.0)
    return round(YEAR_DRAWDOWN_CAP * size / 50_000.0, 2)


def clears_profit_ratio(row: dict | None) -> bool:
    """The average winning trade is more than 1.12 times the average losing trade.

    A run with no losing trade is above that. A run that never recorded the ratio is not.
    """
    if not isinstance(row, dict) or "profit_ratio" not in row:
        return False
    raw = row.get("profit_ratio")
    if raw is None:
        try:
            profit = float(row.get("net_profit"))
            trades = int(row.get("trades") or 0)
        except (TypeError, ValueError):
            return False
        return profit > 0 and trades >= 1
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    return value > PROFIT_RATIO_MIN


def firm_ratio(row: dict | None) -> bool:
    """Near $50,000 profit and $22,000 drawdown.

    Profit may be $10,000 lower, so $40,000 still counts, and any higher profit counts.
    At $40,000 the drawdown may be $22,000. Above that, the drawdown may grow only in the
    same proportion, so more profit does not excuse a worse ratio.
    """
    found = numbers(row)
    if found is None:
        return False
    profit, drawdown, _trades = found
    floor = FIRM_PROFIT - FIRM_BUFFER
    if profit + 0.01 < floor or drawdown < 0:
        return False
    allowed = FIRM_DRAWDOWN * profit / floor
    return drawdown <= allowed + 0.01


def goal_distance(row: dict | None, account_size: float) -> float | None:
    """Higher is closer to the goal. None when the result did not trade.

    The goal is a profit with drawdown inside the one-year cap, $35,000 on a $50,000 account.
    Drawdown above that cap counts against the profit. A result outside 130 to 600 trades
    a year stays behind a result inside that band, and it can still move.
    """
    found = numbers(row)
    if found is None or found[2] < 1:
        return None
    profit, drawdown, trades = found
    cap = research_drawdown_cap(account_size)
    distance = profit - max(0.0, drawdown - cap)
    if trades < MIN_TRADES or paced(row) is False:
        distance -= cap
    return distance


def scale_move(previous: dict | None, row: dict | None, account_size: float) -> str:
    """closer, farther, or flat. Flat is no helpful change toward the goal.

    The scale goal is the win rate on the strategy library page, and the profit factor on that page.
    A single trade that loses more than $1,500 moves away from the goal.
    When both results report a win rate, the marker uses that reading.
    """
    if trade_loss_ok(previous) and not trade_loss_ok(row):
        return "farther"
    if not trade_loss_ok(previous) and trade_loss_ok(row):
        return "closer"
    before_over = _past_drawdown_goal(previous, account_size)
    after_over = _past_drawdown_goal(row, account_size)
    if before_over or after_over:
        if before_over and not after_over:
            return "closer"
        if after_over and not before_over:
            return "farther"
        before_draw = numbers(previous)
        after_draw = numbers(row)
        if before_draw is None or after_draw is None:
            return "flat"
        if after_draw[1] < before_draw[1] - 0.5:
            return "closer"
        if after_draw[1] > before_draw[1] + 0.5:
            return "farther"
    before_rate = win_rate_percent(previous)
    after_rate = win_rate_percent(row)
    before_factor = profit_factor_of(previous)
    after_factor = profit_factor_of(row)
    if scale_goal() != "win_rate" or (before_rate is not None and after_rate is not None) or (
        before_factor is not None and after_factor is not None
    ):
        before_reading = result_scale(previous, account_size)
        after_reading = result_scale(row, account_size)
        if after_reading > before_reading + 0.001:
            return "closer"
        if after_reading < before_reading - 0.001:
            return "farther"
        return "flat"
    before = goal_distance(previous, account_size)
    after = goal_distance(row, account_size)
    if before is None and after is None:
        return "flat"
    if before is None:
        return "closer"
    if after is None:
        return "farther"
    if after > before + 0.5:
        return "closer"
    if after < before - 0.5:
        return "farther"
    return "flat"


FACTOR_GOAL = 3.0
WIN_RATE_GOAL = 60.0
PROFIT_FACTOR_MIN = 1.1
DRAWDOWN_LIMIT_PCT = 5.0    # the first goal: drawdown stays under this share of the account
TRADE_LOSS_PCT = 3.0        # one trade, as a share of the account (3% of $50,000 is $1,500)
MAX_TRADE_LOSS = 1500.0     # the same single-trade limit on a $50,000 account
TRADE_SAVE_MAX = 900        # a run with more trades than this cannot be saved
CONSISTENCY_DAYS = (7, 30, 90, 365)
ACCOUNT_LOSS_CAP = 2_500.0  # 5% of a $50,000 account
ACCOUNT_PROFIT_LOCK = 3_000.0


def scale_win_rate() -> float:
    """The win rate at the goal end of the scale. It is the win rate on the strategy library page."""
    try:
        from futuresfund.book import strategy_acceptance

        rate = float(strategy_acceptance().get("win_rate"))
    except (TypeError, ValueError, OSError):
        return WIN_RATE_GOAL
    if rate <= 0 or rate > 100:
        return WIN_RATE_GOAL
    return rate


def scale_goal() -> str:
    """The checked acceptance number the scale moves toward.

    Drawdown is handled before this. The trade count is the scale when its box is on
    and the saved goal's box is off. An empty string means no number box is on.
    """
    try:
        from futuresfund.book import acceptance_goal

        goal = acceptance_goal()
    except (TypeError, ValueError, OSError):
        return "min_trades"
    if goal in {"win_rate", "profit_factor", "min_profit", "min_trades"}:
        return goal
    return ""


def scale_min_profit() -> float:
    """The profit at the goal end of the scale when profit is the chosen goal."""
    try:
        from futuresfund.book import strategy_acceptance

        profit = float(strategy_acceptance().get("min_profit"))
    except (TypeError, ValueError, OSError):
        return 20_000.0
    if profit < 0:
        return 20_000.0
    return profit


def scale_min_trades() -> int:
    """The trade count at the goal end of the scale when trades are the chosen goal."""
    try:
        from futuresfund.book import strategy_acceptance

        trades = int(strategy_acceptance().get("min_trades"))
    except (TypeError, ValueError, OSError):
        return 100
    if trades < 1:
        return 100
    return trades


def _toward_goal(value: float, target: float) -> float:
    """0 is below the chosen number. 1 is at that number or above it."""
    try:
        value = float(value)
        target = float(target)
    except (TypeError, ValueError):
        return 0.0
    if target <= 0:
        return 1.0 if value > 0 else 0.0
    if value >= target:
        return 1.0
    if value <= 0:
        return 0.0
    return round(min(0.99, value / target), 4)


def _cap_scale_loss(row: dict | None, reading: float) -> float:
    if not trade_loss_ok(row):
        return round(min(reading, 0.25), 4)
    return round(reading, 4)


def scale_profit_factor() -> float:
    """The profit factor the scale has to clear. It is the profit factor on the strategy library page."""
    try:
        from futuresfund.book import strategy_acceptance

        factor = float(strategy_acceptance().get("profit_factor"))
    except (TypeError, ValueError, OSError):
        return PROFIT_FACTOR_MIN
    if factor <= 0:
        return PROFIT_FACTOR_MIN
    return factor


def account_drawdown_cap(size: float | None = None) -> float:
    """The drawdown box, in dollars of the account. The box is 5 percent unless it was changed.

    A box that is turned off does not cap the result.
    """
    account = _account_size() if size is None else float(size)
    pct = _saved_drawdown_pct()
    if pct is None:
        return float("inf")
    return account * pct / 100.0


def _saved_drawdown_pct() -> float | None:
    """The checked drawdown percent. None when that box is off."""
    try:
        from futuresfund.book import strategy_acceptance

        limits = strategy_acceptance()
        if not limits.get("use_drawdown", True):
            return None
        pct = float(limits.get("drawdown_pct") or DRAWDOWN_LIMIT_PCT)
    except (TypeError, ValueError, OSError):
        pct = DRAWDOWN_LIMIT_PCT
    if pct <= 0:
        return None
    return pct


def account_trade_loss_cap(size: float | None = None) -> float:
    """3 percent of the account. One trade may not lose more than this."""
    account = _account_size() if size is None else float(size)
    return account * TRADE_LOSS_PCT / 100.0


def drawdown_share_text() -> str:
    """The drawdown box on the strategy library page, in words."""
    pct = _saved_drawdown_pct()
    if pct is None:
        return "the drawdown box on the strategy library page"
    return f"{pct:g} percent of the account"


def account_line() -> str:
    """The account rule, in the words the researchers read."""
    share = drawdown_share_text()
    return (
        f"The goal, above everything else, is to keep the drawdown under {share}. "
        "The trade count is on as well. "
        "Profit, the profit factor, and the win rate count only when their boxes are checked on the strategy library page. "
        f"Until profit is above $3,000, a drawdown of {share} or more is not included. "
        "After that, the run is included when the profit is at least 1.5 times the drawdown. "
        "A single loss cannot be more than 3 percent of the account."
    )


def drawdown_line() -> str:
    return f"The drawdown reached {drawdown_share_text()}, so the result is not included."


def goal_line() -> str:
    """The scale goal. Only a checked acceptance number is the end of the scale."""
    from futuresfund.book import strategy_acceptance

    limits = strategy_acceptance()
    rate = scale_win_rate()
    factor = scale_profit_factor()
    goal = scale_goal()
    if goal == "profit_factor":
        head = (
            f"A profit factor of {factor:g}. That is the goal on the scale. "
            "The number is the profit factor on the strategy library page. "
            "A higher profit factor moves toward that end. A lower profit factor moves away."
        )
    elif goal == "min_profit":
        profit = scale_min_profit()
        head = (
            f"A profit of ${profit:,.0f}. That is the goal on the scale. "
            "The number is the profit on the strategy library page. "
            "A higher profit moves toward that end. A lower profit moves away."
        )
    elif goal == "min_trades":
        trades = scale_min_trades()
        head = (
            f"A trade count of {trades:g}. That is the goal on the scale. "
            "The number is the trade count on the strategy library page. "
            "More trades move toward that end. Fewer trades move away."
        )
    elif goal == "win_rate":
        head = (
            f"A win rate of {rate:g} percent. That is the goal on the scale. "
            "The number is the win rate on the strategy library page. "
            "A higher win rate moves toward that end. A lower win rate moves away."
        )
    else:
        head = "No scale number is turned on."
    extras = []
    if limits.get("use_drawdown"):
        extras.append(f"Keep the drawdown under {float(limits['drawdown_pct']):g} percent of the account.")
    if limits.get("use_win_rate") and goal != "win_rate":
        extras.append(f"A win rate above {rate:g} percent.")
    if limits.get("use_factor") and goal != "profit_factor":
        extras.append(f"A profit factor above {factor:g}.")
    if limits.get("use_profit") and goal != "min_profit":
        extras.append(f"Profit above ${scale_min_profit():,.0f}.")
    if limits.get("use_trades") and goal != "min_trades":
        extras.append(f"At least {scale_min_trades()} trades.")
    if not extras:
        return head
    return head + " " + " ".join(extras)


def acceptance_line() -> str:
    """The checked boxes on the strategy library page, read when the sentence is built.

    Filing, the research guide, the agents, and the portfolio manager emails use this
    sentence, so a saved change is the bar from then on.
    """
    from futuresfund.book import strategy_acceptance

    limits = strategy_acceptance()
    parts = []
    if limits.get("use_drawdown"):
        parts.append(f"the drawdown stays under {float(limits['drawdown_pct']):g} percent of the account")
    if limits.get("use_trades"):
        parts.append(f"the run has at least {int(limits['min_trades'])} trades")
    if limits.get("use_profit"):
        parts.append(f"profit is above ${float(limits['min_profit']):,.0f}")
    if limits.get("use_factor"):
        parts.append(f"the profit factor is above {float(limits['profit_factor']):g}")
    if limits.get("use_win_rate"):
        parts.append(f"the win rate is above {float(limits['win_rate']):g} percent")
    if not parts:
        return "No acceptance box is turned on. The strategy library page holds the checkboxes."
    if len(parts) == 1:
        joined = parts[0]
    else:
        joined = ", ".join(parts[:-1]) + ", and " + parts[-1]
    return (
        "A strategy is filed when "
        + joined
        + ". Those are the boxes turned on at the strategy library page."
    )


def win_rate_percent(row: dict | None) -> float | None:
    """Win rate as a percent from 0 to 100.

    Closed wins over closed trades win when both are present. A stored win rate
    of 1 or less is a fraction. The engine stores the percent, so 60 is 60 percent.
    """
    if not isinstance(row, dict):
        return None
    try:
        trades = int(row.get("trades") or 0)
        if row.get("wins") is not None and trades > 0:
            return max(0.0, min(100.0, 100.0 * float(row["wins"]) / trades))
    except (TypeError, ValueError):
        pass
    if row.get("win_rate") is None:
        return None
    try:
        value = float(row["win_rate"])
    except (TypeError, ValueError):
        return None
    if value < 0:
        return 0.0
    if value <= 1.0:
        return value * 100.0
    return min(value, 100.0)


def account_survives(row: dict | None) -> bool:
    """Until profit is above $3,000, drawdown stays under 5 percent of the account.

    Above that profit the dollar cap is lifted. The profit still has to be at least
    1.5 times the drawdown, so the drawdown stays low. A missing drawdown is not a
    breach. Exactly 5 percent is not under the line unless the profit is already
    above $3,000 and at least 1.5 times that drawdown.
    One trade down more than 3 percent of the account is a separate rule.
    """
    if not isinstance(row, dict) or row.get("max_drawdown") is None:
        return True
    try:
        drawdown = float(row["max_drawdown"])
    except (TypeError, ValueError):
        return True
    if drawdown < account_drawdown_cap():
        return True
    try:
        profit = float(row.get("net_profit"))
    except (TypeError, ValueError):
        return False
    if profit <= PROFIT_CUSHION or drawdown <= 0:
        return False
    return profit / drawdown + 1e-9 >= RATIO_FLOOR


def too_many_to_save(row: dict | None) -> bool:
    """A run with more than 900 trades cannot be saved, so the script is skipped."""
    if not isinstance(row, dict):
        return False
    try:
        return int(row.get("trades") or 0) > TRADE_SAVE_MAX
    except (TypeError, ValueError):
        return False


def trades_already_over_cap(notes: dict | None) -> int:
    """The trade count already measured when it is over 900. Zero when the script can still be studied."""
    if not isinstance(notes, dict):
        return 0
    found = 0

    def take(value) -> None:
        nonlocal found
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            return
        if count > found:
            found = count

    def walk(record) -> None:
        if not isinstance(record, dict):
            return
        take(record.get("over_trade_cap"))
        take(record.get("trades"))
        best = record.get("best")
        if isinstance(best, dict):
            take(best.get("trades"))
        for row in record.get("trials") or []:
            if isinstance(row, dict):
                take(row.get("trades"))
        charts = record.get("charts")
        if isinstance(charts, dict):
            for chart in charts.values():
                walk(chart)

    walk(notes)
    instruments = notes.get("instruments")
    if isinstance(instruments, dict):
        for record in instruments.values():
            walk(record)
    return found if found > TRADE_SAVE_MAX else 0


def _trade_exit(trade: dict) -> datetime | None:
    text = str(trade.get("exit_time") or "")
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def trade_spans(trades: list, end: datetime) -> dict:
    """Profit on the last 7, 30, 90, and 365 days, taken from trades that closed in each window."""
    report = {}
    for days in CONSISTENCY_DAYS:
        start = end - timedelta(days=days)
        pnls = []
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            when = _trade_exit(trade)
            if when is None or when < start:
                continue
            try:
                pnls.append(float(trade["pnl"]))
            except (TypeError, ValueError, KeyError):
                continue
        report[str(days)] = {
            "net_profit": round(sum(pnls), 2) if pnls else 0.0,
            "trades": len(pnls),
            "biggest_loser": round(min(pnls), 2) if pnls else None,
        }
    return report


def consistent_spans(spans: dict | None) -> bool:
    """Each of 7, 30, 90, and 365 days made money, and no trade in them lost more than the cap."""
    if not isinstance(spans, dict):
        return False
    for days in CONSISTENCY_DAYS:
        row = spans.get(str(days))
        if not isinstance(row, dict):
            return False
        try:
            profit = float(row.get("net_profit"))
            count = int(row.get("trades") or 0)
        except (TypeError, ValueError):
            return False
        if count < 1 or profit <= 0 or not trade_loss_ok(row):
            return False
    return True


def trade_loss_ok(row: dict | None) -> bool:
    """One trade may not lose more than 3 percent of the account. A missing loss is not treated as a huge one."""
    if not isinstance(row, dict) or row.get("biggest_loser") is None:
        return True
    try:
        loss = float(row["biggest_loser"])
    except (TypeError, ValueError):
        return True
    return loss >= -account_trade_loss_cap()


def profit_factor_of(row: dict | None) -> float | None:
    """Gross profit divided by gross loss. None when this result did not report it."""
    if not isinstance(row, dict) or row.get("profit_factor") is None:
        return None
    try:
        value = float(row["profit_factor"])
    except (TypeError, ValueError):
        return None
    if value < 0:
        return 0.0
    return value


def library_pace(row: dict | None) -> bool:
    """Whether this run can be filed on the strategy library.

    130 to 600 trades a year files. More than 600 does not, however large the profit.
    Three trades a week still files when the count is inside that band and the profit
    factor is 3 or more, or the profit and drawdown are near $50,000 and $22,000.
    Two trades does not file.
    """
    yearly = trades_per_year(row)
    if yearly is not None and not in_trade_band(yearly):
        return False
    if tradable(row):
        return True
    found = numbers(row)
    if found is None or found[2] < LIBRARY_PACE_WEEK:
        return False
    weekly = trades_per_week(row)
    if weekly is None or weekly + 1e-9 < LIBRARY_PACE_WEEK:
        return False
    factor = profit_factor_of(row)
    if factor is not None and factor + 1e-9 >= FACTOR_GOAL:
        return True
    return firm_ratio(row)


def result_scale(row: dict | None, account_size: float) -> float:
    """0 is the opposite of the goal. 1 is the win rate on the strategy library page.

    The profit factor also has to be above the profit factor on that page, and no
    trade may lose more than $1,500. A result that does not report a win rate
    still uses its profit factor.
    """
    found = numbers(row)
    if found is None or found[2] < 1:
        return 0.0
    limited = _drawdown_goal_scale(found[1], account_size)
    if limited is not None:
        return _scale_for_trade_count(row, limited)
    goal = scale_goal()
    if not goal:
        return _cap_scale_loss(row, 0.5)
    if goal == "profit_factor":
        factor = profit_factor_of(row)
        reading = _toward_goal(factor, scale_profit_factor()) if factor is not None else 0.0
        return _cap_scale_loss(row, reading)
    if goal == "min_profit":
        return _cap_scale_loss(row, _toward_goal(found[0], scale_min_profit()))
    if goal == "min_trades":
        return _cap_scale_loss(row, _toward_goal(found[2], scale_min_trades()))
    rate = win_rate_percent(row)
    if rate is not None:
        goal = scale_win_rate()
        floor = scale_profit_factor()
        if rate + 1e-9 >= goal:
            reading = 1.0
        else:
            reading = round(min(0.99, max(0.0, rate / goal)), 4)
        factor = profit_factor_of(row)
        if factor is None:
            reading = min(reading, 0.99)
        elif factor <= floor:
            reading = min(reading, round(min(0.99, max(0.0, factor / floor)), 4))
        if not trade_loss_ok(row):
            reading = min(reading, 0.25)
        return round(reading, 4)
    factor = profit_factor_of(row)
    if factor is not None:
        if factor >= FACTOR_GOAL:
            reading = 1.0
        else:
            reading = round(min(0.99, max(0.0, factor / FACTOR_GOAL)), 4)
        return _scale_for_trade_count(row, reading)
    profit, drawdown, _trades = found
    cap = max(research_drawdown_cap(account_size), 1.0)
    score = profit - max(0.0, drawdown - cap)
    if score < 0:
        weight = abs(score) / (abs(score) + cap * 0.25)
        return _scale_for_trade_count(row, round(0.45 * (1.0 - weight), 4))
    if score == 0:
        return _scale_for_trade_count(row, 0.5)
    weight = score / (score + cap)
    return _scale_for_trade_count(row, round(min(0.99, 0.5 + 0.49 * weight), 4))


def _past_drawdown_goal(row: dict | None, account_size: float) -> bool:
    """True when this result's drawdown is at or over 5 percent of the account."""
    found = numbers(row)
    if found is None:
        return False
    cap = account_drawdown_cap(account_size)
    return cap != float("inf") and cap > 0 and found[1] >= cap


def _drawdown_goal_scale(drawdown: float, account_size: float) -> float | None:
    """None when the drawdown is under 5 percent of the account.

    Past that line the marker stays below the consistency scores. Closer to the
    line is higher, and it never reaches 0.5.
    """
    cap = account_drawdown_cap(account_size)
    if cap == float("inf") or cap <= 0 or drawdown < cap:
        return None
    over = drawdown - cap
    weight = over / (over + cap)
    return round(0.49 * (1.0 - weight), 4)


def _scale_for_trade_count(row: dict | None, reading: float) -> float:
    """Keep a full reward only inside 130 to 600 trades a year. Far outside pulls the marker down."""
    yearly = trades_per_year(row)
    if yearly is None or in_trade_band(yearly):
        return reading
    if yearly > TRADES_YEAR_MAX:
        over = yearly - TRADES_YEAR_MAX
    else:
        over = TRADES_YEAR_MIN - yearly
    weight = over / (over + TRADES_YEAR_MAX)
    return round(max(0.0, reading * (1.0 - weight)), 4)


def next_scale(position: float, row: dict | None, account_size: float, previous: dict | None = None) -> float:
    """Move the marker one result, in either direction.

    A higher win rate moves toward the win rate on the strategy library page.
    The goal also needs the profit factor on that page. A result with no win
    rate still ticks on its profit factor,
    and a result with neither still ticks on profit.
    """
    if scale_goal() != "win_rate" or win_rate_percent(row) is not None or profit_factor_of(row) is not None:
        reading = result_scale(row, account_size)
        before = result_scale(previous, account_size) if previous is not None else None
        try:
            current = float(position)
        except (TypeError, ValueError):
            current = 0.5
        if before is None:
            return reading
        if reading > before + 0.001:
            return round(max(current, reading) if reading < 1 else 1.0, 4)
        if reading < before - 0.001:
            return round(min(current, reading), 4)
        return round(min(1.0, max(0.0, current)), 4)
    found = numbers(row)
    if found is None or found[2] < 1:
        return 0.0
    profit = found[0]
    cap = max(research_drawdown_cap(account_size), 1.0)
    try:
        current = float(position)
    except (TypeError, ValueError):
        current = 0.5
    if profit < 0:
        weight = min(1.0, abs(profit) / cap)
        tick = 0.08 + 0.12 * weight
        return _scale_for_trade_count(row, round(max(0.0, current - tick), 4))
    if profit > 0:
        before = numbers(previous) if previous is not None else None
        improving = before is None or profit > before[0] + 0.5
        weight = min(1.0, profit / cap)
        tick = 0.08 + 0.12 * weight
        if improving:
            return _scale_for_trade_count(row, round(min(0.99, current + tick), 4))
        return _scale_for_trade_count(row, round(max(0.5, current - tick), 4))
    return _scale_for_trade_count(row, round(min(0.5, max(0.0, current)), 4))


def walk_scale(rows: list | None, account_size: float) -> float:
    """Where the marker sits after every result, starting from the middle."""
    position = 0.5
    previous = None
    for row in rows or []:
        position = next_scale(position, row, account_size, previous)
        previous = row
    return position


def research_success(row: dict | None, account_size: float) -> bool:
    """A run the chart can stop on: a profit, drawdown inside the one-year cap, and 130 to 600 trades a year."""
    found = numbers(row)
    if found is None:
        return False
    profit, drawdown, _trades = found
    if profit <= 0 or drawdown > research_drawdown_cap(account_size):
        return False
    return tradable(row) is True


def profit_to_drawdown(row: dict | None) -> float | None:
    """Profit divided by drawdown. None when the result did not trade."""
    found = numbers(row)
    if found is None or found[2] < 1:
        return None
    profit, drawdown, _trades = found
    if drawdown <= 0:
        return float("inf") if profit > 0 else None
    return profit / drawdown


def ratio_success(row: dict | None) -> bool:
    """A profit at least 1.5 times the drawdown, and the trade count is not low.

    Two or three trades do not count. Outside 130 to 600 trades a year does not count
    when the span is known. Unknown days are not held against the run.
    """
    found = numbers(row)
    if found is None:
        return False
    profit, _drawdown, trades = found
    if profit <= 0 or trades < MIN_TRADES:
        return False
    if paced(row) is False:
        return False
    ratio = profit_to_drawdown(row)
    return ratio is not None and ratio + 1e-9 >= RATIO_FLOOR


def counted_success(row: dict | None, account_size: float) -> bool:
    """A run the engine counts.

    It is a profit inside the one-year drawdown cap, or a profit at least 1.5 times
    the drawdown, and the trade count is not low.
    """
    return research_success(row, account_size) or ratio_success(row)


def within_drawdown(row: dict | None) -> bool:
    """The drawdown is no higher than the one-year cap, $35,000 on a $50,000 account."""
    found = numbers(row)
    if found is None:
        return False
    return found[1] <= research_drawdown_cap(float(limits()["size"]))


def tier(row: dict | None) -> int:
    found = numbers(row)
    if found is None:
        return TIER_NONE
    profit, _drawdown, trades = found
    if trades < 1:
        return TIER_NONE
    if trades < MIN_TRADES:
        return TIER_OFF_PACE if profit > 0 else TIER_LOSING
    if profit <= 0 or not within_drawdown(row):
        return TIER_LOSING
    if paced(row) is False:
        return TIER_OFF_PACE
    if not hit_target(row):
        return TIER_SHORT
    return TIER_COUNTS


def acceptable(row: dict | None) -> bool:
    """Hit the target with drawdown inside the one-year cap, at 130 to 600 trades a year."""
    return tier(row) == TIER_COUNTS


def research_rank(row: dict | None) -> tuple:
    """Search order on the full chart. Higher is better.

    The headquarters profit target and the trailing-drawdown cap are not used.
    A profit counts at 130 to 600 trades a year. Fewer than 130 is too quiet and more than 600 is too many.
    Two or three trades stays at the bottom. A trade that loses more than $1,500 stays off the list.
    A run outside that band ranks below every run inside it, and the one closer to the band ranks higher.
    Drawdown under 5 percent of the account comes first. A result over that line ranks below
    every result under it. Then consistency: the win rate, then the profit factor. Then profit.
    A result with no win rate still ranks by the lower drawdown, then the larger profit.
    Two or three trades stay at the bottom. Trade count only breaks a tie.
    """
    found = numbers(row)
    if found is None or (row or {}).get("error") or (row or {}).get("fatal"):
        return (0, 0.0, 0.0, 0.0, 0, 0)
    profit, drawdown, trades = found
    if trades < 1:
        return (0, 0.0, 0.0, 0.0, 0, 0)
    if trades < MIN_TRADES:
        return (0, float(trades), -drawdown, profit, 0, 0)
    if not trade_loss_ok(row):
        try:
            loss = float(row.get("biggest_loser"))
        except (TypeError, ValueError):
            loss = -account_trade_loss_cap()
        return (0, loss, profit, 0.0, 0, 0)
    under = 1 if drawdown < account_drawdown_cap() else 0
    if paced(row) is True:
        rate = win_rate_percent(row)
        if rate is not None:
            factor = profit_factor_of(row)
            factor_key = factor if factor is not None and factor > PROFIT_FACTOR_MIN else -1.0
            return (3, under, rate, factor_key, profit, float(trades))
        return (2, under, -drawdown, profit, float(trades), 0.0)
    return (1, under, -pace_gap(row), -drawdown, profit, float(trades))


def rank(row: dict | None) -> tuple:
    """Sort key. Higher is better.

    Acceptable results: the lower drawdown band, then the profit. Paced results under the target: the larger
    profit, then the lower drawdown, so the search climbs to the target. Profitable results outside the pace
    band: the one nearest the band, then the profit. Losers: the smaller loss. Trade count is the last
    component, so it breaks a tie and nothing else.
    """
    found = numbers(row)
    if found is None:
        return (TIER_NONE, 0, -10**12, 0.0, 0)
    profit, drawdown, trades = found
    level = tier(row)
    if level == TIER_COUNTS:
        return (level, -band(drawdown), profit, -drawdown, trades)
    if level == TIER_OFF_PACE:
        return (level, -pace_gap(row), profit, -drawdown, trades)
    return (level, profit, -drawdown, 0.0, trades)


def improved(row: dict | None, previous: dict | None) -> bool:
    """Did this result beat the one before it on the objective?"""
    return rank(row) > rank(previous)


def counts(row: dict | None) -> bool:
    """Acceptable: the profit target inside the trailing drawdown, at 130 to 600 trades a year."""
    return tier(row) == TIER_COUNTS


def best(rows, key=None) -> dict | None:
    """The row that ranks highest, or None when nothing ranks."""
    chosen = None
    for row in rows or []:
        item = key(row) if key else row
        if chosen is None or rank(item) > rank(key(chosen) if key else chosen):
            chosen = row
    return chosen


def pace_text() -> str:
    return f"{TRADES_YEAR_MIN:g} to {TRADES_YEAR_MAX:g} trades a year"


def describe() -> str:
    """One paragraph every desk can quote."""
    rules = limits()
    return (
        f"The research objective: keep the drawdown under {drawdown_share_text()}, and meet the trade count. "
        "Profit, the profit factor, and the win rate count only when their boxes are checked. "
        "Guideline 1 is to flatten every open trade at 16:45 New York. "
        "A trade left open after 16:45 can liquidate the account. "
        f"Until profit is above $3,000, a drawdown of {drawdown_share_text()} or more is not included. "
        "After that, the run is included when the profit is at least 1.5 times the drawdown. "
        "A single loss cannot be more than 3 percent of the account. "
        f"{goal_line()} {acceptance_line()} "
        "That acceptance line is the bar for every agent and every report. "
        "The take profit is larger than the stop loss. "
        "A strategy is not included when one trade loses more than 3 percent of the account. "
        f"The headquarters research target is the ${rules['profit_target']:,.0f} profit target "
        f"set on headquarters and the one-year drawdown is no higher than "
        f"${research_drawdown_cap(rules['size']):,.0f} on a ${rules['size']:,.0f} account, "
        f"while averaging {TRADES_YEAR_MIN:g} to {TRADES_YEAR_MAX:g} trades a year over the tested range. "
        f"Fewer than {TRADES_YEAR_MIN:g} is too quiet and more than {TRADES_YEAR_MAX:g} is too many trades. "
        "Two or three trades is not a strategy that can be tried in the market, however large the profit. "
        "Among results under that drawdown, the higher win rate wins, then the higher profit factor, then the profit. "
        "Trade count only breaks a tie. "
        "A result that never trades is not a result."
    )


# The curve reads the same columns as the engine table.
# Time is the days the test covered. The clock stamp on a row is not a quality.
_CURVE_WEIGHTS = {
    "time": 0.08,
    "profit": 0.24,
    "drawdown": 0.20,
    "factor": 0.12,
    "ratio": 0.12,
    "trades": 0.12,
    "win": 0.12,
}
_CURVE_FLOOR = 0.02
_CURVE_ALPHA = 0.35


def _logistic(value: float, center: float, width: float) -> float:
    """A smooth step from 0 to 1. The middle of the rise is `center`."""
    span = max(abs(width), 1e-6)
    z = max(-12.0, min(12.0, (float(value) - float(center)) / span))
    return 1.0 / (1.0 + math.exp(-z))


def _curve_value(row: dict, *keys: str) -> float | None:
    for key in keys:
        if row.get(key) is None or row.get(key) == "":
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            continue
    return None


def curve_parts(row: dict | None) -> dict[str, float]:
    """Each table column as a smooth 0 to 1 piece. A missing column is left out.

    Profit, factor, ratio, and win rate rise toward the library numbers.
    Drawdown falls as the loss grows past 5 percent of a $50,000 account.
    Trades rise through 60 and fall after 600. Time rises with the days covered,
    and a full year sits near the top of that piece.
    """
    if not isinstance(row, dict):
        return {}
    parts: dict[str, float] = {}
    days = _curve_value(row, "days")
    if days is not None and days >= 0:
        parts["time"] = _logistic(days, 130.0, 50.0)
    profit = _curve_value(row, "net_profit", "profit")
    if profit is not None:
        parts["profit"] = _logistic(profit, 5_000.0, 2_500.0)
    drawdown = _curve_value(row, "max_drawdown", "drawdown")
    if drawdown is not None and drawdown >= 0:
        parts["drawdown"] = 1.0 - _logistic(drawdown, 2_500.0, 1_250.0)
    factor = _curve_value(row, "profit_factor", "factor")
    if factor is not None:
        parts["factor"] = _logistic(max(0.0, factor), 1.1, 0.3)
    ratio = _curve_value(row, "profit_ratio", "ratio")
    if ratio is not None:
        parts["ratio"] = _logistic(max(0.0, ratio), 1.12, 0.3)
    trades = _curve_value(row, "trades")
    if trades is not None and trades >= 0:
        rise = _logistic(trades, 60.0, 20.0)
        fall = 1.0 - _logistic(trades, 600.0, 120.0)
        parts["trades"] = rise * fall
    win = _curve_value(row, "win_rate", "win")
    if win is not None:
        if 0.0 < win <= 1.0:
            win *= 100.0
        parts["win"] = _logistic(win, 40.0, 8.0)
    return parts


def result_curve(row: dict | None) -> float | None:
    """One smooth height for a backtest, from 0 to 1.

    The height is the weighted geometric mean of the columns that are present.
    A one-dollar change moves it a little. It cannot jump at $5,000 or at 40 percent.
    None when the row has none of the columns.
    """
    parts = curve_parts(row)
    if not parts:
        return None
    weight = sum(_CURVE_WEIGHTS[name] for name in parts)
    if weight <= 0:
        return None
    total = 0.0
    for name, value in parts.items():
        clamped = min(1.0 - _CURVE_FLOOR, max(_CURVE_FLOOR, value))
        total += (_CURVE_WEIGHTS[name] / weight) * math.log(clamped)
    return round(math.exp(total), 4)


def smooth_result_curve(rows: list | None, alpha: float = _CURVE_ALPHA) -> list[float]:
    """The curve across results in time order.

    Each new result pulls the line part of the way toward that result.
    A run of better backtests lifts the line. A run of worse ones lowers it.
    Rows with nothing to score are skipped.
    """
    pace = min(1.0, max(0.05, float(alpha)))
    line: list[float] = []
    level = None
    for row in rows or []:
        raw = result_curve(row)
        if raw is None:
            continue
        level = raw if level is None else level + pace * (raw - level)
        line.append(round(level, 4))
    return line


def short() -> str:
    rules = limits()
    return (
        f"{acceptance_line()} "
        f"Take profit larger than the stop, no trade down more than 3 percent of the account, at {pace_text()}."
    )


def stamp() -> str:
    """Names the objective and the headquarters figures it reads. When either changes, the notes are ranked again
    and the library is folded again at the next start."""
    from futuresfund.library import OBJECTIVE_TAG

    return f"{OBJECTIVE_TAG}@{profit_target():.0f}/{drawdown_limit():.0f}/{PACE_WEEK_MIN:g}-{PACE_WEEK_MAX:g}w"
