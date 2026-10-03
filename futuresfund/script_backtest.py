"""Run a Pine script's orders on the Backtrader bar loop.

Each closed trade uses the same fill as the checked engine: the bar close,
one slippage tick against a market order or a stop, and the limit price for
a target. Changing one input has to be able to change the trade list, so a
study can record 200 different results.
"""

from __future__ import annotations

import ast
import math
import re
import time

from futuresfund.backtrader_engine import Bar, _protect


class Unsupported(RuntimeError):
    """This script's entry rule is not one the engine can run yet."""


def _breathe(index: int) -> None:
    """Let the desk answer a click while a long backtest is running."""
    if index and index % 250 == 0:
        time.sleep(0)


def run_source(source: str, bars: list[Bar], inputs: dict | None) -> list[dict]:
    values = _values(source, inputs or {})
    title = _title(source)
    slip = _header_number(source, "slippage", 1) * 0.25
    commission = _header_number(source, "commission_value", 5.50)
    point = float(values.get("pv") or values.get("point_value") or 50)
    if title == "Volume Profile + Smart Trail":
        return _volume_trail(bars, values, slip, commission, point)
    if title == "60 Candle Range Stay":
        return _range_stay(bars, values, slip, commission, point)
    if title == "VWAP Breakout (Intraday)":
        return _vwap(bars, values, slip, commission, point)
    if title == "BOS Breakout Evening ES 2min":
        return _bos_evening(bars, values, slip, commission, point)
    if title == "EMA BB Mean Reversion":
        return _ema_bb(bars, values, slip, commission, point)
    if title == "Night Shift Delta Volume Breakout":
        return _night_delta(bars, values, slip, commission, point)
    from futuresfund.entry_rules import run_saved

    saved = run_saved(title, bars, values, slip, commission, point)
    if saved is not None:
        return saved
    return _from_signals(bars, source, values, slip, commission, point)


def _bos_evening(bars, values, slip, commission, point) -> list[dict]:
    """Break of structure from the evening script. A pivot is confirmed after `pivot_window` bars."""
    pw = max(int(values.get("pivot_window") or 4), 1)
    bc = max(int(values.get("backcandles") or 25), pw)
    use_ema = bool(values.get("use_ema_filter"))
    ema_len = max(int(values.get("ema_length") or 50), 1)
    ema_window = max(int(values.get("ema_trend_window") or 10), 1)
    closes = [bar.c for bar in bars]
    ema = _ema(closes, ema_len)
    atr = _atr(bars, max(int(values.get("atr_length") or 14), 1))
    stop_dollars = float(values.get("stop_dollars") or 0)
    tp_dollars = float(values.get("tp_dollars") or 0)
    atr_sl = float(values.get("atr_sl_mult") or 2)
    atr_tp = float(values.get("atr_tp_mult") or 4)
    signals = []
    widths = []
    targets = []
    for i, bar in enumerate(bars):
        _breathe(i)
        signal = 0
        if i >= bc:
            highs = []
            lows = []
            for offset in range(bc, pw - 1, -1):
                pivot = i - offset
                if pivot < 0:
                    continue
                hi = bars[pivot].h
                lo = bars[pivot].l
                is_high = True
                is_low = True
                for side in range(1, pw + 1):
                    newer = pivot + side
                    older = pivot - side
                    if 0 <= newer < len(bars):
                        if bars[newer].h > hi:
                            is_high = False
                        if bars[newer].l < lo:
                            is_low = False
                    if 0 <= older < len(bars):
                        if bars[older].h > hi:
                            is_high = False
                        if bars[older].l < lo:
                            is_low = False
                if is_high:
                    highs.append((offset, hi))
                if is_low:
                    lows.append((offset, lo))
            if highs:
                ph_off, ph_val = highs[-1]
                prev = bars[i - 1].c if i else bar.c
                if bar.c > ph_val and prev <= ph_val and lows:
                    older = [(off, price) for off, price in lows if off > ph_off]
                    newer = [price for off, price in lows if off < ph_off]
                    if older and newer and min(newer) < min(older, key=lambda item: item[0])[1]:
                        signal = 2
            if signal == 0 and lows:
                pl_off, pl_val = lows[-1]
                prev = bars[i - 1].c if i else bar.c
                if bar.c < pl_val and prev >= pl_val and highs:
                    older = [(off, price) for off, price in highs if off > pl_off]
                    newer = [price for off, price in highs if off < pl_off]
                    if older and newer and max(newer) > min(older, key=lambda item: item[0])[1]:
                        signal = 1
        if use_ema and signal and ema[i] is not None and i >= ema_window:
            above = True
            below = True
            for k in range(ema_window + 1):
                past = bars[i - k]
                ema_k = ema[i - k]
                if ema_k is None:
                    above = below = False
                    break
                body_lo = min(past.o, past.c)
                body_hi = max(past.o, past.c)
                if body_lo <= ema_k:
                    above = False
                if body_hi >= ema_k:
                    below = False
            if signal == 2 and not above:
                signal = 0
            elif signal == 1 and not below:
                signal = 0
        width = (atr[i] or 0) * atr_sl if stop_dollars <= 0 else stop_dollars / point
        target = (atr[i] or 0) * atr_tp if tp_dollars <= 0 else tp_dollars / point
        signals.append((signal == 2, signal == 1, None))
        widths.append(width)
        targets.append(target)
    stop_pts = stop_dollars / point if stop_dollars > 0 else 0
    tp_pts = tp_dollars / point if tp_dollars > 0 else 0
    return _simulate(
        bars, signals, stop_pts, tp_pts, slip, commission, point, values,
        atr_stops=None if stop_dollars > 0 else widths,
        tp_enabled=tp_dollars > 0 or tp_pts > 0,
    )


