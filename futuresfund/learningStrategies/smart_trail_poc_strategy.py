"""
Smart Trail + POC Pullback — Python port for backtesting.

Entry: price pulls back to POC zone in trend direction, bounces off.
  Long:  trend up + POC green + price dips to POC + closes above
  Short: trend down + POC red + price rallies to POC + closes below
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_poc(df, start_idx, end_idx, rows):
    if end_idx < start_idx or end_idx >= len(df):
        return None, None, None, None
    prof_hi = df["high"].iloc[start_idx:end_idx + 1].max()
    prof_lo = df["low"].iloc[start_idx:end_idx + 1].min()
    prof_span = prof_hi - prof_lo
    if prof_span <= 0:
        return None, None, None, None
    step_size = prof_span / rows
    vol_total = np.zeros(rows)
    vol_up = np.zeros(rows)
    vol_dn = np.zeros(rows)
    for j in range(start_idx, end_idx + 1):
        bar_vol = df["volume"].iloc[j]
        if bar_vol <= 0:
            continue
        bar_lo = df["low"].iloc[j]
        bar_hi = df["high"].iloc[j]
        bar_is_up = df["close"].iloc[j] >= df["open"].iloc[j]
        s_row = max(int((bar_lo - prof_lo) / step_size), 0)
        e_row = min(int((bar_hi - prof_lo) / step_size), rows - 1)
        span = max(e_row - s_row + 1, 1)
        per_row = bar_vol / span
        for k in range(span):
            r = s_row + k
            if 0 <= r < rows:
                vol_total[r] += per_row
                if bar_is_up:
                    vol_up[r] += per_row
                else:
                    vol_dn[r] += per_row
    poc_row = int(np.argmax(vol_total))
    poc_tot = vol_total[poc_row]
    if poc_tot <= 0:
        return None, None, None, None
    poc_lo_price = prof_lo + poc_row * step_size
    poc_hi_price = prof_lo + (poc_row + 1) * step_size
    poc_mid = prof_lo + (poc_row + 0.5) * step_size
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot) * 100.0
    return poc_lo_price, poc_hi_price, poc_mid, delta_pct


def _smart_trail(high, low, close, sensitivity):
    n = len(close)
    atr_val = ta.atr(high, low, close, sensitivity)
    hl2 = (high + low) / 2.0
    up_level = hl2 - (sensitivity * 0.3) * atr_val
    dn_level = hl2 + (sensitivity * 0.3) * atr_val

    trail_arr = np.full(n, np.nan)
    trend_arr = np.ones(n, dtype=int)

    for i in range(sensitivity, n):
        if i == sensitivity:
            trail_arr[i] = hl2.iloc[i]
            trend_arr[i] = 1
            continue
        prev_trail = trail_arr[i - 1]
        prev_trend = trend_arr[i - 1]
        c = close.iloc[i]
        if prev_trend == 1:
            if c < prev_trail:
                trend_arr[i] = -1
                trail_arr[i] = dn_level.iloc[i]
            else:
                trend_arr[i] = 1
                trail_arr[i] = max(prev_trail, up_level.iloc[i])
        else:
            if c > prev_trail:
                trend_arr[i] = 1
                trail_arr[i] = up_level.iloc[i]
            else:
                trend_arr[i] = -1
                trail_arr[i] = min(prev_trail, dn_level.iloc[i])

    return (pd.Series(trail_arr, index=close.index),
            pd.Series(trend_arr, index=close.index))


class SmartTrailPocStrategy(Strategy):
    title = "Smart Trail + POC Pullback"
    initial_capital = 25000.0
    default_qty_type = QtyType.FIXED
    default_qty_value = 1.0
    commission_type = CommissionType.PER_CONTRACT
    commission_value = 5.50
    slippage = 1
    pyramiding = 0
    process_orders_on_close = True
    is_futures = True
    point_value = 50.0
    tick_size = 0.25

    sensitivity = 13
    profile_lookback = 120
    profile_rows = 10

    require_delta_match = True
    min_delta_pct = 0.0
    cooldown_bars = 3

    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    stop_dollars = 250.0
    tp_dollars = 400.0

    use_breakeven = True
    be_trigger_dollars = 150.0

    use_reversal = True

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)

        self.trail_line, self.trend_line = _smart_trail(
            df["high"], df["low"], df["close"], self.sensitivity)

        poc_lo_arr, poc_hi_arr, delta_arr = [], [], []
        for i in range(n):
            lb = min(self.profile_lookback, i)
            if lb < 2:
                poc_lo_arr.append(np.nan)
                poc_hi_arr.append(np.nan)
                delta_arr.append(np.nan)
                continue
            start_idx = max(0, i - lb)
            plo, phi, _, dpct = _compute_poc(df, start_idx, i, self.profile_rows)
            poc_lo_arr.append(plo if plo is not None else np.nan)
            poc_hi_arr.append(phi if phi is not None else np.nan)
            delta_arr.append(dpct if dpct is not None else np.nan)

        self.poc_lo = pd.Series(poc_lo_arr, index=df.index)
        self.poc_hi = pd.Series(poc_hi_arr, index=df.index)
        self.poc_delta = pd.Series(delta_arr, index=df.index)

        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._be_active = False
        self._last_dir = 0
        self._is_reversal = False
        self._reversal_pending = False
        self._prev_pos = 0.0

    def _in_session(self, ts):
        if not self.use_session:
            return True
        try:
            ts_et = ts.tz_convert("America/New_York") if hasattr(ts, "tz_convert") else ts
            hr, mn = ts_et.hour, ts_et.minute
        except Exception:
            hr, mn = getattr(ts, "hour", 0), getattr(ts, "minute", 0)
        bar_t = hr * 100 + mn
        s_start = self.sess_start_hr * 100 + self.sess_start_mn
        s_end = self.sess_end_hr * 100 + self.sess_end_mn
        if s_start > s_end:
            return bar_t >= s_start or bar_t < s_end
        return s_start <= bar_t < s_end

    def _session_ended(self, prev_ts, ts):
        if not self.use_session:
            return False
        return self._in_session(prev_ts) and not self._in_session(ts)

    def next(self):
        i = self.bar_index
        df = self._data
        warmup = max(self.profile_lookback, self.sensitivity + 1)
        if i < warmup:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        plo = self.poc_lo.iloc[i]
        phi = self.poc_hi.iloc[i]
        dpct = self.poc_delta.iloc[i]
        if pd.isna(plo) or pd.isna(phi) or pd.isna(dpct):
            return

        close_now = df["close"].iloc[i]
        low_now = df["low"].iloc[i]
        high_now = df["high"].iloc[i]
        close_1 = df["close"].iloc[i - 1] if i > 0 else close_now

        plo_1 = self.poc_lo.iloc[i - 1] if i > 0 else plo
        phi_1 = self.poc_hi.iloc[i - 1] if i > 0 else phi
        if pd.isna(phi_1):
            phi_1 = phi
        if pd.isna(plo_1):
            plo_1 = plo

        trend_now = int(self.trend_line.iloc[i])

        # Pullback to POC
        touched_from_above = low_now <= phi and close_now > phi and close_1 > phi_1
        touched_from_below = high_now >= plo and close_now < plo and close_1 < plo_1

        poc_green = dpct > self.min_delta_pct
        poc_red = dpct < -self.min_delta_pct
        delta_ok_long = (not self.require_delta_match) or poc_green
        delta_ok_short = (not self.require_delta_match) or poc_red

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars

        long_sig = trend_now == 1 and touched_from_above and delta_ok_long and in_session and cooled and self.position_size <= 0
        short_sig = trend_now == -1 and touched_from_below and delta_ok_short and in_session and cooled and self.position_size >= 0

        # Reversal detection
        prev_pos = self._prev_pos
        if self.position_size == 0 and prev_pos != 0:
            if self.use_reversal and not self._is_reversal and self._last_dir != 0 and not self._be_active:
                if len(self._broker.closed_trades) > 0:
                    last_trade = self._broker.closed_trades[-1]
                    if last_trade.profit < 0:
                        self._reversal_pending = True
            self._be_active = False
            self._is_reversal = False

        stop_pts = self.stop_pts
        tp_pts = self.tp_pts

        if long_sig:
            self._current_stop = close_now - stop_pts
            self._entry_price = close_now
            self._be_active = False
            self._is_reversal = False
            self._last_trade_bar = i
            self._last_dir = 1
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close_now + tp_pts)
        elif short_sig:
            self._current_stop = close_now + stop_pts
            self._entry_price = close_now
            self._be_active = False
            self._is_reversal = False
            self._last_trade_bar = i
            self._last_dir = -1
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close_now - tp_pts)
        elif self._reversal_pending and self.position_size == 0 and in_session:
            self._reversal_pending = False
            self._is_reversal = True
            self._be_active = False
            self._last_trade_bar = i
            rev_stop_pts = stop_pts / 2.0
            rev_tp_pts = stop_pts
            if self._last_dir == 1:
                self._current_stop = close_now + rev_stop_pts
                self._entry_price = close_now
                self._last_dir = -1
                self.entry("RevShort", Direction.SHORT)
                self.exit("XRS", from_entry="RevShort", stop=self._current_stop, limit=close_now - rev_tp_pts)
            elif self._last_dir == -1:
                self._current_stop = close_now - rev_stop_pts
                self._entry_price = close_now
                self._last_dir = 1
                self.entry("RevLong", Direction.LONG)
                self.exit("XRL", from_entry="RevLong", stop=self._current_stop, limit=close_now + rev_tp_pts)

        # Breakeven
        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0 and (close_now - self._entry_price) >= self.be_trigger_pts:
                self._be_active = True
                self._current_stop = self._entry_price + 0.25
                self.exit("XL", from_entry="Long", stop=self._current_stop, limit=self._entry_price + tp_pts)
            elif self.position_size < 0 and (self._entry_price - close_now) >= self.be_trigger_pts:
                self._be_active = True
                self._current_stop = self._entry_price - 0.25
                self.exit("XS", from_entry="Short", stop=self._current_stop, limit=self._entry_price - tp_pts)

        if self.position_size == 0:
            self._be_active = False

        self._prev_pos = self.position_size
