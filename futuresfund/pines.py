"""Original Pine strategies. One uses a point-of-control pullback. The others do not."""

from __future__ import annotations


def render(name: str, params: dict, title: str, point_value: float, capital: float) -> str:
    header = (
        "//@version=5\n"
        f'strategy("{title}", overlay=true, initial_capital={capital:.0f}, '
        "default_qty_type=strategy.fixed, default_qty_value=1, "
        f"commission_type=strategy.commission.cash_per_contract, commission_value={max(point_value, 1):.2f}, "
        "pyramiding=0, process_orders_on_close=true)\n"
    )
    if name == "poc_pullback":
        body = _poc(int(params["lookback"]), int(params["rows"]))
    elif name == "ema_cross":
        body = _ema(int(params["fast"]), int(params["slow"]))
    elif name == "donchian":
        body = _donchian(int(params["length"]))
    elif name == "rsi_revert":
        body = _rsi(int(params["period"]), float(params["low"]), float(params["high"]))
    elif name == "vwap_side":
        body = _vwap(int(params["length"]))
    elif name == "bollinger":
        body = _bollinger(int(params["length"]), float(params["dev"]))
    elif name == "macd":
        body = _macd(int(params["fast"]), int(params["slow"]), int(params["signal"]))
    else:
        body = "// This rule has no Pine form yet.\n"
    return header + body + _stop(params, point_value)


def _poc(lookback: int, rows: int) -> str:
    return f"""
lookback = {lookback}
rows = {rows}
average = ta.vwma(close, 20)
hi = ta.highest(high, lookback)
lo = ta.lowest(low, lookback)
span = hi - lo
step = span > 0 ? span / rows : na
var bins = array.new_float(rows, 0.0)
float bandLow = na
float bandHigh = na
if not na(step) and span > 0
    for i = 0 to rows - 1
        array.set(bins, i, 0.0)
    for ago = 0 to lookback - 1
        weight = volume[ago] > 0 ? volume[ago] : 1.0
        start = math.max(0, math.min(rows - 1, int((low[ago] - lo) / step)))
        stop = math.max(0, math.min(rows - 1, int((high[ago] - lo) / step)))
        share = weight / (stop - start + 1)
        for row = start to stop
            array.set(bins, row, array.get(bins, row) + share)
    int best = 0
    float most = 0.0
    for row = 0 to rows - 1
        if array.get(bins, row) > most
            most := array.get(bins, row)
            best := row
    bandLow := lo + best * step
    bandHigh := lo + (best + 1) * step
longPull = not na(bandHigh) and close > bandHigh and low <= bandHigh and close > open and close > average
shortPull = not na(bandLow) and close < bandLow and high >= bandLow and close < open and close < average
if longPull
    strategy.entry("Long", strategy.long)
if shortPull
    strategy.entry("Short", strategy.short)
plot(average, "Volume average")
plot(bandHigh, "Contact high")
plot(bandLow, "Contact low")
"""


def _ema(fast: int, slow: int) -> str:
    return f"""
fast = ta.ema(close, {fast})
slow = ta.ema(close, {slow})
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.entry("Short", strategy.short)
plot(fast, "Fast average")
plot(slow, "Slow average")
"""


def _donchian(length: int) -> str:
    return f"""
upper = ta.highest(high, {length})[1]
lower = ta.lowest(low, {length})[1]
if close > upper
    strategy.entry("Long", strategy.long)
if close < lower
    strategy.entry("Short", strategy.short)
plot(upper, "Breakout high")
plot(lower, "Breakout low")
"""


def _rsi(period: int, low: float, high: float) -> str:
    return f"""
strength = ta.rsi(close, {period})
if strength < {low}
    strategy.entry("Long", strategy.long)
if strength > {high}
    strategy.entry("Short", strategy.short)
if strategy.position_size > 0 and strength > 50
    strategy.close("Long")
if strategy.position_size < 0 and strength < 50
    strategy.close("Short")
plot(strength, "RSI")
"""


def _bollinger(length: int, dev: float) -> str:
    return f"""
basis = ta.sma(close, {length})
band = ta.stdev(close, {length})
upper = basis + {dev} * band
lower = basis - {dev} * band
if close < lower
    strategy.entry("Long", strategy.long)
if close > upper
    strategy.entry("Short", strategy.short)
if strategy.position_size > 0 and close > basis
    strategy.close("Long")
if strategy.position_size < 0 and close < basis
    strategy.close("Short")
plot(basis, "Average")
plot(upper, "Upper band")
plot(lower, "Lower band")
"""


def _macd(fast: int, slow: int, signal: int) -> str:
    return f"""
macd = ta.ema(close, {fast}) - ta.ema(close, {slow})
sig = ta.ema(macd, {signal})
if ta.crossover(macd, sig)
    strategy.entry("Long", strategy.long)
if ta.crossunder(macd, sig)
    strategy.entry("Short", strategy.short)
plot(macd, "MACD")
plot(sig, "Signal")
"""


def _stop(params: dict, point_value: float) -> str:
    raw = params.get("stop") if isinstance(params, dict) else None
    try:
        dollars = float(raw)
    except (TypeError, ValueError):
        return ""
    if dollars <= 0 or point_value <= 0:
        return ""
    ticks = max(1, int(round(dollars / (point_value * 0.25))))
    return f"""
if strategy.position_size > 0
    strategy.exit("Stop long", "Long", loss={ticks})
if strategy.position_size < 0
    strategy.exit("Stop short", "Short", loss={ticks})
"""


def _vwap(length: int) -> str:
    return f"""
typical = hlc3
vol = volume > 0 ? volume : 1.0
weighted = math.sum(typical * vol, {length}) / math.sum(vol, {length})
if ta.crossover(close, weighted)
    strategy.entry("Long", strategy.long)
if ta.crossunder(close, weighted)
    strategy.entry("Short", strategy.short)
plot(weighted, "Rolling VWAP")
"""