def _volume_trail(bars, values, slip, commission, point) -> list[dict]:
    closes = [bar.c for bar in bars]
    ema_len = int(values.get("ema_len") or 21)
    slope_len = int(values.get("ema_slope_len") or 5)
    ema = _ema(closes, ema_len)
    lookback = int(values.get("profile_lookback") or 240)
    rows = max(int(values.get("profile_rows") or 50), 1)
    lvn = float(values.get("lvn_pct") or 3) / 100.0
    sigma = float(values.get("gauss_sigma") or 0.2) or 0.2
    sensitivity = int(values.get("sensitivity") or 13)
    atr = _atr(bars, max(sensitivity, 1))
    stop_pts = float(values.get("stop_dollars") or 200) / point
    tp_pts = float(values.get("tp_dollars") or 1000) / point
    cooldown = int(values.get("cooldown_bars") or 0)
    min_slope = float(values.get("min_slope") or 0)
    use_zone = bool(values.get("use_zone_exit"))
    zone_atr = _atr(bars, 20)
    zone_ema = _ema(closes, 20)

    trail = None
    trend = 1
    last_trade = -10**9
    signals = []
    for i, bar in enumerate(bars):
        _breathe(i)
        hl2 = (bar.h + bar.l) / 2
        width = (sensitivity * 0.3) * (atr[i] or 0)
        up_level = hl2 - width
        dn_level = hl2 + width
        if trail is None:
            trail = hl2
        if trend == 1:
            if bar.c < trail:
                trend = -1
                trail = dn_level
            else:
                trail = max(trail, up_level)
        else:
            if bar.c > trail:
                trend = 1
                trail = up_level
            else:
                trail = min(trail, dn_level)
        ema_now = ema[i]
        ema_then = ema[i - slope_len] if i >= slope_len else None
        slope = 0 if ema_now is None or ema_then is None else ema_now - ema_then
        long_sig = short_sig = False
        profile = _gaussian_profile(bars, i, lookback, rows, sigma)
        if profile is not None and ema_now is not None:
            total, step, lo = profile
            threshold = max(total) * lvn
            now = min(max(int((bar.c - lo) / step), 0), rows - 1)
            prev_close = bars[i - 1].c if i else bar.c
            prev = min(max(int((prev_close - lo) / step), 0), rows - 1)
            hvn_to_lvn = total[prev] >= threshold and total[now] < threshold
            allowed = _session_ok(bar, values) and not _skip_window(bar, values)
            cooled = i - last_trade >= cooldown
            if hvn_to_lvn and now > prev and slope > min_slope and trend == 1 and allowed and cooled:
                long_sig = True
                last_trade = i
            elif hvn_to_lvn and now < prev and slope < -min_slope and trend == -1 and allowed and cooled:
                short_sig = True
                last_trade = i
        zone_exit = None
        if use_zone and zone_ema[i] is not None and zone_atr[i] is not None:
            upper = zone_ema[i] + zone_atr[i] * 2
            lower = zone_ema[i] - zone_atr[i] * 2
            if bar.c < lower:
                zone_exit = "long"
            elif bar.c > upper:
                zone_exit = "short"
        signals.append((long_sig, short_sig, zone_exit))
    return _simulate(bars, signals, stop_pts, tp_pts, slip, commission, point, values)


