"""The research objective: drawdown inside the pro account rule, then the trade count.

Every desk that ranks a backtest reads this module, so the researchers, the library, headquarters,
the meetings, and the creation team all pull in the same direction.

A script passes when it clears the acceptance boxes on the strategy library page (acceptance_misses).
The proving stages use the same boxes pro-rated to their sessions (stage_bars): a 7-day test is 5 of the
260 sessions in a year. The trade count that passes is the library count; more than 600 trades a year
never passes. The search ranking below still aims at 130 to 600 trades a year: that band steers the
search and passes or fails nothing. Two or three trades is not a
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
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BAND_SHARE = 0.05         # a drawdown band is this share of the headquarters drawdown ($125 at $2,500)
PACE_WEEK_MIN = 4         # left in the objective stamp; the rewarded count is the year band below
LIBRARY_PACE_WEEK = 3     # read only by library_pace, which no filing gate calls; filing follows acceptance_misses
PACE_WEEK_MAX = 7         # left in the objective stamp; more than 600 trades a year is not rewarded
TRADES_YEAR_MIN = 130     # the search ranking's aim on a 260-session year; it passes or fails nothing (stage_bars does)
TRADES_YEAR_MAX = 600     # more trades than this are not rewarded
PACE_SLACK = 0            # under 4 a week does not count; 2 or 3 trades total is not a strategy
PACE_MIN = 4 / 5          # the same band, in trades a trading day, for older comparisons
PACE_MAX = 7 / 5
MIN_TRADES = 4            # two or three trades cannot be tried in the market
YEAR_DRAWDOWN_CAP = 750.0  # the size of one step on the research scale, on a $50,000 account
FIRM_PROFIT = 50_000.0    # the profit the portfolio manager files and emails
FIRM_DRAWDOWN = 22_000.0  # the drawdown beside that profit
FIRM_BUFFER = 10_000.0    # profit may be this much lower and still count
PROFIT_DRAWDOWN_SHARE = 0.30  # past the profit target, the drawdown limit grows by this share of each dollar above it
RATIO_FLOOR = 1 / PROFIT_DRAWDOWN_SHARE  # the same rule as profit over drawdown

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
WEEKS_PER_YEAR = 52
# A script proves itself on the last 7, 30, and 90 days before the one-year search. Each stage is
# (calendar days, futures sessions). A stage passes on the acceptance boxes pro-rated to its sessions.
PROVING_STAGES = ((7, 5), (30, 21), (90, 64))
STAGE_SESSIONS = {**{days: sessions for days, sessions in PROVING_STAGES}, 365: RESEARCH_SESSIONS}
# An optional floor of trades a week on top of the library trade count. 0 is off, and then the
# fewest trades come from the library count only, pro-rated to the window.
STAGE_TRADES_PER_WEEK = 0


def stage_passed(row: dict | None, days: int, account_size: float = 50_000.0) -> bool:
    """A short-window run moves to the next stage when it is profitable and clears the saved boxes pro-rated to it.

    stage_bars(days) holds the numbers: the profit box and the library trade count times the stage's
    share of a 260-session year (5, 21, and 64 sessions), and at most 600 trades a year at that pace.
    The drawdown rule, the one-trade stop, the profit factor, and the win rate are the same as for the year.
    """
    return not acceptance_misses(row, days=days)


def stage_sessions(days: int | None) -> float:
    """Futures sessions in a window of this many calendar days. None is the one-year search, 260 sessions.

    The stages are 5, 21, and 64 sessions. Another day count is its trading days, five in every seven.
    """
    if days is None:
        return float(RESEARCH_SESSIONS)
    try:
        count = int(days)
    except (TypeError, ValueError):
        return float(RESEARCH_SESSIONS)
    if count in STAGE_SESSIONS:
        return float(STAGE_SESSIONS[count])
    return max(1, count) * 5 / 7


def stage_share(days: int | None) -> float:
    """The share of a 260-session year a window covers: 5/260 for 7 days, 21/260, 64/260, and 1 for the year."""
    if days is None:
        return 1.0
    return stage_sessions(days) / RESEARCH_SESSIONS


def _window_days(days: int | None) -> int | None:
    """A stage's day count, or None for the year. 365 days or more is the year search, judged as the year."""
    try:
        return None if not days or int(days) >= 365 else int(days)
    except (TypeError, ValueError):
        return None


def stage_name(days: int | None) -> str:
    """The window in words: "7-day test", or "year" for the one-year search."""
    if days is None or int(days) >= 365:
        return "year"
    return f"{int(days)}-day test"


def _window_words(days: int | None) -> str:
    if days is None or int(days) >= 365:
        return "a year"
    return f"{int(days)} days"


