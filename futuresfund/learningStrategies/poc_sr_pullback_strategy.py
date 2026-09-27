"""
POC S/R Pullback Strategy — tracks historical POC levels as support/resistance.
When POCs trend down, sell pullbacks to old POC (resistance).
When POCs trend up, buy pullbacks to old POC (support).
"""

import pandas as pd
import numpy as np
from collections import deque

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

    poc_price = prof_lo + (poc_row + 0.5) * step_size
    poc_lo_price = prof_lo + poc_row * step_size
    poc_hi_price = prof_lo + (poc_row + 1) * step_size
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot) * 100.0
    return poc_price, poc_lo_price, poc_hi_price, delta_pct


class POCSRPullbackStrategy(Strategy):
    title = "POC S/R Pullback"
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

    poc_shift_pts = 2.0
    poc_history_count = 5
    poc_trend_count = 3

    ema_len = 50

    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 16
    sess_end_mn = 0

    cooldown_bars = 10

    stop_dollars = 500.0
    tp_dollars = 1000.0

    use_trail = False
    trail_pct = 75.0
    trail_step_amt = 500.0
    trail_step_pct = 50.0
    trail_min_dollars = 300.0

    use_breakeven = True
    be_trigger_dollars = 200.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)

        self.ema_val = ta.ema(df["close"], self.ema_len)

        poc_price_arr = []
        poc_lo_arr = []
        poc_hi_arr = []
        delta_arr = []

        for i in range(n):
            lb = min(self.profile_lookback, i)
            if lb < 2:
                poc_price_arr.append(np.nan)
                poc_lo_arr.append(np.nan)
                poc_hi_arr.append(np.nan)
                delta_arr.append(np.nan)
                continue
            start_idx = max(0, i - lb)
            pprice, plo, phi, dpct = _compute_poc(df, start_idx, i, self.profile_rows)
            poc_price_arr.append(pprice if pprice is not None else np.nan)
            poc_lo_arr.append(plo if plo is not None else np.nan)
            poc_hi_arr.append(phi if phi is not None else np.nan)
            delta_arr.append(dpct if dpct is not None else np.nan)

        self.poc_price = pd.Series(poc_price_arr, index=df.index)
        self.poc_lo = pd.Series(poc_lo_arr, index=df.index)
        self.poc_hi = pd.Series(poc_hi_arr, index=df.index)
        self.poc_delta = pd.Series(delta_arr, index=df.index)

        self._hist_poc_price = deque(maxlen=self.poc_history_count)
        self._hist_poc_hi = deque(maxlen=self.poc_history_count)
        self._hist_poc_lo = deque(maxlen=self.poc_history_count)
        self._hist_poc_delta = deque(maxlen=self.poc_history_count)
        self._last_rec_poc = np.nan

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

        if i < max(self.profile_lookback, self.ema_len) + 1:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        poc_p = self.poc_price.iloc[i]
        plo = self.poc_lo.iloc[i]
        phi = self.poc_hi.iloc[i]
        dpct = self.poc_delta.iloc[i]
        if pd.isna(poc_p) or pd.isna(plo) or pd.isna(phi):
            return

        # Track POC history
        if np.isnan(self._last_rec_poc) or abs(poc_p - self._last_rec_poc) >= self.poc_shift_pts:
            if not np.isnan(self._last_rec_poc):
                self._hist_poc_price.appendleft(self._last_rec_poc)
                step = (phi - plo) if not pd.isna(phi) and not pd.isna(plo) else 1.0
                self._hist_poc_hi.appendleft(self._last_rec_poc + step / 2)
                self._hist_poc_lo.appendleft(self._last_rec_poc - step / 2)
                self._hist_poc_delta.appendleft(dpct if not pd.isna(dpct) else 0.0)
            self._last_rec_poc = poc_p

        hist_size = len(self._hist_poc_price)
        if hist_size < self.poc_trend_count:
            return

        # POC trend detection
        poc_downtrend = all(
            self._hist_poc_price[k] < self._hist_poc_price[k + 1]
            for k in range(self.poc_trend_count - 1)
        )
        poc_uptrend = all(
            self._hist_poc_price[k] > self._hist_poc_price[k + 1]
            for k in range(self.poc_trend_count - 1)
        )

        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        ema_now = self.ema_val.iloc[i]
        if pd.isna(ema_now):
            return

        nearest_res = self._hist_poc_hi[0]
        nearest_sup = self._hist_poc_lo[0]

        touching_resistance = high >= nearest_res and close < nearest_res
        touching_support = low <= nearest_sup and close > nearest_sup

        in_session = self._in_session(ts)
        cooled_down = i - self._last_trade_bar >= self.cooldown_bars

        short_sig = (poc_downtrend and touching_resistance and close < ema_now
                     and in_session and cooled_down and self.position_size >= 0)
        long_sig = (poc_uptrend and touching_support and close > ema_now
                    and in_session and cooled_down and self.position_size <= 0)

        if long_sig:
            self._current_stop = close - self.stop_pts
            self._entry_price = close
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close + self.tp_pts)
        elif short_sig:
            self._current_stop = close + self.stop_pts
            self._entry_price = close
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close - self.tp_pts)

        if self.position_size == 0:
            self._peak_profit_pts = 0.0
            self._be_active = False

        # Breakeven
        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if close - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + 0.25
                    self.exit("XL", from_entry="Long", stop=self._current_stop,
                              limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                if self._entry_price - close >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - 0.25
                    self.exit("XS", from_entry="Short", stop=self._current_stop,
                              limit=self._entry_price - self.tp_pts)

        # Trailing stop
        if self.use_trail and self.position_size != 0:
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
                        self.exit("XL", from_entry="Long", stop=self._current_stop,
                                  limit=avg + self.tp_pts)
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
                        self.exit("XS", from_entry="Short", stop=self._current_stop,
                                  limit=avg - self.tp_pts)