def _range_stay(bars, values, slip, commission, point) -> list[dict]:
    closes = [bar.c for bar in bars]
    ema_len = int(values.get("ema_len") or 200)
    slope_len = int(values.get("ema_slope_len") or 5)
    ema = _ema(closes, max(ema_len, 1))
    lookback = max(int(values.get("lookback") or 60), 2)
    cycle = lookback * max(int(values.get("cycle_mult") or 3), 2)
    confirm = max(int(values.get("confirm_len") or 3), 2)
    zone_pts = float(values.get("zone_dollars") or 150) / point
    stop_pts = float(values.get("stop_dollars") or 300) / point
    tp_pts = float(values.get("tp_dollars") or 300) / point
    min_slope = float(values.get("min_slope") or 0)
    run_hi = run_lo = range_hi = range_lo = bars[0].h if bars else 0
    ready = False
    phase = 0
    direction = 0
    start = 0
    conf_hi = conf_lo = None
    signals = []
    for i, bar in enumerate(bars):
        _breathe(i)
        pos = i % cycle
        if pos == 0:
            run_hi, run_lo = bar.h, bar.l
        elif pos < lookback:
            run_hi, run_lo = max(run_hi, bar.h), min(run_lo, bar.l)
        if pos == lookback - 1:
            range_hi, range_lo, ready = run_hi, run_lo, True
        show = ready and pos >= lookback
        ema_now = ema[i]
        ema_then = ema[i - slope_len] if i >= slope_len else None
        slope = 0 if ema_now is None or ema_then is None else ema_now - ema_then
        in_session = _ny_fut_session(bar, values)
        long_sig = short_sig = False
        if phase == 0 and in_session and show:
            sell = bar.c >= range_hi - zone_pts and bar.c <= range_hi and bar.c < bar.o and slope > min_slope
            buy = bar.c <= range_lo + zone_pts and bar.c >= range_lo and bar.c > bar.o and slope < -min_slope
            if sell:
                phase, direction, start, conf_hi, conf_lo = 1, -1, i + 1, None, None
            elif buy:
                phase, direction, start, conf_hi, conf_lo = 1, 1, i + 1, None, None
        elif phase == 1 and i >= start:
            bars_in = i - start
            conf_hi = bar.h if bars_in == 0 or conf_hi is None else max(conf_hi, bar.h)
            conf_lo = bar.l if bars_in == 0 or conf_lo is None else min(conf_lo, bar.l)
            if bars_in >= confirm - 1:
                phase, start = 2, i + 1
        elif phase == 2 and i >= start and in_session and conf_lo is not None and conf_hi is not None:
            if direction == -1 and bar.c < conf_lo:
                short_sig = True
                phase = 0
            elif direction == 1 and bar.c > conf_hi:
                long_sig = True
                phase = 0
            elif i - start >= confirm - 1:
                phase = 0
        signals.append((long_sig, short_sig, None))
    return _simulate(bars, signals, stop_pts, tp_pts, slip, commission, point, values, session=_ny_fut_session)


def _vwap(bars, values, slip, commission, point) -> list[dict]:
    atr_len = max(int(values.get("atr_len") or 14), 1)
    atr = _atr(bars, atr_len)
    stop_mult = float(values.get("atr_stop") or 1.5)
    tp_dollars = float(values.get("tp_dollars") or 0)
    tp_pts = tp_dollars / point if tp_dollars > 0 else None
    min_cross = float(values.get("min_cross_pts") or 0)
    cooldown = int(values.get("cooldown_bars") or 0)
    max_day = int(values.get("max_trades_day") or 5)
    direction = str(values.get("trade_direction") or "Long and Short")
    typical_cross = str(values.get("vwap_cross_type") or "Close") == "Typical"
    can_long = direction in {"Long Only", "Long and Short"}
    can_short = direction in {"Short Only", "Long and Short"}
    cum_pv = cum_vol = 0.0
    last_day = None
    last_exit = -10**9
    trades_today = 0
    prev_price = prev_vwap = None
    signals = []
    stops = []
    for i, bar in enumerate(bars):
        _breathe(i)
        day = (bar.when.year, bar.when.month, bar.when.day)
        if day != last_day:
            cum_pv = cum_vol = 0.0
            last_day = day
            trades_today = 0
        typical = (bar.h + bar.l + bar.c) / 3
        cum_pv += typical * bar.v
        cum_vol += bar.v
        vwap = cum_pv / cum_vol if cum_vol else None
        price = typical if typical_cross else bar.c
        long_sig = short_sig = False
        if vwap is not None and prev_price is not None and prev_vwap is not None and _session_ok(bar, values):
            cross_above = prev_price <= prev_vwap and price > vwap and (bar.c - vwap) >= min_cross
            cross_below = prev_price >= prev_vwap and price < vwap and (vwap - bar.c) >= min_cross
            cooled = i - last_exit >= cooldown
            if can_long and cross_above and cooled and trades_today < max_day:
                long_sig = True
                trades_today += 1
                last_exit = i
            elif can_short and cross_below and cooled and trades_today < max_day:
                short_sig = True
                trades_today += 1
                last_exit = i
        prev_price, prev_vwap = price, vwap
        width = stop_mult * (atr[i] or 0)
        stops.append(width)
        signals.append((long_sig, short_sig, None))
    return _simulate(bars, signals, 0, tp_pts or 0, slip, commission, point, values, atr_stops=stops, tp_enabled=tp_pts is not None)