def stage_bars(days: int | None = None, limits: dict | None = None) -> dict:
    """What a run of this length must show. The one source for the checks, the posts, and the library page.

    The numbers come from the boxes saved on the strategy library page (book.strategy_acceptance), pro-rated
    to the window's share of a 260-session year:
    profit is the profit box times the share; the fewest trades are the library trade count times the share,
    rounded up; the most trades are 600 a year at the same pace. The drawdown rule is not scaled: a short
    window's drawdown can never be larger than the drawdown of the year that holds it. The stop on one trade,
    the profit factor, and the win rate are not scaled either. Profit above the drawdown and profit that grows
    from 7 to 365 days are checked on the year only. STAGE_TRADES_PER_WEEK, when it is above 0, is an extra
    floor of that many trades for every five sessions.
    """
    if limits is None:
        from futuresfund.book import strategy_acceptance

        limits = strategy_acceptance()
    sessions = stage_sessions(days)
    share = sessions / RESEARCH_SESSIONS
    year = _window_days(days) is None
    try:
        min_profit = max(0.0, float(limits.get("min_profit") or 0.0))
    except (TypeError, ValueError):
        min_profit = 0.0
    try:
        library_count = max(0, int(limits.get("min_trades") or 0))
    except (TypeError, ValueError):
        library_count = 0
    use_profit = bool(limits.get("use_profit"))
    use_trades = bool(limits.get("use_trades"))
    try:
        floor_week = max(0.0, float(STAGE_TRADES_PER_WEEK or 0))
    except (TypeError, ValueError):
        floor_week = 0.0
    library_trades = math.ceil(library_count * share - 1e-9) if use_trades else 0
    # Two or three trades is not a strategy: the year needs at least MIN_TRADES whatever the boxes say,
    # and a shorter window that share of it.
    least = math.ceil(MIN_TRADES * share - 1e-9)
    floor_trades = math.ceil(floor_week * sessions / 5 - 1e-9) if floor_week > 0 else 0
    fewest = max(library_trades, least, floor_trades)
    most = math.floor(TRADES_YEAR_MAX * share + 1e-9)
    use_drawdown = bool(limits.get("use_drawdown", True))
    try:
        account = _account_size()
        start = account * DRAWDOWN_STARTS_PCT / 100.0
        target = drawdown_target()
    except Exception:
        start, target = 2_500.0, ACCOUNT_PROFIT_LOCK
    try:
        factor = float(limits.get("profit_factor"))
    except (TypeError, ValueError):
        factor = PROFIT_FACTOR_MIN
    try:
        rate = float(limits.get("win_rate"))
    except (TypeError, ValueError):
        rate = WIN_RATE_GOAL
    return {
        "days": 365 if days is None else int(days),
        "name": stage_name(days),
        "year": year,
        "sessions": sessions,
        "share": round(share, 6),
        "use_profit": use_profit,
        "profit": round(min_profit * share, 2) if use_profit else 0.0,
        "year_profit": min_profit,
        "weekly_profit": round(min_profit / WEEKS_PER_YEAR, 4),
        "use_trades": use_trades,
        "min_trades": fewest,
        "library_trades": library_trades,
        "year_trades": library_count,
        "weekly_trades": round(library_count / WEEKS_PER_YEAR, 4),
        "floor_week": floor_week,
        "floor_trades": floor_trades,
        "least_trades": least,
        "max_trades": most,
        "trades_year_max": TRADES_YEAR_MAX,
        "use_drawdown": use_drawdown,
        "drawdown_start": round(start, 2),
        "drawdown_target": round(target, 2),
        "drawdown_share": PROFIT_DRAWDOWN_SHARE,
        "trade_loss": round(account_trade_loss_cap(), 2),
        "use_factor": bool(limits.get("use_factor")),
        "profit_factor": factor,
        "use_win_rate": bool(limits.get("use_win_rate")),
        "win_rate": rate,
        "under_drawdown": year,
        "growth": year,
    }


def weekly_trade_floor(limits: dict | None = None) -> float:
    """The fewest trades a week the year asks for: the library count over 52 weeks, or the optional floor when higher."""
    bars = stage_bars(None, limits)
    return round(max(bars["min_trades"] / WEEKS_PER_YEAR, bars["floor_week"]), 4)


def _plain_count(value: float) -> str:
    """A count to one decimal, without a trailing .0: 1.2, 3."""
    shown = f"{float(value):.1f}"
    return shown[:-2] if shown.endswith(".0") else shown


def stage_bars_text(days: int | None = None, limits: dict | None = None) -> str:
    """The bars of one window as one plain sentence, for the posts and the researcher's questions."""
    bars = stage_bars(days, limits)
    where = "The year" if bars["year"] else f"The {bars['name']}"
    parts = []
    if bars["use_profit"]:
        parts.append(f"more than ${bars['profit']:,.0f} profit")
    else:
        parts.append("a profit above $0")
    parts.append(f"at least {bars['min_trades']} and at most {bars['max_trades']} trades")
    if bars["use_factor"]:
        parts.append(f"a profit factor above {bars['profit_factor']:g}")
    if bars["use_win_rate"]:
        parts.append(f"a win rate above {bars['win_rate']:g} percent")
    text = f"{where} needs " + ", ".join(parts[:-1]) + (", and " if len(parts) > 1 else "") + parts[-1] + "."
    if bars["use_drawdown"]:
        text += f" The drawdown stays inside {drawdown_share_text()}" + ("." if bars["year"] else ", the same as the year.")
    if bars["year"]:
        text += " The profit is larger than the drawdown and grows from the last 7 days to the last 30, 90, and 365 days."
    text += f" No stop above ${bars['trade_loss']:,.0f}."
    return text


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
    """One step on the research scale, $750 on a $50,000 account. The limit itself is year_allowance."""
    size = max(float(account_size), 0.0)
    return round(YEAR_DRAWDOWN_CAP * size / 50_000.0, 2)


def year_allowance(profit: float, account_size: float) -> float:
    """The drawdown allowed once the profit has reached this high. See drawdown_allowance."""
    return drawdown_allowance(profit, account_size)


