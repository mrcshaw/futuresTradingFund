"""Chart timeframe. TradingView sends {{interval}} as minutes, or D for a day."""

from __future__ import annotations

from datetime import datetime

_MINUTES = {
    "1": "1m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "240": "4h",
    "d": "1d",
    "1d": "1d",
    "1day": "1d",
    "daily": "1d",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "60m": "1h",
    "4h": "4h",
}

# Yahoo's intraday history is shorter than its daily history.
_YAHOO = {
    "1m": ("1m", 7, 1),
    "5m": ("5m", 59, 1),
    "15m": ("15m", 59, 1),
    "30m": ("30m", 59, 1),
    "1h": ("60m", 729, 1),
    "4h": ("60m", 729, 4),
    "1d": ("1d", 365, 1),
}

LABELS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")

_BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def parse_timeframe(value) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("Set a timeframe. TradingView can send timeframe={{interval}}.")
    label = _MINUTES.get(text)
    if not label:
        allowed = ", ".join(LABELS)
        raise ValueError(f"Timeframe {value!r} is not one of {allowed}.")
    return label


def bar_minutes(value) -> int:
    """How many minutes one candle of this timeframe covers."""
    return _BAR_MINUTES[parse_timeframe(value)]


def bucket_open(stamp: str, timeframe: str) -> str:
    """The strategy candle that contains this alert time. Times are the bar open, in UTC."""
    minutes = bar_minutes(timeframe)
    text = str(stamp)[:19]
    parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    total = parsed.hour * 60 + parsed.minute
    floored = (total // minutes) * minutes
    hour, minute = divmod(floored, 60)
    return f"{parsed:%Y-%m-%d}T{hour:02d}:{minute:02d}:00"


def yahoo_request(timeframe: str, lookback_days: int) -> tuple[str, str, int]:
    """Return Yahoo interval, period, and how many of those bars make one strategy bar."""
    interval, cap, group = _YAHOO[parse_timeframe(timeframe)]
    days = max(1, min(int(lookback_days), cap))
    return interval, f"{days}d", group