def _ema_bb(bars, values, slip, commission, point) -> list[dict]:
    """Uptrend into the lower band, or downtrend into the upper band. The stop is the ATR width at entry."""
    slow_len = max(int(values.get("ema_slow") or 55), 1)
    fast_len = max(int(values.get("ema_fast") or 15), 1)
    back = max(int(values.get("ema_backcandles") or 5), 1)
    bb_len = max(int(values.get("bb_length") or 25), 1)
    bb_std = float(values.get("bb_std") or 1.5)
    sl_mult = float(values.get("sl_atr_mult") or 1.5)
    tp_ratio = float(values.get("tp_sl_ratio") or 2.5)
    closes = [bar.c for bar in bars]
    fast = _ema(closes, fast_len)
    slow = _ema(closes, slow_len)
    basis = _sma(closes, bb_len)
    dev = _stdev(closes, bb_len)
    atr = _atr(bars, max(int(values.get("atr_length") or 7), 1))
    signals = []
    stops = []
    targets = []
    for i, bar in enumerate(bars):
        _breathe(i)
        long_sig = short_sig = False
        width = target = 0.0
        if (
            i >= back
            and basis[i] is not None
            and dev[i] is not None
            and atr[i]
            and _session_ok(bar, values)
        ):
            above = below = True
            for k in range(back):
                left, right = fast[i - k], slow[i - k]
                if left is None or right is None:
                    above = below = False
                    break
                if left <= right:
                    above = False
                if left >= right:
                    below = False
            lower = basis[i] - bb_std * dev[i]
            upper = basis[i] + bb_std * dev[i]
            width = atr[i] * sl_mult
            target = width * tp_ratio
            if width > 0 and above and bar.c <= lower:
                long_sig = True
            elif width > 0 and below and bar.c >= upper:
                short_sig = True
        signals.append((long_sig, short_sig, None))
        stops.append(width)
        targets.append(target)
    return _simulate(
        bars, signals, 0, 0, slip, commission, point, values,
        atr_stops=stops, tp_widths=targets, risk_at_entry=True,
    )


def _night_delta(bars, values, slip, commission, point) -> list[dict]:
    """Arm on a POC break with delta, then enter when the pivot confirms."""
    lookback = max(int(values.get("profile_lookback") or 90), 1)
    rows = max(int(values.get("profile_rows") or 10), 1)
    min_delta = float(values.get("min_delta_pct") or 0)
    vwma_len = max(int(values.get("vwma_len") or 50), 1)
    slope_len = max(int(values.get("vwma_slope_len") or 5), 1)
    min_slope = float(values.get("min_slope") or 0)
    left = max(int(values.get("pivot_left") or 3), 1)
    right = max(int(values.get("pivot_right") or 3), 1)
    disarm_inside = bool(values.get("disarm_only_inside_poc", True))
    use_price = bool(values.get("use_price_vwma", True))
    use_volume = bool(values.get("use_vol_filter", True))
    vol_len = max(int(values.get("vol_ma_len") or 20), 1)
    vwma = _vwma(bars, vwma_len)
    vol_ma = _sma([bar.v for bar in bars], vol_len)
    ph = _pivot_price(bars, left, right, high=True)
    pl = _pivot_price(bars, left, right, high=False)
    stop_pts = float(values.get("stop_dollars") or 600) / point if point else 8.0
    tp_pts = float(values.get("tp_dollars") or 1100) / point if point else 12.0
    cooldown = int(values.get("cooldown_bars") or 0)
    signals = []
    armed = 0
    last_entry = -10**9
    for i, bar in enumerate(bars):
        _breathe(i)
        profile = _delta_profile(bars, i, lookback, rows)
        prev = bars[i - 1].c if i else bar.c
        slope = None
        if vwma[i] is not None and i >= slope_len and vwma[i - slope_len] is not None:
            slope = vwma[i] - vwma[i - slope_len]
        vwma_up = min_slope <= 0 or (slope is not None and slope > min_slope)
        vwma_dn = min_slope <= 0 or (slope is not None and slope < -min_slope)
        if profile is not None:
            poc_lo, poc_hi, delta = profile
            if bar.c > poc_hi and prev <= poc_hi and vwma_up and (min_delta <= 0 or delta > min_delta):
                armed = 1
            elif bar.c < poc_lo and prev >= poc_lo and vwma_dn and (min_delta <= 0 or delta < -min_delta):
                armed = -1
            if armed == 1 and ((disarm_inside and bar.c <= poc_lo) or (not disarm_inside and bar.c <= poc_hi)):
                armed = 0
            elif armed == -1 and ((disarm_inside and bar.c >= poc_hi) or (not disarm_inside and bar.c >= poc_lo)):
                armed = 0
        price_long = (not use_price) or (vwma[i] is not None and bar.c > vwma[i])
        price_short = (not use_price) or (vwma[i] is not None and bar.c < vwma[i])
        volume_ok = (not use_volume) or (vol_ma[i] is not None and bar.v >= vol_ma[i])
        cooled = i - last_entry >= cooldown
        in_session = _session_ok(bar, values)
        long_sig = armed == 1 and ph[i] is not None and price_long and volume_ok and cooled and in_session
        short_sig = armed == -1 and pl[i] is not None and price_short and volume_ok and cooled and in_session
        if long_sig or short_sig:
            armed = 0
            last_entry = i
        signals.append((long_sig, short_sig, None))
    return _simulate(bars, signals, stop_pts, tp_pts, slip, commission, point, values)