def profit_grows(spans: dict | None) -> bool:
    """The profit on the last 7 days is above zero, and each longer window of 30, 90, and 365 days made more than the one before.

    A result without its windows passes this rule.
    """
    if not isinstance(spans, dict) or not spans:
        return True
    previous = 0.0
    for days in CONSISTENCY_DAYS:
        window = spans.get(str(days))
        if not isinstance(window, dict):
            return True
        try:
            profit = float(window.get("net_profit"))
        except (TypeError, ValueError):
            return True
        if profit <= previous:
            return False
        previous = profit
    return True


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

    The goal is a profit with drawdown inside the pro account rule.
    Drawdown above that cap counts against the profit. A result outside 130 to 600 trades
    a year stays behind a result inside that band, and it can still move.
    """
    found = numbers(row)
    if found is None or found[2] < 1:
        return None
    profit, drawdown, trades = found
    cap = research_drawdown_cap(account_size)
    distance = profit - max(0.0, drawdown - drawdown_goal(profit, account_size))
    if trades < MIN_TRADES or paced(row) is False:
        distance -= cap
    return distance


def drawdown_goal(profit: float, account_size: float) -> float:
    """The drawdown the search aims under: the larger of $750 on a $50,000 account or 30 percent of the profit.

    This keeps the drawdown low before the limit starts. It guides the search and passes or fails nothing.
    """
    return max(research_drawdown_cap(account_size), PROFIT_DRAWDOWN_SHARE * max(float(profit), 0.0))


def scale_move(previous: dict | None, row: dict | None, account_size: float) -> str:
    """closer, farther, or flat. Flat is no helpful change toward the goal.

    The scale goal is the win rate on the strategy library page, and the profit factor on that page.
    A single trade that loses more than $750 moves away from the goal.
    When both results report a win rate, the marker uses that reading.
    """
    before_numbers, after_numbers = numbers(previous), numbers(row)
    if before_numbers and after_numbers and before_numbers[2] >= 1 and after_numbers[2] < 1:
        # A run that stopped entering has no drawdown, but it is no closer to the goal.
        return "farther"
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
DRAWDOWN_STARTS_PCT = 5.0   # the drawdown limit starts when the profit reaches the allowed drawdown, $2,500 on a $50,000 account
TRADE_LOSS_PCT = 1.5        # one trade, as a share of the account (1.5% of $50,000 is $750)
MAX_TRADE_LOSS = 750.0      # the same single-trade limit on a $50,000 account
TRADE_SAVE_MAX = 900        # a run with more trades than this is never filed; the search cuts trades (learn trade cut)
CONSISTENCY_DAYS = (7, 30, 90, 365)
ACCOUNT_LOSS_CAP = 750.0  # 1.5% of a $50,000 account
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


def drawdown_start(size: float | None = None) -> float:
    """The profit where the drawdown limit starts. A box that is turned off never starts it."""
    if not _drawdown_box_on():
        return float("inf")
    account = _account_size() if size is None else float(size)
    return account * DRAWDOWN_STARTS_PCT / 100.0


_target_seen = {"at": 0.0, "value": None}


def drawdown_target(size: float | None = None) -> float:
    """The headquarters profit target, $3,000 on a $50,000 account, for an account of this size.

    Read at most every 5 seconds, because ranking a search checks the drawdown on every row.
    """
    import time as _time

    account = _account_size()
    now = _time.monotonic()
    target = _target_seen["value"]
    if target is None or now - _target_seen["at"] > 5.0:
        try:
            target = float(profit_target())
        except Exception:
            target = ACCOUNT_PROFIT_LOCK * account / 50_000.0
        _target_seen.update(at=now, value=target)
    if size is None or not account:
        return float(target)
    return float(target) * float(size) / account


def drawdown_allowance(profit: float, size: float | None = None) -> float:
    """The most drawdown allowed from a profit high.

    A new pro account may only draw down the allowed drawdown, $2,500 on $50,000, so that is the limit
    until the profit passes the profit target. Past the target the rules set no limit, but the drawdown
    is kept low: the limit grows by 30 percent of each dollar above the target. $6,000 allows $3,400,
    $10,000 allows $4,600, and $20,000 allows $7,600 on a $50,000 account.
    """
    base = drawdown_start(size)
    if base == float("inf"):
        return base
    target = drawdown_target(size)
    return base + PROFIT_DRAWDOWN_SHARE * max(0.0, float(profit) - target)


def _drawdown_box_on() -> bool:
    try:
        from futuresfund.book import strategy_acceptance

        return bool(strategy_acceptance().get("use_drawdown", True))
    except (TypeError, ValueError, OSError):
        return True


def account_trade_loss_cap(size: float | None = None) -> float:
    """1.5 percent of the account, $750 on $50,000. One trade may not lose more than this, costs included."""
    account = _account_size() if size is None else float(size)
    return account * TRADE_LOSS_PCT / 100.0


def drawdown_share_text() -> str:
    """The drawdown limit, in words."""
    start = drawdown_start()
    if start == float("inf"):
        return "the drawdown box on the strategy library page"
    target = drawdown_target()
    return f"${start:,.0f} until the profit passes ${target:,.0f}, then ${start:,.0f} plus 30 percent of the profit above ${target:,.0f}"


def account_line() -> str:
    """The account rule, in the words the researchers read."""
    share = drawdown_share_text()
    return (
        f"The goal, above everything else, is the pro account rule: the stop is never set above 1.5 percent of the account, and the drawdown stays inside {share}. "
        "Keep the drawdown low before that as well. "
        "There is no maximum on the profit. "
        "The trade count is on as well. "
        "Profit, the profit factor, and the win rate count only when their boxes are checked on the strategy library page. "
        "Set the stop at no more than 1.5 percent of the account, $750 on $50,000. "
        "A fill past a stop set there is the market, and the run still counts."
    )


def drawdown_line() -> str:
    return f"The drawdown is past the limit, {drawdown_share_text()}, so the result is not included."


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
        extras.append(f"Keep the drawdown inside {drawdown_share_text()}.")
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
        parts.append(f"the drawdown stays inside {drawdown_share_text()}")
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
    floor = float(STAGE_TRADES_PER_WEEK or 0)
    pace = f"trade at least {floor:g} times a week and no more than {TRADES_YEAR_MAX:g} times a year" if floor > 0 else f"trade no more than {TRADES_YEAR_MAX:g} times a year"
    return (
        "A strategy is filed when "
        + joined
        + ". Those are the boxes turned on at the strategy library page. "
        + weekly_line(limits)
        + f"It must also make more than its drawdown, {pace}, "
        "make more profit on each of the last 7, 30, 90, and 365 days than on the shorter window before it, "
        "and the stop may not be set above 1.5 percent of the account. A fill past that stop does not count against it."
    )


def weekly_line(limits: dict | None = None) -> str:
    """The library numbers as a week and as each proving stage at a steady pace. Empty when neither box is on."""
    bars = {days: stage_bars(days, limits) for days, _sessions in PROVING_STAGES}
    year = stage_bars(None, limits)
    week = []
    if year["use_profit"]:
        week.append(f"${year['weekly_profit']:,.0f} a week")
    if year["use_trades"]:
        week.append(f"about {_plain_count(year['weekly_trades'])} trades a week")
    if not week:
        return ""
    text = "That is " + " and ".join(week) + " at a steady pace. "
    named = _joined([str(days) for days in bars])
    needs = []
    if year["use_profit"]:
        needs.append("more than " + _joined([f"${bars[days]['profit']:,.0f}" for days in bars]) + " profit")
    needs.append("at least " + _joined([str(bars[days]["min_trades"]) for days in bars]) + " trades")
    return text + f"The {named}-day tests need " + " and ".join(needs) + ". "


def _joined(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


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


def account_survives(row: dict | None, start: float | None = None) -> bool:
    """The pro account rule on the drawdown.

    Each dip is held to the limit at the profit high it fell from (drawdown_allowance): the allowed
    drawdown, start, until the profit passes the profit target, then more as the profit grows. A run
    that records its dips is checked dip by dip. An older run without them is checked at its final
    profit, which is never above its profit high, so it is not let through by mistake.
    There is no maximum on the profit. A missing drawdown is not a breach. One trade down more than
    1.5 percent of the account is a separate rule.
    """
    if not isinstance(row, dict) or row.get("max_drawdown") is None:
        return True
    base = drawdown_start() if start is None else float(start)
    if base == float("inf"):
        return True
    target = drawdown_target()

    def allowed(high: float) -> float:
        return base + PROFIT_DRAWDOWN_SHARE * max(0.0, high - target)

    dips = row.get("dips")
    if isinstance(dips, list) and dips:
        try:
            return all(float(dip) <= allowed(float(high)) + 1e-6 for high, dip in dips)
        except (TypeError, ValueError):
            pass
    try:
        drawdown = float(row["max_drawdown"])
    except (TypeError, ValueError):
        return True
    if drawdown <= 0:
        return True
    try:
        profit = float(row.get("net_profit"))
    except (TypeError, ValueError):
        return False
    return drawdown <= allowed(profit) + 1e-6


def too_many_to_save(row: dict | None) -> bool:
    """A run with more than TRADE_SAVE_MAX trades. It is never filed; the search cuts its trades instead."""
    if not isinstance(row, dict):
        return False
    try:
        return int(row.get("trades") or 0) > TRADE_SAVE_MAX
    except (TypeError, ValueError):
        return False


def trade_pace(row: dict | None, days: int | None = None) -> float | None:
    """A run's trades on a 260-session year. A stage run uses the sessions it covered, or the stage's own."""
    if not isinstance(row, dict):
        return None
    try:
        trades = int(float(row.get("trades") or 0))
    except (TypeError, ValueError):
        return None
    if not days:
        return trades_per_year(row)
    return _stage_yearly(row, trades, {"sessions": stage_sessions(days)})


def over_trade_cap(row: dict | None, days: int | None = None) -> bool:
    """Too many trades to research on: more than TRADE_SAVE_MAX in the run, or on a proving stage a pace
    over TRADES_YEAR_MAX a year. A run like that is kept but can never pass or be filed, and the search
    turns to cutting trades (daily limits, a longer cooldown, most trades a day)."""
    if too_many_to_save(row):
        return True
    if not days:
        return False
    yearly = trade_pace(row, days)
    return yearly is not None and yearly > TRADES_YEAR_MAX


def trade_cap_text(row: dict | None, days: int | None = None) -> str:
    """Why a run is over the trade cap, in one plain sentence."""
    try:
        count = int(float((row or {}).get("trades") or 0))
    except (TypeError, ValueError):
        count = 0
    if days and not too_many_to_save(row):
        yearly = trade_pace(row, days) or 0.0
        return (
            f"took {count:,} trades in the {days}-day test, about {yearly:,.0f} a year. "
            f"The library keeps at most {TRADES_YEAR_MAX:,.0f} trades a year"
        )
    return f"took {count:,} trades in a year. A run with more than {TRADE_SAVE_MAX:,} trades is never filed"


def chart_over_cap(notes: dict | None, root: str = "", timeframe: str = "") -> int:
    """The largest trade count over TRADE_SAVE_MAX already measured on this one chart, or 0.

    Only that instrument and timeframe count, so a 2m count over the cap does not touch the 15m chart
    or another instrument. The trade cut starts at once on a chart that already saw one.
    """
    if not isinstance(notes, dict) or not root or not timeframe:
        return 0
    found = 0

    def take(value) -> None:
        nonlocal found
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            return
        found = max(found, count)

    charts = notes.get("over_trade_charts")
    if isinstance(charts, dict):
        take(charts.get(f"{root}|{timeframe}"))
    record = ((notes.get("instruments") or {}).get(root) or {}) if isinstance(notes.get("instruments"), dict) else {}
    chart = ((record.get("charts") or {}).get(timeframe) or {}) if isinstance(record, dict) and isinstance(record.get("charts"), dict) else {}
    if isinstance(chart, dict):
        take(chart.get("trades"))
        best = chart.get("best")
        if isinstance(best, dict):
            take(best.get("trades"))
        for row in chart.get("trials") or []:
            if isinstance(row, dict):
                take(row.get("trades"))
    return found if found > TRADE_SAVE_MAX else 0


def trades_already_over_cap(notes: dict | None) -> int:
    """The largest trade count over TRADE_SAVE_MAX anywhere in the notes, or 0.

    Information only: no chart refuses a script on it any more. chart_over_cap reads one chart.
    """
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
        held = []
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            when = _trade_exit(trade)
            if when is None or when < start:
                continue
            try:
                pnls.append(float(trade["pnl"]))
                held.append(pnls[-1] + float(trade.get("jump") or 0.0))
            except (TypeError, ValueError, KeyError):
                continue
        report[str(days)] = {
            "net_profit": round(sum(pnls), 2) if pnls else 0.0,
            "trades": len(pnls),
            "biggest_loser": round(min(pnls), 2) if pnls else None,
            "held_loser": round(min(held), 2) if held else None,
        }
    return report


def consistent_spans(spans: dict | None) -> bool:
    """Each of 7, 30, 90, and 365 days made money. The stop is checked on the whole run."""
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
        if count < 1 or profit <= 0:
            return False
    return True


def held_loss(row: dict | None):
    """The largest loss without the part where price jumped past the stop. An older result only has the full loss."""
    if not isinstance(row, dict):
        return None
    return row["held_loser"] if row.get("held_loser") is not None else row.get("biggest_loser")


_STOP_INPUT = re.compile(r"(?m)^\s*stop_dollars\s*=\s*input(?:\.\w+)?\(\s*(?:defval\s*=\s*)?(-?\d+(?:\.\d+)?)")


def stop_in_script(text: str | None) -> float | None:
    """The stop_dollars default in the script as it ran. Every research run carries that input."""
    match = _STOP_INPUT.search(text or "")
    return float(match.group(1)) if match else None


def stop_setting(row: dict | None) -> float | None:
    """The dollar stop a run was set to. A newer run records it.

    An older run has it in its settings when an attempt moved it. The engine ran
    a setting above $700 at $700, so that is the stop it had.
    """
    if not isinstance(row, dict):
        return None
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    for value, capped in ((row.get("stop_dollars"), False), (params.get("stop_dollars"), True)):
        if value is None or isinstance(value, bool):
            continue
        try:
            stop = float(value)
        except (TypeError, ValueError):
            continue
        if capped:
            from futuresfund.risk_style import STOP_CAP

            stop = min(stop, STOP_CAP)
        return stop
    return None


def trade_loss_ok(row: dict | None, cap: float | None = None) -> bool:
    """The stop is set at no more than 1.5 percent of the account, $750 on $50,000.

    With the stop set there, a trade that lost more was filled past the stop. That is
    the market, not the script, and it does not fail the run. A run that does not
    record its stop is held to its largest loss without a price jump past the stop,
    and a missing loss is not treated as a huge one.
    """
    limit = account_trade_loss_cap() if cap is None else cap
    stop = stop_setting(row)
    if stop is not None:
        return stop <= limit + 1e-9
    if held_loss(row) is None:
        return True
    try:
        loss = float(held_loss(row))
    except (TypeError, ValueError):
        return True
    return loss >= -limit


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


def _stage_yearly(row: dict, trades: int, bars: dict) -> float | None:
    """A stage run's trades on a 260-session year. The engine records the sessions the run covered; without
    them the stage's own sessions stand in, never the year chart's."""
    try:
        sessions = float(row.get("days")) if row.get("days") is not None else 0.0
    except (TypeError, ValueError):
        sessions = 0.0
    if sessions <= 0:
        sessions = float(bars["sessions"])
    if sessions <= 0:
        return None
    return round(trades / sessions * RESEARCH_SESSIONS, 2)


def acceptance_misses(row: dict | None, *, days: int | None = None, limits: dict | None = None, start: float | None = None, loss_cap: float | None = None) -> dict:
    """The one acceptance check. Every table, filing, email, and count reads its pass or fail from here.

    Returns what the result misses, each with its number. Empty means it passes.
    The boxes on the strategy library page turn the drawdown, trades, profit, profit factor, and
    win rate on and off. A profit and the loss on one trade are always checked, and so is the
    most trades: 600 a year at the run's pace. The fewest trades are the library trade count, never
    fewer than four on a year, plus the optional STAGE_TRADES_PER_WEEK floor when it is above 0.
    A full result must also make more than its drawdown and make more profit on each of the last
    7, 30, 90, and 365 days than on the shorter window before it.

    days is a proving stage on that many days. stage_bars(days) pro-rates the profit and the trade
    numbers to it, and the last two rules wait for the full result because a few trades cannot show
    them. The caller passes days; a row's own stage_days is never read here, so a stage row judged
    without days is judged as a year. limits, start, and loss_cap let a report over many rows read
    the book once.
    """
    if not isinstance(row, dict) or any(row.get(name) for name in ("error", "fatal", "stopped", "blocked")):
        return {"error": None}
    if limits is None:
        from futuresfund.book import strategy_acceptance

        limits = strategy_acceptance()
    days = _window_days(days)
    bars = stage_bars(days, limits)
    missed: dict = {}
    try:
        profit = float(row.get("net_profit"))
    except (TypeError, ValueError):
        profit = None
        missed["profit"] = None
    if profit is not None and (profit <= 0 or (bars["use_profit"] and profit <= bars["profit"])):
        missed["profit"] = profit
    if not trade_loss_ok(row, loss_cap):
        stop = stop_setting(row)
        missed["trade_loss"] = stop if stop is not None else abs(float(held_loss(row)))
    if limits.get("use_drawdown") and not account_survives(row, start):
        try:
            missed["drawdown"] = float(row.get("max_drawdown"))
        except (TypeError, ValueError):
            missed["drawdown"] = None
    try:
        trades = int(float(row.get("trades") or 0))
    except (TypeError, ValueError):
        trades = None
    if trades is None:
        if bars["use_trades"] or days:
            missed["trades"] = None
    elif days:
        if trades < bars["min_trades"]:
            missed["trades"] = float(trades)
    else:
        # The year counts the library number (never under four trades). The optional weekly floor is a
        # rate, so a year measured on more or fewer sessions than 260 is held to the same pace.
        weekly = None
        fewest = max(bars["library_trades"], bars["least_trades"])
        if bars["floor_week"] > 0:
            sessions = days_for(row)
            weekly = trades / sessions * 5 if sessions else None
            if weekly is None:
                # A year with no session count is held to the floor's count over 260 sessions.
                fewest = max(fewest, bars["floor_trades"])
        if trades < fewest or (
            weekly is not None and weekly + 1e-9 < bars["floor_week"]
        ):
            missed["trades"] = float(trades)
    if trades is not None:
        yearly = trades_per_year(row) if not days else _stage_yearly(row, trades, bars)
        if yearly is not None and yearly > TRADES_YEAR_MAX:
            missed["pace"] = float(yearly)
    if limits.get("use_factor"):
        factor = profit_factor_of(row)
        if factor is None or factor <= float(limits["profit_factor"]):
            missed["factor"] = factor
    if limits.get("use_win_rate"):
        rate = win_rate_percent(row)
        if rate is None or rate <= float(limits["win_rate"]):
            missed["win_rate"] = rate
    if not days:
        try:
            drawdown = float(row.get("max_drawdown") or 0)
        except (TypeError, ValueError):
            drawdown = 0.0
        if profit is not None and drawdown > 0 and profit <= drawdown:
            missed["under_drawdown"] = drawdown
        if not profit_grows(row.get("spans")):
            missed["growth"] = None
    return missed


def passes_acceptance(row: dict | None) -> bool:
    """True when the full result misses nothing."""
    return not acceptance_misses(row)


def _dollars(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "not measured"
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.0f}"


def _trades_source(bars: dict) -> str:
    """Where a window's fewest trades come from, in words."""
    fewest = bars["min_trades"]
    if bars["floor_trades"] and bars["floor_trades"] >= fewest and bars["floor_trades"] > bars["library_trades"]:
        return f"{bars['floor_week']:g} a week, the floor set on the Research workflow page"
    if bars["library_trades"] and bars["library_trades"] >= fewest:
        return f"{bars['year_trades']} a year at a steady pace"
    return f"at least {MIN_TRADES} a year, because two or three trades is not a strategy"


