"""Backtest the desk's direction on the chosen bar size. This does not call the model once per bar."""

from __future__ import annotations

from futuresfund.contracts import POINT_VALUE, root_of
from futuresfund.crosstrade import trade_side

_FETCH = {
    "1m": ("1m", "7d", None),
    "5m": ("5m", "60d", None),
    "15m": ("15m", "60d", None),
    "30m": ("30m", "60d", None),
    "1h": ("60m", "730d", None),
    "4h": ("60m", "730d", "4h"),
    "1D": ("1d", "2y", None),
}


def score_closes(closes: list[float], direction: str, qty: int, point_value: float) -> dict:
    """Hold the desk's side from the first close to the last. Hold scores as flat."""
    usable = [float(price) for price in closes if price == price]
    side = trade_side(direction)
    if side is None:
        return {
            "bars": len(usable),
            "side": None,
            "points": 0.0,
            "pnl": 0.0,
            "bar_win_rate": None,
            "max_drawdown_points": 0.0,
            "note": "Hold takes no position, so the backtest is flat.",
        }
    if len(usable) < 2:
        raise ValueError("The backtest needs at least two bars.")
    sign = 1 if side == "BUY" else -1
    changes = [(newer - older) * sign for older, newer in zip(usable, usable[1:])]
    points = sum(changes)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for change in changes:
        equity += change
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    wins = sum(1 for change in changes if change > 0)
    return {
        "bars": len(usable),
        "side": side,
        "points": round(points, 4),
        "pnl": round(points * point_value * qty, 2),
        "bar_win_rate": round(wins / len(changes), 4),
        "max_drawdown_points": round(max_dd, 4),
        "note": f"Held {side} across {len(usable)} bars, from {usable[0]:.4g} to {usable[-1]:.4g}.",
    }


def load_closes(symbol: str, timeframe: str, bars: int) -> list[float]:
    interval, period, resample = _FETCH[timeframe]
    import yfinance as yf

    history = yf.Ticker(symbol).history(period=period, interval=interval)
    if history is None or history.empty:
        raise ValueError(f"No {timeframe} bars came back for {symbol}.")
    if resample:
        history = history.resample(resample).agg({"Close": "last"}).dropna()
    closes = [float(price) for price in history["Close"].tolist()]
    if len(closes) < 2:
        raise ValueError(f"Not enough {timeframe} bars to backtest {symbol}.")
    return closes[-bars:]


def backtest_direction(symbol: str, instrument: str, timeframe: str, direction: str, qty: int, bars: int) -> dict:
    closes = load_closes(symbol, timeframe, bars)
    try:
        value = POINT_VALUE[root_of(instrument)]
    except (ValueError, KeyError):
        value = 1.0
    result = score_closes(closes, direction, qty, value)
    result["timeframe"] = timeframe
    result["symbol"] = symbol
    return result