def _pivot_price(bars, left: int, right: int, high: bool) -> list:
    """The pivot price on the bar where Pine confirms it, `right` bars after the pivot."""
    out = [None] * len(bars)
    for i in range(left + right, len(bars)):
        pivot = i - right
        price = bars[pivot].h if high else bars[pivot].l
        ok = True
        for j in range(pivot - left, i + 1):
            if j == pivot:
                continue
            other = bars[j].h if high else bars[j].l
            if high and other >= price:
                ok = False
                break
            if not high and other <= price:
                ok = False
                break
        if ok:
            out[i] = price
    return out


def _delta_profile(bars, index: int, lookback: int, rows: int):
    """POC edges and the signed delta percent of the heaviest row."""
    length = min(lookback, index)
    if length <= 1:
        return None
    window = bars[index - length + 1:index + 1]
    hi = max(bar.h for bar in window)
    lo = min(bar.l for bar in window)
    span = hi - lo
    if span <= 0:
        return None
    step = span / rows
    total = [0.0] * rows
    up = [0.0] * rows
    down = [0.0] * rows
    for bar in window:
        if bar.v <= 0:
            continue
        start = max(int(math.floor((bar.l - lo) / step)), 0)
        end = min(int(math.floor((bar.h - lo) / step)), rows - 1)
        count = end - start + 1
        if count <= 0:
            continue
        share = bar.v / count
        rising = bar.c >= bar.o
        for row in range(start, end + 1):
            total[row] += share
            if rising:
                up[row] += share
            else:
                down[row] += share
    best = max(range(rows), key=total.__getitem__)
    if total[best] <= 0:
        return None
    delta = ((up[best] - down[best]) / total[best]) * 100
    return lo + best * step, lo + (best + 1) * step, delta


def _from_signals(bars, source, values, slip, commission, point) -> list[dict]:
    long_name, short_name = _signal_names(source)
    if not long_name:
        raise Unsupported(_title(source) or "This script")
    env_series = _series(bars, source, values)
    bools = _bool_lines(source)
    stop_pts = _stop_points(values, point)
    tp_pts = _tp_points(values, point)
    signals = []
    for i, bar in enumerate(bars):
        _breathe(i)
        env = {
            "close": bar.c, "open": bar.o, "high": bar.h, "low": bar.l, "volume": bar.v,
            "close_1": bars[i - 1].c if i else bar.c,
            "open_1": bars[i - 1].o if i else bar.o,
            "high_1": bars[i - 1].h if i else bar.h,
            "low_1": bars[i - 1].l if i else bar.l,
            "in_session": _session_ok(bar, values),
            "entry_allowed": _session_ok(bar, values) and not _skip_window(bar, values),
            "cooled_down": True, "cooled": True, "cooled_off": True,
            "daily_locked": False,
            "strategy_position_size": 0,
        }
        for key, value in values.items():
            if isinstance(value, (int, float, bool, str)):
                env[key] = value
        for name, series in env_series.items():
            env[name] = series[i]
            env[f"{name}_1"] = series[i - 1] if i else series[i]
        for name, expr in bools:
            value = _eval_bool(expr, env)
            if value is not None:
                env[name] = value
        signals.append((bool(env.get(long_name)), bool(env.get(short_name)), None))
    return _simulate(bars, signals, stop_pts, tp_pts, slip, commission, point, values)


