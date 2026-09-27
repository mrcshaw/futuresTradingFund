"""
Delta Volume Breakout — exact port of Pine Script.
POC breakout + delta confirmation + EMA trend + trailing stop.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_poc(df, start_idx, end_idx, rows):
    """Compute POC channel and delta % from volume profile."""
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
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot) * 100.0
    return poc_lo_price, poc_hi_price, poc_row, delta_pct


class DeltaVolumeBreakoutStrategy(Strategy):
    title = "Delta Volume Breakout"
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

    profile_lookback = 90
    profile_rows = 10
    min_delta_pct = 5.0

    ema_len = 20
    ema_slope_len = 5
    min_slope = 0.1

    pivot_left = 3
    pivot_right = 3
    use_pivot_confirmation = True
    cooldown_bars = 10
    disarm_only_inside_poc = True

    use_session = True
    sess_start_hr = 8
    sess_start_mn = 0
    sess_end_hr = 16
    sess_end_mn = 0

    stop_dollars = 640.0
    tp_dollars = 1100.0

    use_trail = False
    trail_pct = 75.0
    trail_step_amt = 500.0
    trail_step_pct = 50.0
    trail_min_dollars = 600.0

    use_breakeven = False
    be_trigger_dollars = 200.0

    # Extra direction filter: require price on correct side of EMA
    require_price_above_ema_long = False
    require_price_below_ema_short = False

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)

        self.ema_val = ta.ema(df["close"], self.ema_len)

        poc_lo_arr = []
        poc_hi_arr = []
        delta_arr = []

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

        # Pivot high/low; in Pine, value appears at confirmation bar (right_bars after pivot)
        self.ph_raw = ta.pivothigh(df["high"], self.pivot_left, self.pivot_right)
        self.pl_raw = ta.pivotlow(df["low"], self.pivot_left, self.pivot_right)
        self.ph_confirmed = self.ph_raw.shift(self.pivot_right)
        self.pl_confirmed = self.pl_raw.shift(self.pivot_right)

        self._armed_dir = 0
        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._peak_profit_pts = 0.0
        self._be_active = False

    def _in_session(self, ts):
        if not self.use_session:
            return True
        try:
            if hasattr(ts, "tz_convert"):
                ts_et = ts.tz_convert("America/New_York")
            else:
                ts_et = ts
            hr = ts_et.hour
            mn = ts_et.minute
        except Exception:
            hr = getattr(ts, "hour", 0)
            mn = getattr(ts, "minute", 0)
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

        if i < max(self.profile_lookback, self.ema_len + self.ema_slope_len, self.pivot_left + self.pivot_right):
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")
            self._armed_dir = 0

        plo = self.poc_lo.iloc[i]
        phi = self.poc_hi.iloc[i]
        dpct = self.poc_delta.iloc[i]
        if pd.isna(plo) or pd.isna(phi) or pd.isna(dpct):
            return

        close = df["close"].iloc[i]
        close_1 = df["close"].iloc[i - 1] if i > 0 else close
        open_ = df["open"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]

        # EMA trend
        ema_now = self.ema_val.iloc[i]
        ema_prev = self.ema_val.iloc[i - self.ema_slope_len] if i >= self.ema_slope_len else np.nan
        if pd.isna(ema_now) or pd.isna(ema_prev):
            return
        ema_slope = ema_now - ema_prev
        ema_up = self.min_slope <= 0 or ema_slope > self.min_slope
        ema_dn = self.min_slope <= 0 or ema_slope < -self.min_slope

        # POC breakout detection
        plo_1 = self.poc_lo.iloc[i - 1] if i > 0 else plo
        phi_1 = self.poc_hi.iloc[i - 1] if i > 0 else phi
        above_poc = close > phi and close_1 <= (phi_1 if not pd.isna(phi_1) else phi)
        below_poc = close < plo and close_1 >= (plo_1 if not pd.isna(plo_1) else plo)

        # Delta filter
        delta_bull = self.min_delta_pct <= 0 or dpct > self.min_delta_pct
        delta_bear = self.min_delta_pct <= 0 or dpct < -self.min_delta_pct

        # Price vs EMA (extra direction conviction)
        price_above_ema = close > ema_now if self.require_price_above_ema_long else True
        price_below_ema = close < ema_now if self.require_price_below_ema_short else True

        # Arm on breakout
        if above_poc and ema_up and delta_bull and price_above_ema:
            self._armed_dir = 1
        elif below_poc and ema_dn and delta_bear and price_below_ema:
            self._armed_dir = -1

        # Disarm: when disarm_only_inside_poc=True, only disarm when price fully re-enters POC zone
        if self._armed_dir == 1 and not pd.isna(plo) and not pd.isna(phi):
            if self.disarm_only_inside_poc:
                if close <= plo:  # fell below POC low = fully back in
                    self._armed_dir = 0
            else:
                if close <= phi:  # standard: any close back at/below breakout level
                    self._armed_dir = 0
        elif self._armed_dir == -1 and not pd.isna(plo) and not pd.isna(phi):
            if self.disarm_only_inside_poc:
                if close >= phi:  # rose above POC high = fully back in
                    self._armed_dir = 0
            else:
                if close >= plo:
                    self._armed_dir = 0

        # Confirmation: pivot or direct (no pivot wait)
        if self.use_pivot_confirmation:
            ph_ok = not pd.isna(self.ph_confirmed.iloc[i])
            pl_ok = not pd.isna(self.pl_confirmed.iloc[i])
            confirmed_long = self._armed_dir == 1 and ph_ok
            confirmed_short = self._armed_dir == -1 and pl_ok
        else:
            confirmed_long = self._armed_dir == 1
            confirmed_short = self._armed_dir == -1

        in_session = self._in_session(ts)
        cooled_down = i - self._last_trade_bar >= self.cooldown_bars

        long_sig = confirmed_long and in_session and cooled_down and self.position_size <= 0
        short_sig = confirmed_short and in_session and cooled_down and self.position_size >= 0

        # Entry
        if long_sig:
            self._current_stop = close - self.stop_pts
            self._entry_price = close
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self._armed_dir = 0
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close + self.tp_pts)
        elif short_sig:
            self._current_stop = close + self.stop_pts
            self._entry_price = close
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self._armed_dir = 0
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close - self.tp_pts)

        # Breakeven management
        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                unrealized = close - self._entry_price
                if unrealized >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + 0.25
                    self.exit("XL", from_entry="Long", stop=self._current_stop, limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                unrealized = self._entry_price - close
                if unrealized >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - 0.25
                    self.exit("XS", from_entry="Short", stop=self._current_stop, limit=self._entry_price - self.tp_pts)

        if self.position_size == 0:
            self._be_active = False

        # Trailing stop
        if self.use_trail:
            if self.position_size > 0:
                avg = self.position_avg_price
                profit_now = high - avg
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_min_dollars / self.point_value:
                    peak_dollars = self._peak_profit_pts * self.point_value
                    steps = int(peak_dollars / self.trail_step_amt)
                    keep_pct = min(self.trail_pct + steps * self.trail_step_pct, 95.0)
                    new_stop = avg + self._peak_profit_pts * (keep_pct / 100.0)
                    if new_stop > self._current_stop:
                        self._current_stop = new_stop
                        self.exit("XL", from_entry="Long", stop=self._current_stop, limit=avg + self.tp_pts)
            elif self.position_size < 0:
                avg = self.position_avg_price
                profit_now = avg - low
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_min_dollars / self.point_value:
                    peak_dollars = self._peak_profit_pts * self.point_value
                    steps = int(peak_dollars / self.trail_step_amt)
                    keep_pct = min(self.trail_pct + steps * self.trail_step_pct, 95.0)
                    new_stop = avg - self._peak_profit_pts * (keep_pct / 100.0)
                    if new_stop < self._current_stop:
                        self._current_stop = new_stop
                        self.exit("XS", from_entry="Short", stop=self._current_stop, limit=avg - self.tp_pts)
