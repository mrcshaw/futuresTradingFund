"""TradingView bar fills, used to check the desk against a known tester report.

Market orders fill at the bar close. A stop is placed at that close and can
fill on a later bar when price trades through it. One tick of slippage is
added against the fill. Commission is cash per contract, charged on the
entry and the exit. On ES that round trip is $36 when the two closes match:
two ticks at $12.50 plus $5.50 each side.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .contracts import POINT_VALUE, tick_size

ET = ZoneInfo("America/New_York")


def to_tick(price: float, tick: float) -> float:
    """Round an order price to the contract's minimum increment."""
    steps = math.floor(price / tick + 0.5)
    return round(steps * tick, 10)

# Defaults from the POC pullback tester run: 1 ES, $25,000, $5.50 per fill, 1 tick.
COMMISSION = 5.50
SLIPPAGE_TICKS = 1
QTY = 1
CAPITAL = 25000.0


def market_fill(price: float, action: str, tick: float, slippage_ticks: int = SLIPPAGE_TICKS) -> float:
    """Buy fills higher and sell fills lower, by `slippage_ticks`."""
    worse = tick * slippage_ticks
    if action == "buy":
        return price + worse
    if action == "sell":
        return price - worse
    raise ValueError(action)


def stop_touched(side: int, stop: float, bar: dict) -> bool:
    if side > 0:
        return bar["l"] <= stop
    return bar["h"] >= stop


def stop_fill(side: int, stop: float, bar: dict, tick: float, slippage_ticks: int = SLIPPAGE_TICKS) -> float:
    """Exit a position. A gap through the stop fills at the open, then slippage."""
    action = "sell" if side > 0 else "buy"
    if side > 0:
        raw = bar["o"] if bar["o"] <= stop else stop
    else:
        raw = bar["o"] if bar["o"] >= stop else stop
    return market_fill(raw, action, tick, slippage_ticks)


def trade_net(side: int, entry: float, exit_fill: float, point: float, qty: int = QTY,
              commission: float = COMMISSION) -> float:
    """Price result minus commission on both fills."""
    return (exit_fill - entry) * side * point * qty - commission * 2 * qty