def _simulate(bars, signals, stop_pts, tp_pts, slip, commission, point, values, session=None, atr_stops=None, tp_enabled=True, tp_widths=None, risk_at_entry=False) -> list[dict]:
    session = session or _session_ok
    cooldown = int(values.get("cooldown_bars") or values.get("cool_bars") or 0)
    last_entry = -10**9
    use_trail = bool(values.get("use_trail"))
    trail_pct = float(values.get("trail_pct") or 60)
    trail_step_amt = float(values.get("trail_step_amt") or 500)
    trail_step_pct = float(values.get("trail_step_pct") or 10)
    trail_min = float(values.get("trail_min_dollars") or 300)
    pos = 0
    entry = stop = limit = 0.0
    entry_i = -1
    peak = 0.0
    open_trade = None
    trades = []

    def close(i, price, kind):
        nonlocal pos, open_trade, peak
        if open_trade is None or pos == 0:
            pos = 0
            return
        points = (price - entry) if pos > 0 else (entry - price)
        open_trade.update({
            "exit_time": bars[i].when.strftime("%Y-%m-%d %H:%M"),
            "exit_price": round(price, 2),
            "exit_signal": kind,
            "pnl": round(points * point - commission * 2, 2),
        })
        trades.append(open_trade)
        open_trade = None
        pos = 0
        peak = 0.0

    for i, bar in enumerate(bars):
        _breathe(i)
        long_sig, short_sig, zone_exit = signals[i]
        if pos != 0 and i > entry_i:
            if atr_stops is not None and not risk_at_entry:
                width = atr_stops[i] or 0
                stop = entry - width if pos > 0 else entry + width
            if use_trail and peak * point >= trail_min:
                steps = math.floor((peak * point) / trail_step_amt) if trail_step_amt else 0
                keep = min(trail_pct + steps * trail_step_pct, 95.0) / 100.0
                if pos > 0:
                    stop = max(stop, entry + peak * keep)
                else:
                    stop = min(stop, entry - peak * keep)
            hit = _protect(bar, pos, stop, limit if tp_enabled else (10**9 if pos > 0 else -10**9), slip)
            if hit is not None:
                close(i, hit[1], "XL" if pos > 0 else "XS")
            elif zone_exit == "long" and pos > 0:
                close(i, bar.c - slip, "Zone Exit")
            elif zone_exit == "short" and pos < 0:
                close(i, bar.c + slip, "Zone Exit")
            elif pos > 0:
                peak = max(peak, bar.h - entry)
            elif pos < 0:
                peak = max(peak, entry - bar.l)
        in_now = session(bar, values)
        in_prev = session(bars[i - 1], values) if i else False
        if pos != 0 and i and in_prev and not in_now:
            close(i, bar.c - slip if pos > 0 else bar.c + slip, "Session Close")
        if pos == 0 and in_now and i - last_entry >= cooldown and (long_sig or short_sig):
            last_entry = i
            side = 1 if long_sig else -1
            fill = bar.c + slip if side > 0 else bar.c - slip
            if atr_stops is not None:
                width = atr_stops[i] or stop_pts
                stop = fill - width if side > 0 else fill + width
            else:
                stop = bar.c - stop_pts if side > 0 else bar.c + stop_pts
            target = tp_widths[i] if tp_widths is not None else None
            if target:
                limit = bar.c + target if side > 0 else bar.c - target
            elif tp_pts:
                limit = bar.c + tp_pts if side > 0 else bar.c - tp_pts
            else:
                limit = fill + 10**6 if side > 0 else fill - 10**6
            pos = side
            entry = fill
            entry_i = i
            peak = 0.0
            open_trade = {
                "side": "long" if side > 0 else "short",
                "entry_time": bar.when.strftime("%Y-%m-%d %H:%M"),
                "entry_price": round(fill, 2),
                "entry_signal": "Long" if side > 0 else "Short",
            }
    return trades


def _values(source: str, inputs: dict) -> dict:
    found = {}
    labels = {}
    for match in re.finditer(
        r"(\w+)\s*=\s*input\.(int|float|bool|string)\(\s*([^,\n]+)\s*,\s*\"([^\"]*)\"",
        source or "",
    ):
        name, kind, raw, label = match.group(1), match.group(2), match.group(3).strip(), match.group(4)
        found[name] = _literal(kind, raw)
        labels[label.strip().lower()] = name
    for key, value in inputs.items():
        name = key if key in found else labels.get(str(key).strip().lower())
        if name:
            found[name] = _coerce(found.get(name), value)
    return found


def _literal(kind: str, raw: str):
    text = raw.strip().rstrip(",")
    if kind == "bool":
        return text.lower() == "true"
    if kind == "string":
        return text.strip().strip("\"'")
    number = float(text)
    return int(number) if kind == "int" else number