def acceptance_reason(row: dict | None, days: int | None = None, *, misses: dict | None = None, limits: dict | None = None) -> str:
    """The first thing this result misses, as a sentence that names the bar it was held to. Empty when it passes.

    days is a proving stage. Its sentence gives that stage's numbers from stage_bars, for example
    "needs more than $115 profit in 7 days (a $6,000 year at a steady pace)". misses lets a caller that
    already ran acceptance_misses pass the answer in.
    """
    if limits is None:
        from futuresfund.book import strategy_acceptance

        limits = strategy_acceptance()
    days = _window_days(days)
    missed = acceptance_misses(row, days=days, limits=limits) if misses is None else misses
    best = row if isinstance(row, dict) else {}
    bars = stage_bars(days, limits)
    if days:
        return _stage_reason(missed, best, bars)
    if "error" in missed:
        return "The backtest did not finish, so this strategy was not included."
    if "trade_loss" in missed:
        if stop_setting(best) is not None:
            return f"The stop is set at ${missed['trade_loss']:,.0f}, more than 1.5 percent of the account (${account_trade_loss_cap():,.0f}), so this strategy was not included."
        return "One trade lost more than 1.5 percent of the account, so this strategy was not included."
    if "drawdown" in missed:
        return f"The drawdown is past the limit, {drawdown_share_text()}, so this strategy was not included."
    if "under_drawdown" in missed:
        return f"The drawdown of {best.get('max_drawdown')} is not less than the profit of {best.get('net_profit')}, so this strategy was not included."
    if "pace" in missed:
        return f"The library keeps at most {TRADES_YEAR_MAX:g} trades a year. This run is {missed['pace']:g} trades a year."
    if "trades" in missed:
        count = int(best.get("trades") or 0)
        if count < 3:
            return "Two trades is not a strategy that can be tried in the market, so it was not filed."
        # The same unrounded rate acceptance_misses tests, so the sentence never names a bar the run clears.
        sessions = days_for(best) if bars["floor_week"] > 0 else None
        weekly = count / sessions * 5 if sessions else None
        if weekly is not None and weekly + 1e-9 < bars["floor_week"]:
            return f"The library keeps a strategy that trades at least {bars['floor_week']:g} times a week. This run trades {weekly:.2f} times a week, so it was not filed."
        needed = max(bars["library_trades"], bars["least_trades"], bars["floor_trades"] if weekly is None else 0)
        return f"The library keeps a strategy after at least {needed} trades. This run has {count}, so it was not filed."
    if "profit" in missed:
        if not bars["use_profit"]:
            return f"The run made {_dollars(best.get('net_profit'))}. The library keeps only a profit above $0, so it was not filed."
        return f"The library keeps a profit over ${bars['year_profit']:,.0f}. This run is under that."
    if "factor" in missed:
        shown = best.get("profit_factor")
        return f"The library keeps a profit factor above {bars['profit_factor']:g}. This run's profit factor is {shown if shown is not None else 'not measured'}."
    if "win_rate" in missed:
        shown = best.get("win_rate")
        return f"The library keeps a win rate above {bars['win_rate']:g} percent. This run's win rate is {shown if shown is not None else 'not measured'}."
    if "growth" in missed:
        return "The profit did not grow from the last 7 days to the last 30, 90, and 365 days, so this strategy was not included."
    return ""