def _wilder_atr(bars: list[dict], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    window: list[float] = []
    rma = None
    prev = None
    for i, bar in enumerate(bars):
        if prev is None:
            tr = bar["h"] - bar["l"]
        else:
            tr = max(bar["h"] - bar["l"], abs(bar["h"] - prev), abs(bar["l"] - prev))
        prev = bar["c"]
        window.append(tr)
        if rma is None:
            if len(window) == length:
                rma = sum(window) / length
                out[i] = rma
        else:
            rma = (rma * (length - 1) + tr) / length
            out[i] = rma
    return out


def _vwma(bars: list[dict], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for i in range(length - 1, len(bars)):
        chunk = bars[i - length + 1:i + 1]
        weight = sum(bar["v"] for bar in chunk)
        if weight > 0:
            out[i] = sum(bar["c"] * bar["v"] for bar in chunk) / weight
    return out


def _profile(bars: list[dict], i: int, lookback: int, rows: int):
    lb = min(lookback, i)
    if lb < 2:
        return None
    window = bars[i - lb + 1:i + 1]
    prof_hi = max(bar["h"] for bar in window)
    prof_lo = min(bar["l"] for bar in window)
    span = prof_hi - prof_lo
    if span <= 0:
        return None
    step = span / rows
    total = [0.0] * rows
    up = [0.0] * rows
    dn = [0.0] * rows
    for bar in window:
        if bar["v"] <= 0:
            continue
        s_row = max(math.floor((bar["l"] - prof_lo) / step), 0)
        e_row = min(math.floor((bar["h"] - prof_lo) / step), rows - 1)
        covered = e_row - s_row + 1
        if covered <= 0:
            continue
        share = bar["v"] / covered
        up_bar = bar["c"] >= bar["o"]
        for row in range(s_row, e_row + 1):
            total[row] += share
            if up_bar:
                up[row] += share
            else:
                dn[row] += share
    best = -1.0
    poc_row = 0
    for row, volume in enumerate(total):
        if volume > best:
            best = volume
            poc_row = row
    poc_lo = prof_lo + poc_row * step
    poc_hi = prof_lo + (poc_row + 1) * step
    poc_price = prof_lo + (poc_row + 0.5) * step
    base = total[poc_row]
    delta = ((up[poc_row] - dn[poc_row]) / base) * 100 if base > 0 else 0.0
    return poc_lo, poc_hi, poc_price, delta


def _pivot(values: list[float], i: int, left: int, right: int):
    center = i - right
    if center < left:
        return None
    price = values[center]
    for j in range(center - left, center + right + 1):
        if j != center and values[j] >= price:
            return None
    return price


def _clock(unix: int) -> tuple[int, object]:
    stamp = datetime.fromtimestamp(unix, timezone.utc).astimezone(ET)
    return stamp.hour * 100 + stamp.minute, stamp.date()


def replay_poc(bars: list[dict], *, point: float = 50.0, tick: float = 0.25,
               commission: float = COMMISSION, slippage_ticks: int = SLIPPAGE_TICKS,
               qty: int = QTY, capital: float = CAPITAL) -> dict:
    """Replay the POC pullback rules and return the closed trades.

    `bars` need t (unix seconds), o, h, l, c, and v. Bars with no volume are
    skipped inside the profile, the same way the tester skips them.
    """
    atr = _wilder_atr(bars, 14)
    vwma = _vwma(bars, 50)
    highs = [bar["h"] for bar in bars]
    lows = [bar["l"] for bar in bars]
    poc_hist: list[tuple | None] = []
    recent_hi: list[float] = []
    recent_lo: list[float] = []

    position = 0
    entry = 0.0
    stop = 0.0
    peak = None
    trough = None
    be_on = False
    opened_on = None
    cooldown_from = -10_000
    day = None
    daily = 0.0
    locked = False
    prev_in = False
    trades: list[dict] = []
    closed_equity = capital
    peak_equity = capital
    max_dd = 0.0

    def _mark_dd(equity: float) -> None:
        nonlocal peak_equity, max_dd
        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, peak_equity - equity)

    def _close(i: int, fill: float, reason: str) -> None:
        nonlocal position, closed_equity, daily, locked, cooldown_from, be_on, peak, trough
        net = trade_net(position, entry, fill, point, qty, commission)
        closed_equity += net
        daily += net
        _mark_dd(closed_equity)
        if (daily >= 2000) or (daily <= -1000):
            locked = True
        trades.append({
            "side": "Long" if position > 0 else "Short",
            "entry": entry,
            "exit": fill,
            "net": round(net, 2),
            "entry_time": opened_on,
            "exit_time": bars[i]["t"],
            "reason": reason,
            "notional": round(entry * point, 2),
            "stop": stop,
        })
        position = 0
        be_on = False
        peak = None
        trough = None
        cooldown_from = i

    for i, bar in enumerate(bars):
        clock, today = _clock(bar["t"])
        if today != day:
            day = today
            daily = 0.0
            locked = False
        in_session = 800 <= clock < 1600
        reduced = clock >= 1800 or clock < 800
        zone = _profile(bars, i, 120, 10)
        poc_hist.append(zone)
        held = position

        if position and stop_touched(position, stop, bar):
            _close(i, stop_fill(position, stop, bar, tick, slippage_ticks), "stop")
        elif position and prev_in and not in_session:
            action = "sell" if position > 0 else "buy"
            _close(i, market_fill(bar["c"], action, tick, slippage_ticks), "session")
        elif position and locked:
            action = "sell" if position > 0 else "buy"
            _close(i, market_fill(bar["c"], action, tick, slippage_ticks), "daily")

        if position and atr[i]:
            first = i > 0 and opened_on == bars[i - 1]["t"]
            risk = atr[i]
            if position > 0:
                peak = bar["h"] if first or peak is None else max(peak, bar["h"])
                if not be_on and (bar["c"] - entry) >= 0.5 * risk:
                    be_on = True
                    stop = max(stop, to_tick(entry + tick, tick))
                if (peak - entry) >= 0.25 * risk:
                    stop = max(stop, to_tick(peak - risk, tick))
            else:
                trough = bar["l"] if first or trough is None else min(trough, bar["l"])
                if not be_on and (entry - bar["c"]) >= 0.5 * risk:
                    be_on = True
                    stop = min(stop, to_tick(entry + tick, tick))
                if (entry - trough) >= 0.25 * risk:
                    stop = min(stop, to_tick(trough + risk, tick))

        ph = _pivot(highs, i, 5, 5)
        pl = _pivot(lows, i, 5, 5)
        if ph is not None:
            recent_hi.append(ph)
            del recent_hi[:-3]
        if pl is not None:
            recent_lo.append(pl)
            del recent_lo[:-3]
        lower_highs = len(recent_hi) >= 3 and all(
            recent_hi[k] < recent_hi[k - 1] for k in range(1, len(recent_hi))
        )
        higher_lows = len(recent_lo) >= 3 and all(
            recent_lo[k] > recent_lo[k - 1] for k in range(1, len(recent_lo))
        )

        slope = None
        if i >= 5 and atr[i] and atr[i] > 0 and vwma[i] is not None and vwma[i - 5] is not None:
            slope = math.atan((vwma[i] - vwma[i - 5]) / atr[i]) * 180.0 / math.pi
        long_slope = slope is not None and slope > 3.0
        short_slope = slope is not None and slope < -3.0

        depth_long = depth_short = False
        green = red = False
        oppose_long = oppose_short = True
        if zone is not None and atr[i] and atr[i] > 0:
            poc_lo, poc_hi, poc_price, delta = zone
            width = poc_hi - poc_lo
            inside = bar["h"] >= poc_lo and bar["l"] <= poc_hi
            depth_long = inside and bar["l"] <= poc_hi - width * 0.10
            depth_short = inside and bar["h"] >= poc_lo + width * 0.10
            green = delta > 0
            red = delta < 0
            if i >= 60 and poc_hist[i - 60] is not None:
                prior_lo, prior_hi, _prior_price, _prior_delta = poc_hist[i - 60]
                prior_mid = (prior_hi + prior_lo) / 2
                distinct = abs(poc_price - prior_mid) > atr[i] * 0.5
                if distinct and prior_lo > bar["c"] and (prior_lo - bar["c"]) < 2 * atr[i]:
                    oppose_long = False
                if distinct and prior_hi < bar["c"] and (bar["c"] - prior_hi) < 2 * atr[i]:
                    oppose_short = False

        cooled = i - cooldown_from >= 10
        flat = position == 0
        tradable = in_session and cooled and flat and not locked and atr[i]
        up_bar = bar["c"] > bar["o"]
        down_bar = bar["c"] < bar["o"]
        if tradable and depth_long and green and higher_lows and oppose_long and long_slope and up_bar:
            entry = market_fill(bar["c"], "buy", tick, slippage_ticks)
            mult = 1.0 if reduced else 1.5
            stop = to_tick(bar["c"] - mult * atr[i], tick)
            position = 1
            opened_on = bar["t"]
            be_on = False
            peak = bar["h"]
        elif tradable and depth_short and red and lower_highs and oppose_short and short_slope and down_bar:
            entry = market_fill(bar["c"], "sell", tick, slippage_ticks)
            mult = 1.0 if reduced else 1.5
            stop = to_tick(bar["c"] + mult * atr[i], tick)
            position = -1
            opened_on = bar["t"]
            be_on = False
            trough = bar["l"]

        if held and position:
            worst = bar["l"] if position > 0 else bar["h"]
            open_worst = closed_equity + (worst - entry) * position * point * qty
            open_close = closed_equity + (bar["c"] - entry) * position * point * qty
            _mark_dd(open_worst)
            _mark_dd(open_close)
        prev_in = in_session

    wins = [trade for trade in trades if trade["net"] > 0]
    losses = [trade for trade in trades if trade["net"] < 0]
    gross_win = sum(trade["net"] for trade in wins)
    gross_loss = abs(sum(trade["net"] for trade in losses))
    net = sum(trade["net"] for trade in trades)
    return {
        "trades": trades,
        "net": round(net, 2),
        "max_drawdown": round(max_dd, 2),
        "winners": len(wins),
        "count": len(trades),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "equity": round(closed_equity, 2),
    }


def _load_es_chart() -> list[dict]:
    """The exported 15-minute file has no volume column, so volume is joined from ES=F."""
    from pathlib import Path

    import yfinance as yf

    path = next(Path(__file__).resolve().parents[1].glob("CME_MINI_DL_ES1*.csv"))
    bars = []
    for line in path.read_text(encoding="utf-8-sig").splitlines()[1:]:
        stamp, open_, high, low, close, _ema = line.split(",")
        bars.append({
            "t": int(stamp), "o": float(open_), "h": float(high),
            "l": float(low), "c": float(close), "v": 0.0,
        })
    hist = yf.Ticker("ES=F").history(period="60d", interval="15m", auto_adjust=False)
    volume = {int(ts.timestamp()): float(row["Volume"]) for ts, row in hist.iterrows()}
    for bar in bars:
        bar["v"] = volume.get(bar["t"], 0.0)
    return bars


def main() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    report = replay_poc(_load_es_chart())
    clock = ZoneInfo("America/New_York")
    print(
        f"{report['count']} trades  net {report['net']}  "
        f"drawdown {report['max_drawdown']}  "
        f"winners {report['winners']}  profit factor {report['profit_factor']}"
    )
    for trade in report["trades"]:
        entered = datetime.fromtimestamp(trade["entry_time"], timezone.utc).astimezone(clock)
        print(
            f"{trade['side']:5} {trade['net']:8}  {entered:%m-%d %H:%M}  "
            f"fill {trade['entry']:.2f}  {trade['reason']}"
        )


if __name__ == "__main__":
    main()