def _coerce(current, value):
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(value))
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def _series(bars, source, values) -> dict[str, list]:
    closes = [bar.c for bar in bars]
    highs = [bar.h for bar in bars]
    lows = [bar.l for bar in bars]
    out = {}
    for match in re.finditer(r"(\w+)\s*=\s*ta\.(ema|sma|atr|rsi|vwma|highest|lowest)\(\s*([^)]+)\)", source or ""):
        name, kind, args = match.group(1), match.group(2), [part.strip() for part in match.group(3).split(",")]
        length = _length(args[-1] if kind != "highest" and kind != "lowest" else args[-1], values)
        if kind == "ema":
            out[name] = _ema(closes, length)
        elif kind == "sma":
            out[name] = _sma(closes, length)
        elif kind == "atr":
            out[name] = _atr(bars, length)
        elif kind == "rsi":
            out[name] = _rsi(closes, length)
        elif kind == "vwma":
            out[name] = _vwma(bars, length)
        elif kind == "highest":
            out[name] = _rolling(highs if "high" in args[0] else closes, length, max)
        else:
            out[name] = _rolling(lows if "low" in args[0] else closes, length, min)
    for match in re.finditer(r"(\w+)\s*=\s*(\w+)\s*-\s*\2\[(\w+)\]", source or ""):
        name, base, offset = match.group(1), match.group(2), match.group(3)
        series = out.get(base)
        if not series:
            continue
        span = _length(offset, values)
        out[name] = [
            None if i < span or series[i] is None or series[i - span] is None else series[i] - series[i - span]
            for i in range(len(bars))
        ]
    for match in re.finditer(r"(\w+)\s*=\s*ta\.(crossover|crossunder)\(([^)]+)\)", source or ""):
        name, kind, args = match.group(1), match.group(2), [part.strip() for part in match.group(3).split(",")]
        if len(args) != 2 or args[0] not in out or args[1] not in out:
            continue
        left, right = out[args[0]], out[args[1]]
        flags = []
        for i in range(len(bars)):
            if i == 0 or left[i] is None or right[i] is None or left[i - 1] is None or right[i - 1] is None:
                flags.append(False)
            elif kind == "crossover":
                flags.append(left[i - 1] <= right[i - 1] and left[i] > right[i])
            else:
                flags.append(left[i - 1] >= right[i - 1] and left[i] < right[i])
        out[name] = flags
    return out


def _length(token: str, values) -> int:
    token = token.strip()
    if token in values and isinstance(values[token], (int, float)):
        return max(int(values[token]), 1)
    try:
        return max(int(float(token)), 1)
    except ValueError:
        return 1


def _bool_lines(source: str) -> list[tuple[str, str]]:
    rows = []
    for line in source.splitlines():
        match = re.match(r"\s*(?:bool\s+)?(\w+)\s*=\s*(.+)$", line.strip())
        if not match:
            continue
        if "input." in line or "ta." in line or ":=" in line:
            continue
        if "strategy.entry" in line or "strategy.exit" in line or "strategy.close" in line:
            continue
        name, expr = match.group(1), match.group(2).split("//")[0].strip()
        if any(word in expr for word in (" and ", " or ", ">", "<", "==", "!=")):
            rows.append((name, expr))
    return rows


def _signal_names(source: str) -> tuple[str, str]:
    pairs = (
        ("long_sig", "short_sig"),
        ("do_long", "do_short"),
        ("bull_cond", "bear_cond"),
        ("long_pattern", "short_pattern"),
    )
    for long_name, short_name in pairs:
        if re.search(rf"\b{long_name}\b", source) and re.search(rf"\b{short_name}\b", source):
            return long_name, short_name
    return "", ""


def _eval_bool(expr: str, env: dict) -> bool:
    if "?" in expr:
        return None
    text = expr
    text = re.sub(r"\bnot\s+na\((\w+)\)", r"(\1 is not None)", text)
    text = re.sub(r"\bna\((\w+)\)", r"(\1 is None)", text)
    text = re.sub(r"\bstrategy\.position_size\b", "strategy_position_size", text)
    text = re.sub(r"(\w+)\[(\d+)\]", lambda match: f"{match.group(1)}_{match.group(2)}", text)
    text = text.replace("?", " if ").replace(":", " else ") if "?" in text else text
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return False
    if not _safe(tree):
        return False
    try:
        return bool(eval(compile(tree, "<signal>", "eval"), {"__builtins__": {}}, env))
    except Exception:
        return None


def _safe(tree: ast.AST) -> bool:
    allowed = (
        ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Constant,
        ast.Load, ast.And, ast.Or, ast.Not, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
        ast.Gt, ast.Lt, ast.GtE, ast.LtE, ast.Eq, ast.NotEq, ast.USub, ast.IfExp,
    )
    return all(isinstance(node, allowed) for node in ast.walk(tree))


def _stop_points(values, point) -> float:
    if values.get("stop_dollars"):
        return float(values["stop_dollars"]) / point
    if values.get("stop_pts"):
        return float(values["stop_pts"])
    return 8.0


def _tp_points(values, point) -> float:
    if values.get("tp_dollars"):
        return float(values["tp_dollars"]) / point
    if values.get("take_profit_pts"):
        return float(values["take_profit_pts"])
    return 12.0


def _session_ok(bar: Bar, values: dict) -> bool:
    if "use_session" in values and not values.get("use_session"):
        return True
    if "sess_start_hr" not in values:
        return True
    stamp = bar.when.hour * 100 + bar.when.minute
    start = int(values.get("sess_start_hr") or 0) * 100 + int(values.get("sess_start_mn") or 0)
    end = int(values.get("sess_end_hr") or 0) * 100 + int(values.get("sess_end_mn") or 0)
    if start > end:
        return stamp >= start or stamp < end
    return start <= stamp < end