def _stage_reason(missed: dict, best: dict, bars: dict) -> str:
    """The first miss on a proving stage, with that stage's pro-rated number."""
    lines = _stage_lines(missed, best, bars)
    return lines[0] if lines else ""


def stage_miss_lines(row: dict | None, days: int, limits: dict | None = None, misses: dict | None = None) -> list[str]:
    """Every bar a stage run missed, one sentence each, with that stage's numbers. Empty when it passes."""
    if limits is None:
        from futuresfund.book import strategy_acceptance

        limits = strategy_acceptance()
    days = _window_days(days)
    missed = acceptance_misses(row, days=days, limits=limits) if misses is None else misses
    return _stage_lines(missed, row if isinstance(row, dict) else {}, stage_bars(days, limits))


def _stage_lines(missed: dict, best: dict, bars: dict) -> list[str]:
    name = bars["name"]
    span = _window_words(bars["days"])
    try:
        count = int(float(best.get("trades") or 0))
    except (TypeError, ValueError):
        count = 0
    lines = []
    if "error" in missed:
        return [f"The backtest did not finish, so it did not pass the {name}."]
    if "trade_loss" in missed:
        if stop_setting(best) is not None:
            lines.append(f"The stop is set at ${missed['trade_loss']:,.0f}, more than 1.5 percent of the account (${bars['trade_loss']:,.0f}), so it did not pass the {name}.")
        else:
            lines.append(f"One trade lost more than 1.5 percent of the account (${bars['trade_loss']:,.0f}), so it did not pass the {name}.")
    same = "" if bars["year"] else ", the same as the year"
    if "drawdown" in missed:
        if bars["year"]:
            lines.append(f"The drawdown went past the limit, {drawdown_share_text()}, so it did not pass the year.")
        else:
            lines.append(
                f"The drawdown went past the limit, {drawdown_share_text()}. The {name} is held to the same limit as the year, "
                f"because the drawdown of {span} can never be larger than the drawdown of the year that holds it."
            )
    if "under_drawdown" in missed:
        lines.append(
            f"The year needs more profit than drawdown. This run made {_dollars(best.get('net_profit'))} "
            f"with a drawdown of {_dollars(best.get('max_drawdown'))}."
        )
    if "pace" in missed:
        lines.append(
            f"The {name} allows at most {bars['max_trades']} trades in {span} ({TRADES_YEAR_MAX:g} a year at a steady pace). "
            f"This run took {count}, about {missed['pace']:,.0f} a year."
        )
    if "trades" in missed:
        lines.append(f"The {name} needs at least {bars['min_trades']} trades in {span} ({_trades_source(bars)}). This run took {count}.")
    if "profit" in missed:
        made = _dollars(best.get("net_profit"))
        if not bars["use_profit"]:
            lines.append(f"The {name} needs a profit above $0. This run made {made}.")
        else:
            lines.append(
                f"The {name} needs more than ${bars['profit']:,.0f} profit in {span} (a ${bars['year_profit']:,.0f} year at a steady pace). "
                f"This run made {made}."
            )
    if "factor" in missed:
        shown = best.get("profit_factor")
        lines.append(f"The {name} needs a profit factor above {bars['profit_factor']:g}{same}. This run's profit factor is {shown if shown is not None else 'not measured'}.")
    if "win_rate" in missed:
        shown = best.get("win_rate")
        lines.append(f"The {name} needs a win rate above {bars['win_rate']:g} percent{same}. This run's win rate is {shown if shown is not None else 'not measured'}.")
    if "growth" in missed:
        lines.append("The year needs more profit on each of the last 7, 30, 90, and 365 days than on the shorter window before it. This run's profit did not grow that way.")
    return lines

