"""Backtest the timeframe the alerts will use. One bar is one interval."""

from __future__ import annotations

from futuresfund.timeframe import parse_timeframe, yahoo_request


def simulate_crossover(closes: list[float], fast: int = 8, slow: int = 21) -> dict:
    """Long when the fast average crosses above the slow one, short on the cross down."""
    if fast >= slow:
        raise ValueError("The fast average must be shorter than the slow average.")
    if len(closes) < slow + 2:
        raise ValueError(f"This timeframe needs at least {slow + 2} bars to backtest. There are {len(closes)}.")
    fast_avg = _ema(closes, fast)
    slow_avg = _ema(closes, slow)
    position = 0
    entry = None
    trades: list[float] = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for index in range(slow, len(closes)):
        crossed_up = fast_avg[index - 1] <= slow_avg[index - 1] and fast_avg[index] > slow_avg[index]
        crossed_down = fast_avg[index - 1] >= slow_avg[index - 1] and fast_avg[index] < slow_avg[index]
        signal = 1 if crossed_up else -1 if crossed_down else 0
        if not signal or signal == position:
            continue
        if position and entry is not None:
            equity, peak, max_dd = _close(trades, equity, peak, max_dd, (closes[index] - entry) * position)
        position = signal
        entry = closes[index]
    if position and entry is not None:
        equity, peak, max_dd = _close(trades, equity, peak, max_dd, (closes[-1] - entry) * position)
    wins = sum(1 for trade in trades if trade > 0)
    return {
        "rule": "8/21 average crossover. Long on a cross up, short on a cross down, one position at a time.",
        "bars": len(closes),
        "trades": len(trades),
        "wins": wins,
        "win_rate": round(wins / len(trades), 4) if trades else None,
        "net_points": round(equity, 2),
        "max_drawdown_points": round(max_dd, 2),
    }


def fetch_closes(yahoo: str, timeframe: str, lookback_days: int) -> list[float]:
    import yfinance as yf

    interval, period, group = yahoo_request(timeframe, lookback_days)
    history = yf.Ticker(yahoo).history(period=period, interval=interval)
    if history is None or history.empty or "Close" not in history:
        raise ValueError(f"No {parse_timeframe(timeframe)} bars came back for {yahoo}.")
    closes = [float(value) for value in history["Close"].dropna().tolist()]
    if group > 1:
        closes = [closes[index] for index in range(group - 1, len(closes), group)]
    return closes


def describe(timeframe: str, result: dict) -> str:
    rate = "n/a" if result["win_rate"] is None else f"{result['win_rate']:.0%}"
    return (
        f"{timeframe} backtest, {result['bars']} bars, {result['trades']} trades, "
        f"win rate {rate}, net {result['net_points']} points, "
        f"drawdown {result['max_drawdown_points']} points. {result['rule']}"
    )


def _ema(values: list[float], length: int) -> list[float]:
    weight = 2 / (length + 1)
    average = values[0]
    series = []
    for value in values:
        average = value * weight + average * (1 - weight)
        series.append(average)
    return series


def _close(trades: list[float], equity: float, peak: float, max_dd: float, pnl: float):
    trades.append(pnl)
    equity += pnl
    peak = max(peak, equity)
    max_dd = max(max_dd, peak - equity)
    return equity, peak, max_dd
