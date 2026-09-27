"""
POC Delta Pullback -- Overnight (18:00-08:00 EST)

Logic:
1. Compute rolling volume profile -> POC channel (poc_lo, poc_hi) + delta %.
2. EMA trend filter confirms direction.
3. Delta at POC confirms buying/selling pressure.
4. DIRECTION: price breaks away from POC channel in alignment with EMA + delta.
5. ENTRY: price pulls back TO the POC channel (touches or enters it).
6. Stop below/above POC channel. TP fixed or trailed.
"""
import numpy as np
import pandas as pd

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


class POCPullbackNight(Strategy):
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
    min_delta_pct = 5.0
    ema_len = 50
    ema_slope_len = 5
    min_slope = 0.0

    sess_start_hr = 18
    sess_end_hr = 8

    breakaway_pts = 3.0
    pullback_margin = 1.0
    max_attempts = 2
    cooldown_bars = 5

    stop_dollars = 400.0
    tp_dollars = 600.0
    use_trail = True
    trail_activate_dollars = 300.0
    trail_keep_pct = 50.0

    use_breakeven = False
    be_trigger_dollars = 150.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.trail_act_pts = self.trail_activate_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        if len(df) < 60:
            self._ready = False
            return
        self._ready = True
        self._ema_val = ta.ema(df["close"], self.ema_len)

        if hasattr(self.__class__, '_shared_poc') and self.__class__._shared_poc is not None:
            self._poc_lo = self.__class__._shared_poc[0]
            self._poc_hi = self.__class__._shared_poc[1]
            self._poc_delta = self.__class__._shared_poc[2]
        else:
            n = len(df)
            self._poc_lo = np.full(n, np.nan)
            self._poc_hi = np.full(n, np.nan)
            self._poc_delta = np.full(n, np.nan)
            for i in range(2, n):
                lb = min(self.profile_lookback, i)
                if lb < 2:
                    continue
                pp, pl, ph, dd = _compute_poc_delta(df, i, lb, self.profile_rows)
                if pp is not None:
                    self._poc_lo[i] = pl
                    self._poc_hi[i] = ph
                    self._poc_delta[i] = dd

        # State
        self._direction = 0  # 0=undecided, 1=bullish, -1=bearish
        self._session_attempts = 0
        self._last_trade_bar = -9999
        self._peak_profit_pts = 0.0
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._be_active = False
        self._prev_bar_time = 0

    def _parse_bt(self, ts):
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
        return bt >= s or bt < e

    def _is_new_session(self, bt):
        s = self.sess_start_hr * 100
        return bt >= s and self._prev_bar_time < s

    def next(self):
        i = self.bar_index
        df = self._data
        if not getattr(self, "_ready", False) or i < 2:
            return

        bt = self._parse_bt(df.index[i])
        in_sess = self._in_session(bt)
        new_sess = self._is_new_session(bt)

        # Session ended -> flatten
        prev_in = self._in_session(self._prev_bar_time)
        if prev_in and not in_sess and self.position_size != 0:
            self.close_all(comment="Session End")
        self._prev_bar_time = bt

        if new_sess:
            self._direction = 0
            self._session_attempts = 0
            self._peak_profit_pts = 0.0
            self._current_stop = 0.0
            self._be_active = False

        if not in_sess:
            return

        cl = float(df["close"].iloc[i])
        hi = float(df["high"].iloc[i])
        lo = float(df["low"].iloc[i])

        poc_lo_val = self._poc_lo[i]
        poc_hi_val = self._poc_hi[i]
        delta_val = self._poc_delta[i]
        if np.isnan(poc_lo_val) or np.isnan(poc_hi_val):
            return

        ema_now = self._ema_val.iloc[i]
        ema_prev = self._ema_val.iloc[i - self.ema_slope_len] if i >= self.ema_slope_len else ema_now
        if pd.isna(ema_now) or pd.isna(ema_prev):
            return
        ema_slope = ema_now - ema_prev
        ema_up = self.min_slope <= 0 or ema_slope > self.min_slope
        ema_dn = self.min_slope <= 0 or ema_slope < -self.min_slope

        delta_bull = self.min_delta_pct <= 0 or (not np.isnan(delta_val) and delta_val > self.min_delta_pct)
        delta_bear = self.min_delta_pct <= 0 or (not np.isnan(delta_val) and delta_val < -self.min_delta_pct)

        # Trailing stop / breakeven management
        if self.position_size != 0:
            avg = self.position_avg_price
            if self.position_size > 0:
                profit_now = hi - avg
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self.use_trail and self._peak_profit_pts >= self.trail_act_pts:
                    lock = self._peak_profit_pts * (self.trail_keep_pct / 100.0)
                    ns = avg + lock
                    if ns > self._current_stop:
                        self._current_stop = ns
                        self.exit("XL", from_entry="Long", stop=self._current_stop, limit=avg + self.tp_pts)
                if self.use_breakeven and not self._be_active and (cl - avg) >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = avg + 0.25
                    self.exit("XL", from_entry="Long", stop=self._current_stop, limit=avg + self.tp_pts)
            else:
                profit_now = avg - lo
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self.use_trail and self._peak_profit_pts >= self.trail_act_pts:
                    lock = self._peak_profit_pts * (self.trail_keep_pct / 100.0)
                    ns = avg - lock
                    if ns < self._current_stop:
                        self._current_stop = ns
                        self.exit("XS", from_entry="Short", stop=self._current_stop, limit=avg - self.tp_pts)
                if self.use_breakeven and not self._be_active and (avg - cl) >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = avg - 0.25
                    self.exit("XS", from_entry="Short", stop=self._current_stop, limit=avg - self.tp_pts)
            return

        if self._session_attempts >= self.max_attempts:
            return
        if (i - self._last_trade_bar) < self.cooldown_bars:
            return

        # DIRECTION: detect breakaway from POC channel
        if self._direction == 0:
            if cl > poc_hi_val + self.breakaway_pts and ema_up and delta_bull:
                self._direction = 1
            elif cl < poc_lo_val - self.breakaway_pts and ema_dn and delta_bear:
                self._direction = -1

        # ENTRY: pullback to POC channel
        if self._direction == 1:
            if lo <= poc_hi_val + self.pullback_margin and cl > poc_lo_val:
                self._session_attempts += 1
                self._last_trade_bar = i
                self._peak_profit_pts = 0.0
                self._be_active = False
                stop = cl - self.stop_pts
                self._current_stop = stop
                self._entry_price = cl
                self.entry("Long", Direction.LONG)
                self.exit("XL", from_entry="Long", stop=stop, limit=cl + self.tp_pts)
        elif self._direction == -1:
            if hi >= poc_lo_val - self.pullback_margin and cl < poc_hi_val:
                self._session_attempts += 1
                self._last_trade_bar = i
                self._peak_profit_pts = 0.0
                self._be_active = False
                stop = cl + self.stop_pts
                self._current_stop = stop
                self._entry_price = cl
                self.entry("Short", Direction.SHORT)
                self.exit("XS", from_entry="Short", stop=stop, limit=cl - self.tp_pts)