MISS_NAMES = {
    "error": "did not finish",
    "profit": "profit",
    "trade_loss": "the stop on one trade",
    "drawdown": "drawdown",
    "under_drawdown": "profit under the drawdown",
    "trades": "trade count",
    "pace": "trades a year",
    "factor": "profit factor",
    "win_rate": "win rate",
    "growth": "profit growing over 7, 30, 90, and 365 days",
}


def acceptance_summary(row: dict | None) -> str:
    """What the tables show for a result that fails. Empty when it passes."""
    return ", ".join(MISS_NAMES.get(name, name) for name in acceptance_misses(row))


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
    trade may lose more than $750. A result that does not report a win rate
    still uses its profit factor.
    """
    found = numbers(row)
    if found is None or found[2] < 1:
        return 0.0
    limited = _drawdown_goal_scale(found[1], account_size, found[0])
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
    score = profit - max(0.0, drawdown - drawdown_goal(profit, account_size))
    if score < 0:
        weight = abs(score) / (abs(score) + cap * 0.25)
        return _scale_for_trade_count(row, round(0.45 * (1.0 - weight), 4))
    if score == 0:
        return _scale_for_trade_count(row, 0.5)
    weight = score / (score + cap)
    return _scale_for_trade_count(row, round(min(0.99, 0.5 + 0.49 * weight), 4))


def _past_drawdown_goal(row: dict | None, account_size: float) -> bool:
    """True when this result's drawdown is past the goal the search aims under. A box that is off sets no goal."""
    found = numbers(row)
    if found is None or not _drawdown_box_on():
        return False
    cap = drawdown_goal(found[0], account_size)
    return cap > 0 and found[1] > cap


