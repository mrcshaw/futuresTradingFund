"""
Delta Volume Breakout Night Shift -- Python engine version.
Mirrors the Pine Script logic:
  1. Volume Profile with Delta at POC
  2. EMA trend filter
  3. Breakout from POC channel with confirmation candles
  4. Cooldown between trades
  5. Overnight session 18:00-8:00
"""
import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_poc_delta(df, end_idx, lookback, rows):
    start_idx = max(0, end_idx - lookback)
    if end_idx < start_idx or end_idx >= len(df):
        return None, None, None, None
    prof_hi = df["high"].iloc[start_idx:end_idx + 1].max()
    prof_lo = df["low"].iloc[start_idx:end_idx + 1].min()
    span = prof_hi - prof_lo
    if span <= 0:
        return None, None, None, None
    step = span / rows
    vol_total = np.zeros(rows)
    vol_up = np.zeros(rows)
    vol_dn = np.zeros(rows)
    for j in range(start_idx, end_idx + 1):
        bv = df["volume"].iloc[j]
        if bv <= 0:
            continue
        bl = df["low"].iloc[j]
        bh = df["high"].iloc[j]
        is_up = df["close"].iloc[j] >= df["open"].iloc[j]
        sr = max(int((bl - prof_lo) / step), 0)
        er = min(int((bh - prof_lo) / step), rows - 1)
        s = max(er - sr + 1, 1)
        pr = bv / s
        for k in range(s):
            r = sr + k
            if 0 <= r < rows:
                vol_total[r] += pr
                if is_up:
                    vol_up[r] += pr
                else:
                    vol_dn[r] += pr
    poc_row = int(np.argmax(vol_total))
    tot = vol_total[poc_row]
    if tot <= 0:
        return None, None, None, None
    poc_lo_p = prof_lo + poc_row * step
    poc_hi_p = prof_lo + (poc_row + 1) * step
    poc_price = prof_lo + (poc_row + 0.5) * step
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / tot) * 100.0
    return poc_price, poc_lo_p, poc_hi_p, delta_pct


class DeltaVolNightStrategy(Strategy):
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

    profile_lookback = 120
    profile_rows = 10
    min_delta_pct = 10.0
    ema_len = 50
    ema_slope_len = 5
    min_slope = 0.0

    confirm_candles = 3
    cooldown_bars = 10

    sess_start_hr = 18
    sess_end_hr = 8
    stop_dollars = 350.0
    tp_dollars = 500.0

    use_breakeven = False
    be_trigger_dollars = 150.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        if len(df) < 60:
            self._ready = False
            return
        self._ready = True
        self._ema_val = ta.ema(df["close"], self.ema_len)

        # Precompute POC for every bar
        poc_price_arr = [np.nan] * len(df)
        poc_lo_arr = [np.nan] * len(df)
        poc_hi_arr = [np.nan] * len(df)
        poc_delta_arr = [np.nan] * len(df)
        for i in range(2, len(df)):
            lb = min(self.profile_lookback, i)
            if lb < 2:
                continue
            pp, pl, ph, dd = _compute_poc_delta(df, i, lb, self.profile_rows)
            if pp is not None:
                poc_price_arr[i] = pp
                poc_lo_arr[i] = pl
                poc_hi_arr[i] = ph
                poc_delta_arr[i] = dd
        self._poc_price = poc_price_arr
        self._poc_lo = poc_lo_arr
        self._poc_hi = poc_hi_arr
        self._poc_delta = poc_delta_arr

        self._armed_dir = 0
        self._confirm_count = 0
        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._be_active = False
        self._prev_bar_time = 0

    def _parse_ts(self, ts):
        try:
            if hasattr(ts, "tz_convert"):
                et = ts.tz_convert("America/New_York")
            else:
                et = ts
            return et.hour * 100 + et.minute
        except Exception:
            return 0

    def _in_session(self, bt):
        s = self.sess_start_hr * 100
        e = self.sess_end_hr * 100
        if s > e:
            return bt >= s or bt < e
        return bt >= s and bt < e

    def next(self):
        i = self.bar_index
        df = self._data
        if not getattr(self, "_ready", False) or i < 2:
            return

        bt = self._parse_ts(df.index[i])
        in_sess = self._in_session(bt)
        prev_in_sess = self._in_session(self._prev_bar_time)
        self._prev_bar_time = bt

        if prev_in_sess and not in_sess and self.position_size != 0:
            self.close_all(comment="Session Close")
            return

        if not in_sess:
            return

        cl = float(df["close"].iloc[i])
        hi = float(df["high"].iloc[i])
        lo = float(df["low"].iloc[i])
        op = float(df["open"].iloc[i])
        cl_prev = float(df["close"].iloc[i - 1])

        ema_now = self._ema_val.iloc[i]
        ema_prev = self._ema_val.iloc[i - self.ema_slope_len] if i >= self.ema_slope_len else ema_now
        if pd.isna(ema_now) or pd.isna(ema_prev):
            return
        ema_slope = ema_now - ema_prev
        ema_up = self.min_slope <= 0 or ema_slope > self.min_slope
        ema_dn = self.min_slope <= 0 or ema_slope < -self.min_slope

        poc_lo = self._poc_lo[i]
        poc_hi = self._poc_hi[i]
        poc_delta = self._poc_delta[i]
        if np.isnan(poc_lo) or np.isnan(poc_hi):
            return

        above_poc = cl > poc_hi and cl_prev <= poc_hi
        below_poc = cl < poc_lo and cl_prev >= poc_lo

        delta_bull = self.min_delta_pct <= 0 or poc_delta > self.min_delta_pct
        delta_bear = self.min_delta_pct <= 0 or poc_delta < -self.min_delta_pct

        if above_poc and ema_up and delta_bull:
            self._armed_dir = 1
            self._confirm_count = 0
        elif below_poc and ema_dn and delta_bear:
            self._armed_dir = -1
            self._confirm_count = 0

        if self._armed_dir == 1:
            if cl >= op:
                self._confirm_count += 1
            else:
                self._armed_dir = 0
                self._confirm_count = 0
        elif self._armed_dir == -1:
            if cl < op:
                self._confirm_count += 1
            else:
                self._armed_dir = 0
                self._confirm_count = 0

        confirmed_long = self._armed_dir == 1 and self._confirm_count >= self.confirm_candles
        confirmed_short = self._armed_dir == -1 and self._confirm_count >= self.confirm_candles

        cooled = (i - self._last_trade_bar) >= self.cooldown_bars

        # Breakeven management
        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                unrealized = cl - self._entry_price
                if unrealized >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + 0.25
                    self.exit("XL", from_entry="Long", stop=self._current_stop, limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                unrealized = self._entry_price - cl
                if unrealized >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - 0.25
                    self.exit("XS", from_entry="Short", stop=self._current_stop, limit=self._entry_price - self.tp_pts)

        if self.position_size == 0:
            self._be_active = False

        long_sig = confirmed_long and cooled and self.position_size <= 0
        short_sig = confirmed_short and cooled and self.position_size >= 0

        if long_sig:
            self._current_stop = cl - self.stop_pts
            self._entry_price = cl
            self._be_active = False
            self._last_trade_bar = i
            self._armed_dir = 0
            self._confirm_count = 0
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=cl + self.tp_pts)
        elif short_sig:
            self._current_stop = cl + self.stop_pts
            self._entry_price = cl
            self._be_active = False
            self._last_trade_bar = i
            self._armed_dir = 0
            self._confirm_count = 0
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=cl - self.tp_pts)
