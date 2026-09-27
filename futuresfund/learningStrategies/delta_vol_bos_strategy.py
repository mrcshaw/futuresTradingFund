"""
Delta Volume BOS Breakout -- Break of Structure + Pullback Entry.
POC breakout -> swing high/low -> BOS confirms trend -> enter on next pullback.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_poc(df, start_idx, end_idx, rows):
    if end_idx < start_idx or end_idx >= len(df):
        return None, None, None
    prof_hi = df["high"].iloc[start_idx:end_idx + 1].max()
    prof_lo = df["low"].iloc[start_idx:end_idx + 1].min()
    prof_span = prof_hi - prof_lo
    if prof_span <= 0:
        return None, None, None
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
        return None, None, None
    poc_lo_price = prof_lo + poc_row * step_size
    poc_hi_price = prof_lo + (poc_row + 1) * step_size
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot) * 100.0
    return poc_lo_price, poc_hi_price, delta_pct


class DeltaVolBOSStrategy(Strategy):
    title = "Delta Volume BOS Breakout"
    initial_capital = 50000.0
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

    cooldown_bars = 10
    pullback_margin = 1.0

    use_session = True
    sess_start_hr = 8
    sess_start_mn = 0
    sess_end_hr = 16
    sess_end_mn = 0

    stop_dollars = 500.0
    tp_dollars = 1000.0

    use_trail = False
    trail_pct = 45.0
    trail_step_amt = 500.0
    trail_step_pct = 5.0
    trail_min_dollars = 500.0

    use_breakeven = True
    be_trigger_dollars = 200.0

    use_fake_break_exit = True

    _precomputed_poc_lo = None
    _precomputed_poc_hi = None
    _precomputed_poc_delta = None

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)
        self.ema_val = ta.ema(df["close"], self.ema_len)

        if self._precomputed_poc_lo is not None:
            self.poc_lo = self._precomputed_poc_lo
            self.poc_hi = self._precomputed_poc_hi
            self.poc_delta = self._precomputed_poc_delta
        else:
            poc_lo_arr, poc_hi_arr, delta_arr = [], [], []
            for i in range(n):
                lb = min(self.profile_lookback, i)
                if lb < 2:
                    poc_lo_arr.append(np.nan); poc_hi_arr.append(np.nan); delta_arr.append(np.nan)
                    continue
                start_idx = max(0, i - lb)
                plo, phi, dpct = _compute_poc(df, start_idx, i, self.profile_rows)
                poc_lo_arr.append(plo if plo is not None else np.nan)
                poc_hi_arr.append(phi if phi is not None else np.nan)
                delta_arr.append(dpct if dpct is not None else np.nan)
            self.poc_lo = pd.Series(poc_lo_arr, index=df.index)
            self.poc_hi = pd.Series(poc_hi_arr, index=df.index)
            self.poc_delta = pd.Series(delta_arr, index=df.index)

        self._phase = 0
        self._swing_hi = np.nan
        self._swing_lo = np.nan
        self._structure_level = np.nan
        self._post_bos_lo = np.nan
        self._post_bos_hi = np.nan
        self._post_bos_pulled = False
        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._peak_profit_pts = 0.0
        self._be_active = False

    def _get_bar_time(self, ts):
        try:
            if hasattr(ts, "tz_convert"):
                ts_et = ts.tz_convert("America/New_York")
            else:
                ts_et = ts
            return ts_et.hour * 100 + ts_et.minute
        except Exception:
            return getattr(ts, "hour", 0) * 100 + getattr(ts, "minute", 0)

    def _in_session(self, ts):
        if not self.use_session:
            return True
        bar_t = self._get_bar_time(ts)
        s_start = self.sess_start_hr * 100 + self.sess_start_mn
        s_end = self.sess_end_hr * 100 + self.sess_end_mn
        if s_start > s_end:
            return bar_t >= s_start or bar_t < s_end
        return s_start <= bar_t < s_end

    def _session_ended(self, prev_ts, ts):
        if not self.use_session:
            return False
        return self._in_session(prev_ts) and not self._in_session(ts)

    def _reset_phase(self):
        self._phase = 0
        self._swing_hi = np.nan
        self._swing_lo = np.nan
        self._structure_level = np.nan
        self._post_bos_lo = np.nan
        self._post_bos_hi = np.nan
        self._post_bos_pulled = False

    def next(self):
        i = self.bar_index
        df = self._data

        if i < max(self.profile_lookback, self.ema_len + self.ema_slope_len):
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")
            self._reset_phase()

        plo = self.poc_lo.iloc[i]
        phi = self.poc_hi.iloc[i]
        dpct = self.poc_delta.iloc[i]
        if pd.isna(plo) or pd.isna(phi) or pd.isna(dpct):
            return

        cl = df["close"].iloc[i]
        cl_1 = df["close"].iloc[i - 1] if i > 0 else cl
        op = df["open"].iloc[i]
        hi = df["high"].iloc[i]
        lo = df["low"].iloc[i]

        ema_now = self.ema_val.iloc[i]
        ema_prev = self.ema_val.iloc[i - self.ema_slope_len] if i >= self.ema_slope_len else np.nan
        if pd.isna(ema_now) or pd.isna(ema_prev):
            return
        ema_slope = ema_now - ema_prev
        ema_up = self.min_slope <= 0 or ema_slope > self.min_slope
        ema_dn = self.min_slope <= 0 or ema_slope < -self.min_slope

        plo_1 = self.poc_lo.iloc[i - 1] if i > 0 else plo
        phi_1 = self.poc_hi.iloc[i - 1] if i > 0 else phi
        above_poc = cl > phi and cl_1 <= (phi_1 if not pd.isna(phi_1) else phi)
        below_poc = cl < plo and cl_1 >= (plo_1 if not pd.isna(plo_1) else plo)

        delta_bull = self.min_delta_pct <= 0 or dpct > self.min_delta_pct
        delta_bear = self.min_delta_pct <= 0 or dpct < -self.min_delta_pct

        # Phase 1: Arm on POC breakout
        if above_poc and ema_up and delta_bull and self._phase == 0:
            self._phase = 1
            self._swing_hi = hi
            self._swing_lo = np.nan
            self._structure_level = np.nan
            self._post_bos_lo = np.nan
            self._post_bos_pulled = False
        elif below_poc and ema_dn and delta_bear and self._phase == 0:
            self._phase = -1
            self._swing_lo = lo
            self._swing_hi = np.nan
            self._structure_level = np.nan
            self._post_bos_hi = np.nan
            self._post_bos_pulled = False

        long_sig = False
        short_sig = False

        # Bullish: phase 1 = tracking swing, phase 2 = BOS confirmed, waiting pullback
        if self._phase == 1 and self.position_size == 0:
            sh = self._swing_hi if not np.isnan(self._swing_hi) else hi
            if hi > sh:
                self._swing_hi = hi
            if cl < self._swing_hi:
                self._structure_level = self._swing_hi
            if not np.isnan(self._structure_level) and cl > self._structure_level:
                self._phase = 2
                self._post_bos_lo = lo
                self._post_bos_pulled = False

        elif self._phase == 2 and self.position_size == 0:
            pbl = self._post_bos_lo if not np.isnan(self._post_bos_lo) else lo
            if lo < pbl:
                self._post_bos_lo = lo
            if cl > op and lo <= pbl + self.pullback_margin:
                self._post_bos_pulled = True
            if self._post_bos_pulled and cl > op:
                long_sig = True

        # Bearish: phase -1 = tracking swing, phase -2 = BOS confirmed, waiting pullback
        elif self._phase == -1 and self.position_size == 0:
            sl_val = self._swing_lo if not np.isnan(self._swing_lo) else lo
            if lo < sl_val:
                self._swing_lo = lo
            if cl > self._swing_lo:
                self._structure_level = self._swing_lo
            if not np.isnan(self._structure_level) and cl < self._structure_level:
                self._phase = -2
                self._post_bos_hi = hi
                self._post_bos_pulled = False

        elif self._phase == -2 and self.position_size == 0:
            pbh = self._post_bos_hi if not np.isnan(self._post_bos_hi) else hi
            if hi > pbh:
                self._post_bos_hi = hi
            if cl < op and hi >= pbh - self.pullback_margin:
                self._post_bos_pulled = True
            if self._post_bos_pulled and cl < op:
                short_sig = True

        # Disarm if price re-enters POC
        if self._phase != 0 and self.position_size == 0:
            if self._phase > 0 and cl <= phi:
                self._reset_phase()
            elif self._phase < 0 and cl >= plo:
                self._reset_phase()

        in_session = self._in_session(ts)
        cooled_down = i - self._last_trade_bar >= self.cooldown_bars

        long_sig = long_sig and in_session and cooled_down and self.position_size == 0
        short_sig = short_sig and in_session and cooled_down and self.position_size == 0

        if long_sig:
            self._current_stop = cl - self.stop_pts
            self._entry_price = cl
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self._reset_phase()
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=cl + self.tp_pts)
        elif short_sig:
            self._current_stop = cl + self.stop_pts
            self._entry_price = cl
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self._reset_phase()
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=cl - self.tp_pts)

        # Fake break exit
        if self.use_fake_break_exit:
            if self.position_size > 0 and cl <= phi:
                self.close_position(comment="Fake Break")
                self._reset_phase()
                self._peak_profit_pts = 0.0
                self._be_active = False
            elif self.position_size < 0 and cl >= plo:
                self.close_position(comment="Fake Break")
                self._reset_phase()
                self._peak_profit_pts = 0.0
                self._be_active = False

        if self.position_size == 0:
            self._be_active = False

        # Breakeven
        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if cl - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + 0.25
                    self.exit("XL", from_entry="Long", stop=self._current_stop, limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                if self._entry_price - cl >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - 0.25
                    self.exit("XS", from_entry="Short", stop=self._current_stop, limit=self._entry_price - self.tp_pts)

        # Trailing stop
        if self.use_trail and self.position_size != 0:
            if self.position_size > 0:
                avg = self.position_avg_price
                profit_now = hi - avg
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
                profit_now = avg - lo
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


# -----------------------------------------------------------------------------
# Simple BOS: enter on first close above/below POC only (no pullback).
# Matches strategies/delta_volume_bos_day.pine logic.
# -----------------------------------------------------------------------------


class DeltaVolBOSSimpleStrategy(DeltaVolBOSStrategy):
    """BOS = break above/below POC only. Enter on the BOS bar (no pullback)."""

    title = "Delta Volume BOS Day Shift (Simple)"

    vwma_len = 20
    cooldown_bars = 10

    def init(self):
        df = self._data
        n = len(df)
        self.vwma_val = ta.vwma(df["close"], df["volume"], self.vwma_len)
        self.ema_val = ta.ema(df["close"], self.ema_len)

        if self._precomputed_poc_lo is not None:
            self.poc_lo = self._precomputed_poc_lo
            self.poc_hi = self._precomputed_poc_hi
            self.poc_delta = self._precomputed_poc_delta
        else:
            poc_lo_arr, poc_hi_arr, delta_arr = [], [], []
            for i in range(n):
                lb = min(self.profile_lookback, i)
                if lb < 2:
                    poc_lo_arr.append(np.nan)
                    poc_hi_arr.append(np.nan)
                    delta_arr.append(np.nan)
                    continue
                start_idx = max(0, i - lb)
                plo, phi, dpct = _compute_poc(df, start_idx, i, self.profile_rows)
                poc_lo_arr.append(plo if plo is not None else np.nan)
                poc_hi_arr.append(phi if phi is not None else np.nan)
                delta_arr.append(dpct if dpct is not None else np.nan)
            self.poc_lo = pd.Series(poc_lo_arr, index=df.index)
            self.poc_hi = pd.Series(poc_hi_arr, index=df.index)
            self.poc_delta = pd.Series(delta_arr, index=df.index)

        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._peak_profit_pts = 0.0
        self._be_active = False

    def next(self):
        i = self.bar_index
        df = self._data

        if i < max(self.profile_lookback, self.ema_len + self.ema_slope_len, self.vwma_len):
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

        cl = df["close"].iloc[i]
        cl_1 = df["close"].iloc[i - 1] if i > 0 else cl
        op = df["open"].iloc[i]
        hi = df["high"].iloc[i]
        lo = df["low"].iloc[i]

        vwma_now = self.vwma_val.iloc[i]
        vwma_prev = self.vwma_val.iloc[i - self.ema_slope_len] if i >= self.ema_slope_len else np.nan
        if pd.isna(vwma_now):
            return
        vwma_slope = vwma_now - vwma_prev if not pd.isna(vwma_prev) else 0.0
        vwma_up = self.min_slope <= 0 or vwma_slope > self.min_slope
        vwma_dn = self.min_slope <= 0 or vwma_slope < -self.min_slope

        plo_1 = self.poc_lo.iloc[i - 1] if i > 0 else plo
        phi_1 = self.poc_hi.iloc[i - 1] if i > 0 else phi
        above_poc = cl > phi and cl_1 <= (phi_1 if not pd.isna(phi_1) else phi)
        below_poc = cl < plo and cl_1 >= (plo_1 if not pd.isna(plo_1) else plo)

        delta_bull = self.min_delta_pct <= 0 or dpct > self.min_delta_pct
        delta_bear = self.min_delta_pct <= 0 or dpct < -self.min_delta_pct

        in_session = self._in_session(ts)
        cooled_down = i - self._last_trade_bar >= self.cooldown_bars

        long_sig = (
            above_poc and vwma_up and delta_bull and in_session and cooled_down and self.position_size == 0
        )
        short_sig = (
            below_poc and vwma_dn and delta_bear and in_session and cooled_down and self.position_size == 0
        )

        if long_sig:
            self._current_stop = cl - self.stop_pts
            self._entry_price = cl
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=cl + self.tp_pts)
        elif short_sig:
            self._current_stop = cl + self.stop_pts
            self._entry_price = cl
            self._peak_profit_pts = 0.0
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=cl - self.tp_pts)

        if self.use_fake_break_exit:
            if self.position_size > 0 and cl <= phi:
                self.close_position(comment="Fake Break")
                self._peak_profit_pts = 0.0
                self._be_active = False
            elif self.position_size < 0 and cl >= plo:
                self.close_position(comment="Fake Break")
                self._peak_profit_pts = 0.0
                self._be_active = False

        if self.position_size == 0:
            self._be_active = False

        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if cl - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + 0.25
                    self.exit("XL", from_entry="Long", stop=self._current_stop, limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                if self._entry_price - cl >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - 0.25
                    self.exit("XS", from_entry="Short", stop=self._current_stop, limit=self._entry_price - self.tp_pts)

        if self.use_trail and self.position_size != 0:
            if self.position_size > 0:
                avg = self.position_avg_price
                profit_now = hi - avg
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
                profit_now = avg - lo
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