def _drawdown_goal_scale(drawdown: float, account_size: float, profit: float = 0.0) -> float | None:
    """None when the drawdown is inside the goal the search aims under.

    Past that line the marker stays below the consistency scores. Closer to the
    line is higher, and it never reaches 0.5.
    """
    cap = drawdown_goal(profit, account_size)
    if not _drawdown_box_on() or cap <= 0 or drawdown <= cap:
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
    """A run the chart can stop on: one that passes acceptance."""
    return passes_acceptance(row)


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
    """A drawdown no more than 30 percent of the profit, and the trade count is not low.

    The trade count is the year's library count from stage_bars (never under four trades), and more
    than 600 trades a year does not count when the span is known. Unknown days are not held against
    the run. The 130-a-year band only steers the search; it does not decide this.
    """
    found = numbers(row)
    if found is None:
        return False
    profit, _drawdown, trades = found
    try:
        fewest = max(stage_bars(None)["min_trades"], MIN_TRADES)
    except Exception:
        fewest = MIN_TRADES
    if profit <= 0 or trades < fewest:
        return False
    yearly = trades_per_year(row)
    if yearly is not None and yearly > TRADES_YEAR_MAX + 1e-9:
        return False
    ratio = profit_to_drawdown(row)
    return ratio is not None and ratio + 1e-9 >= RATIO_FLOOR