def _ny_fut_session(bar: Bar, values: dict) -> bool:
    if "use_session_filter" in values and not values.get("use_session_filter"):
        return True
    stamp = bar.when.hour * 100 + bar.when.minute
    ny = bool(values.get("use_ny_sess", True)) and stamp >= int(values.get("ny_start_hr") or 9) * 100 + int(values.get("ny_start_mn") or 30) and stamp < int(values.get("ny_end_hr") or 16) * 100
    fut_start = int(values.get("fut_start_hr") or 18) * 100
    fut_end = int(values.get("fut_end_hr") or 8) * 100
    fut = bool(values.get("use_fut_sess", True)) and (stamp >= fut_start or stamp < fut_end)
    return ny or fut


def _skip_window(bar: Bar, values: dict) -> bool:
    if not values.get("skip_open"):
        return False
    stamp = bar.when.hour * 100 + bar.when.minute
    start = int(values.get("skip_start_hr") or 0) * 100 + int(values.get("skip_start_mn") or 0)
    end = int(values.get("skip_end_hr") or 0) * 100 + int(values.get("skip_end_mn") or 0)
    if start > end:
        return stamp >= start or stamp < end
    return start <= stamp < end


def _gaussian_profile(bars, i, lookback, rows, sigma):
    lb = min(lookback, i)
    if lb <= 1:
        return None
    window = bars[i - lb + 1:i + 1]
    hi = max(bar.h for bar in window)
    lo = min(bar.l for bar in window)
    span = hi - lo
    if span <= 0:
        return None
    step = span / rows
    total = [0.0] * rows
    for bar in window:
        if bar.v <= 0:
            continue
        s_row = max(int((bar.l - lo) / step), 0)
        e_row = min(int((bar.h - lo) / step), rows - 1)
        span_rows = e_row - s_row + 1
        if span_rows <= 0:
            continue
        weights = []
        for k in range(span_rows):
            t = 0.5 if span_rows == 1 else k / (span_rows - 1)
            z = (t - 0.5) / sigma
            weights.append(math.exp(-0.5 * z * z))
        scale = sum(weights) or 1
        for k, weight in enumerate(weights):
            total[s_row + k] += bar.v * (weight / scale)
    return total, step, lo


def _ema(values, length):
    out = [None] * len(values)
    if length <= 0 or len(values) < length:
        return out
    alpha = 2 / (length + 1)
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _sma(values, length):
    out = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= length:
            total -= values[i - length]
        if i >= length - 1 and length:
            out[i] = total / length
    return out


def _stdev(values, length):
    """Population standard deviation, the same divisor Pine uses for ta.stdev."""
    out = [None] * len(values)
    if length <= 1:
        return out
    for i in range(length - 1, len(values)):
        window = values[i - length + 1:i + 1]
        mean = sum(window) / length
        out[i] = math.sqrt(sum((value - mean) ** 2 for value in window) / length)
    return out


def _atr(bars, length):
    trs = []
    for i, bar in enumerate(bars):
        prev = bars[i - 1].c if i else bar.c
        trs.append(max(bar.h - bar.l, abs(bar.h - prev), abs(bar.l - prev)))
    return _rma(trs, length)


def _rma(values, length):
    out = [None] * len(values)
    if not values or length <= 0 or len(values) < length:
        return out
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    alpha = 1 / length
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _vwma(bars, length):
    out = [None] * len(bars)
    if length <= 0:
        return out
    for i in range(len(bars)):
        if i + 1 < length:
            continue
        window = bars[i - length + 1:i + 1]
        volume = sum(bar.v for bar in window)
        out[i] = sum(bar.c * bar.v for bar in window) / volume if volume else None
    return out


def _rsi(values, length):
    out = [None] * len(values)
    if length <= 0 or len(values) <= length:
        return out
    gains = losses = 0.0
    for i in range(1, length + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0)
        losses += max(-change, 0)
    avg_gain = gains / length
    avg_loss = losses / length
    out[length] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(length + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (length - 1) + max(change, 0)) / length
        avg_loss = (avg_loss * (length - 1) + max(-change, 0)) / length
        out[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def _rolling(values, length, pick):
    out = [None] * len(values)
    for i in range(len(values)):
        if i + 1 >= length:
            out[i] = pick(values[i - length + 1:i + 1])
    return out


def _title(source: str) -> str:
    match = re.search(r"strategy\s*\(\s*\"([^\"]+)\"", source or "")
    return match.group(1).strip() if match else ""


def _header_number(source: str, name: str, default: float) -> float:
    match = re.search(rf"\b{name}\s*=\s*([0-9.]+)", source or "")
    if not match:
        return default
    try:
        return float(match.group(1))
    except ValueError:
        return default