def counted_success(row: dict | None, account_size: float) -> bool:
    """A run the engine counts: one that passes acceptance."""
    return passes_acceptance(row)


def within_drawdown(row: dict | None) -> bool:
    """The drawdown is inside the pro account rule."""
    found = numbers(row)
    if found is None:
        return False
    return found[1] <= year_allowance(found[0], float(limits()["size"]))


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
    """Hit the target with drawdown inside the pro account rule, at 130 to 600 trades a year."""
    return tier(row) == TIER_COUNTS


def research_rank(row: dict | None) -> tuple:
    """Search order on the full chart. Higher is better.

    The headquarters profit target and the trailing-drawdown cap are not used.
    A profit counts at 130 to 600 trades a year. Fewer than 130 is too quiet and more than 600 is too many.
    Two or three trades stays at the bottom. A trade that loses more than $750 stays off the list.
    A run outside that band ranks below every run inside it, and the one closer to the band ranks higher.
    Drawdown inside the pro account rule comes first. A result past that rule ranks below
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
            loss = float(held_loss(row))
        except (TypeError, ValueError):
            loss = -account_trade_loss_cap()
        return (0, loss, profit, 0.0, 0, 0)
    under = 1 if account_survives(row) else 0
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
        f"The research objective: keep the drawdown inside {drawdown_share_text()}, and meet the trade count. "
        "Profit, the profit factor, and the win rate count only when their boxes are checked. "
        "Guideline 1 is to flatten every open trade at 16:45 New York. "
        "A trade left open after 16:45 can liquidate the account. "
        f"The drawdown may not exceed {drawdown_share_text()}. There is no maximum on the profit. "
        "The stop is never set above 1.5 percent of the account. "
        f"{goal_line()} {acceptance_line()} "
        "That acceptance line is the bar for every agent and every report. "
        "The take profit is larger than the stop loss. "
        "A strategy is not included when its stop is set above 1.5 percent of the account. A fill past a stop set inside it does not count. "
        f"The headquarters research target is the ${rules['profit_target']:,.0f} profit target "
        f"set on headquarters and the one-year drawdown is no higher than {drawdown_share_text()} on a ${rules['size']:,.0f} account, "
        f"while taking at least the library trade count and no more than {TRADES_YEAR_MAX:g} trades a year, pro-rated on the 7, 30, and 90-day tests. "
        f"More than {TRADES_YEAR_MAX:g} a year is too many trades. "
        f"The search ranking prefers {TRADES_YEAR_MIN:g} to {TRADES_YEAR_MAX:g} trades a year; that preference steers the search and does not pass or fail a run. "
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
        f"Take profit larger than the stop, the stop no more than 1.5 percent of the account, at {pace_text()}."
    )


def stamp() -> str:
    """Names the objective and the headquarters figures it reads. When either changes, the notes are ranked again
    and the library is folded again at the next start."""
    from futuresfund.library import OBJECTIVE_TAG

    return f"{OBJECTIVE_TAG}@{profit_target():.0f}/{drawdown_limit():.0f}/{PACE_WEEK_MIN:g}-{PACE_WEEK_MAX:g}w"
