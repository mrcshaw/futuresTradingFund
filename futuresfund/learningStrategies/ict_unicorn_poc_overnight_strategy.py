"""
ICT Unicorn + Delta Volume POC — Overnight session (18:00–8:00 EST).

Same logic as the RTH version but adapted for the evening/overnight session:
  - Session: 18:00–8:00 (crosses midnight)
  - Gather: first N minutes after 18:00
  - Entry window: after gather until entry_end
  - EOD flatten: 8:00 or 8:45
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_poc(df, start_idx, end_idx, rows):
    """Compute POC price, channel, and delta % from volume profile."""
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
    poc_price = prof_lo + (poc_row + 0.5) * step_size
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot) * 100.0
    return poc_price, poc_lo_price, poc_hi_price, delta_pct


def _has_bull_fvg(df, bar, atr, min_atr, lookback):
    atr_val = atr.iloc[bar] if bar < len(atr) and not pd.isna(atr.iloc[bar]) else 2.0
    if atr_val <= 0:
        atr_val = 2.0
    for i in range(bar - 2, max(bar - lookback, 2), -1):
        if i + 2 >= len(df):
            continue
        l0, h2 = df["low"].iloc[i], df["high"].iloc[i + 2]
        if l0 > h2 and (l0 - h2) >= atr_val * min_atr:
            return True
    return False


def _has_bear_fvg(df, bar, atr, min_atr, lookback):
    atr_val = atr.iloc[bar] if bar < len(atr) and not pd.isna(atr.iloc[bar]) else 2.0
    if atr_val <= 0:
        atr_val = 2.0
    for i in range(bar - 2, max(bar - lookback, 2), -1):
        if i + 2 >= len(df):
            continue
        h0, l2 = df["high"].iloc[i], df["low"].iloc[i + 2]
        if h0 < l2 and (l2 - h0) >= atr_val * min_atr:
            return True
    return False


def _atr(high, low, close, length):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def _bar_time(ts, tz="America/New_York"):
    """Extract hr*100+mn from timestamp."""
    try:
        if hasattr(ts, "tz_convert"):
            et = ts.tz_convert(tz)
        else:
            et = ts
        return et.hour * 100 + et.minute, et.hour, et.minute, et.date() if hasattr(et, "date") else None
    except Exception:
        return 0, 0, 0, None


class ICTUnicornPOCOvernightBase(Strategy):
    """ORB + POC + Delta + EMA for overnight session 18:00–8:00."""

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

    # Session: overnight 18:00–8:00
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    gather_minutes = 15
    entry_end_hr = 8
    entry_end_mn = 0
    close_eod_hr = 8
    close_eod_mn = 0

    breakout_margin_pts = 1.0
    or_min_pts = 0.0
    or_max_pts = 9999.0
    max_attempts = 2
    max_stop_dollars = 500.0

    fvg_lookback = 30
    fvg_min_atr = 0.04
    atr_len = 14

    profile_lookback = 120
    profile_rows = 10
    min_delta_pct = 5.0
    ema_len = 50
    ema_slope_len = 5
    min_slope = 0.0

    tp_dollars = 500.0
    use_trail = True
    trail_activate_dollars = 400.0
    trail_keep_pct = 40.0

    require_fvg = False
    poc_filter_mode = "off"

    def configure(self):
        self.tp_pts = self.tp_dollars / self.point_value
        self.trail_activate_pts = self.trail_activate_dollars / self.point_value
        self.max_stop_pts = self.max_stop_dollars / self.point_value
        self._sess_start = self.sess_start_hr * 100 + self.sess_start_mn
        self._sess_end = self.sess_end_hr * 100 + self.sess_end_mn
        self._eod_time = self.close_eod_hr * 100 + self.close_eod_mn
        self._entry_end = self.entry_end_hr * 100 + self.entry_end_mn
        self._gather_end = self._sess_start + self.gather_minutes

    def init(self):
        df = self._data
        if len(df) < 50:
            self._ready = False
            return
        self._ready = True

        self._atr = _atr(df["high"], df["low"], df["close"], self.atr_len)
        self._ema_val = ta.ema(df["close"], self.ema_len)

        poc_price_arr = []
        poc_delta_arr = []

        if self.poc_filter_mode != "off":
            for i in range(len(df)):
                lb = min(self.profile_lookback, i)
                if lb < 2:
                    poc_price_arr.append(np.nan)
                    poc_delta_arr.append(np.nan)
                    continue
                start_idx = max(0, i - lb)
                pprice, _, _, dpct = _compute_poc(df, start_idx, i, self.profile_rows)
                poc_price_arr.append(pprice if pprice is not None else np.nan)
                poc_delta_arr.append(dpct if dpct is not None else np.nan)
        else:
            poc_price_arr = [np.nan] * len(df)
            poc_delta_arr = [np.nan] * len(df)

        self._poc_price = pd.Series(poc_price_arr, index=df.index)
        self._poc_delta = pd.Series(poc_delta_arr, index=df.index)

        self._session_id = None
        self._day_attempts = 0
        self._or_high = 0.0
        self._or_low = 99999.0
        self._or_done = False
        self._or_valid = False
        self._peak_profit_pts = 0.0
        self._current_stop = 0.0
        self._prev_bar_time = 0

    def _in_session(self, bt):
        """Check if bar_time is within 18:00–8:00 (crosses midnight)."""
        return bt >= self._sess_start or bt < self._sess_end

    def _is_new_session(self, bt):
        """Detect transition into 18:00."""
        return bt >= self._sess_start and self._prev_bar_time < self._sess_start

    def _in_gather(self, bt):
        """18:00 to 18:00+gather_minutes."""
        return bt >= self._sess_start and bt < self._gather_end

    def _gather_done(self, bt):
        """After gather, still in session."""
        if not self._in_session(bt):
            return False
        return bt >= self._gather_end or bt < self._sess_end

    def _in_entry_window(self, bt):
        """After gather, before entry_end. Handles midnight wrap."""
        if not self._gather_done(bt):
            return False
        if self._entry_end <= self._sess_start:
            return bt >= self._gather_end or bt < self._entry_end
        return bt >= self._gather_end and bt < self._entry_end

    def _at_eod(self, bt):
        """EOD flatten: only in the morning window, not during evening."""
        return bt >= self._eod_time and bt < self._sess_start

    def next(self):
        i = self.bar_index
        df = self._data
        if not getattr(self, "_ready", False) or i < 1:
            return

        bt_val, hr, mn, today = _bar_time(df.index[i])
        if today is None:
            return

        in_sess = self._in_session(bt_val)
        new_sess = self._is_new_session(bt_val)

        if new_sess:
            self._session_id = today
            self._day_attempts = 0
            self._or_high = 0.0
            self._or_low = 99999.0
            self._or_done = False
            self._or_valid = False
            self._peak_profit_pts = 0.0
            self._current_stop = 0.0

        self._prev_bar_time = bt_val

        if not in_sess:
            return

        close = float(df["close"].iloc[i])
        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        # Gather phase
        if self._in_gather(bt_val):
            if high > self._or_high:
                self._or_high = high
            if low < self._or_low:
                self._or_low = low
            return

        if not self._or_done:
            self._or_done = True
            or_range = self._or_high - self._or_low
            self._or_valid = (self.or_min_pts <= or_range <= self.or_max_pts and or_range > 0)

        if not self._or_valid:
            return

        # EOD flatten
        if self._at_eod(bt_val) and self.position_size != 0:
            self.close_all(comment="EOD")
            return

        # Trailing stop management
        if self.use_trail and self.position_size != 0:
            avg = self.position_avg_price
            profit_now = (high - avg) if self.position_size > 0 else (avg - low)
            if profit_now > self._peak_profit_pts:
                self._peak_profit_pts = profit_now
            if self._peak_profit_pts >= self.trail_activate_pts:
                lock = self._peak_profit_pts * (self.trail_keep_pct / 100.0)
                if self.position_size > 0:
                    ns = avg + lock
                    if ns > self._current_stop:
                        self._current_stop = ns
                        self.exit("XL", from_entry="Long", stop=self._current_stop, limit=avg + self.tp_pts)
                else:
                    ns = avg - lock
                    if ns < self._current_stop:
                        self._current_stop = ns
                        self.exit("XS", from_entry="Short", stop=self._current_stop, limit=avg - self.tp_pts)

        # Entry window check
        if not self._in_entry_window(bt_val) or self._day_attempts >= self.max_attempts:
            return

        if self.poc_filter_mode != "off":
            poc_price = self._poc_price.iloc[i]
            poc_delta = self._poc_delta.iloc[i]
            if pd.isna(poc_price) or pd.isna(poc_delta):
                return
            
            poc_bull = close > poc_price
            poc_bear = close < poc_price
            delta_bull = self.min_delta_pct <= 0 or poc_delta > self.min_delta_pct
            delta_bear = self.min_delta_pct <= 0 or poc_delta < -self.min_delta_pct
        else:
            poc_price = np.nan
            poc_delta = np.nan
            poc_bull = poc_bear = delta_bull = delta_bear = False

        ema_now = self._ema_val.iloc[i]
        ema_prev = self._ema_val.iloc[i - self.ema_slope_len] if i >= self.ema_slope_len else ema_now
        if pd.isna(ema_now) or pd.isna(ema_prev):
            return
        ema_slope = ema_now - ema_prev
        ema_up = self.min_slope <= 0 or ema_slope > self.min_slope
        ema_dn = self.min_slope <= 0 or ema_slope < -self.min_slope

        breakout_long = close > (self._or_high + self.breakout_margin_pts)
        breakout_short = close < (self._or_low - self.breakout_margin_pts)

        if self.poc_filter_mode == "confirm":
            if breakout_long and not (poc_bull and delta_bull and ema_up):
                breakout_long = False
            if breakout_short and not (poc_bear and delta_bear and ema_dn):
                breakout_short = False
        elif self.poc_filter_mode == "reject":
            reject_thresh = max(self.min_delta_pct, 3.0)
            if breakout_long and (close < poc_price and poc_delta < -reject_thresh):
                breakout_long = False
            if breakout_short and (close > poc_price and poc_delta > reject_thresh):
                breakout_short = False

        if self.require_fvg:
            if breakout_long and not _has_bull_fvg(df, i, self._atr, self.fvg_min_atr, self.fvg_lookback):
                breakout_long = False
            if breakout_short and not _has_bear_fvg(df, i, self._atr, self.fvg_min_atr, self.fvg_lookback):
                breakout_short = False

        long_sig = breakout_long and self.position_size <= 0
        short_sig = breakout_short and self.position_size >= 0

        if long_sig:
            self._day_attempts += 1
            self._peak_profit_pts = 0.0
            raw_stop = self._or_low
            if (close - raw_stop) > self.max_stop_pts:
                raw_stop = close - self.max_stop_pts
            self._current_stop = raw_stop
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close + self.tp_pts)
        elif short_sig:
            self._day_attempts += 1
            self._peak_profit_pts = 0.0
            raw_stop = self._or_high
            if (raw_stop - close) > self.max_stop_pts:
                raw_stop = close + self.max_stop_pts
            self._current_stop = raw_stop
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close - self.tp_pts)


class ICTUnicornPOCOvernightProfit(ICTUnicornPOCOvernightBase):
    """Profit mode overnight: FVG optional, POC reject filter."""
    title = "ICT Unicorn + POC Overnight [Profit]"
    require_fvg = False
    poc_filter_mode = "reject"
    entry_end_hr = 8
    entry_end_mn = 0
    max_attempts = 2
    or_min_pts = 0.0
    or_max_pts = 9999.0
    breakout_margin_pts = 1.0
    min_delta_pct = 5.0
    tp_dollars = 500.0
    max_stop_dollars = 500.0


class ICTUnicornPOCOvernightConsistent(ICTUnicornPOCOvernightBase):
    """Consistent mode overnight: FVG required, stricter filters."""
    title = "ICT Unicorn + POC Overnight [Consistent]"
    require_fvg = True
    poc_filter_mode = "confirm"
    entry_end_hr = 4
    entry_end_mn = 0
    max_attempts = 1
    or_min_pts = 1.0
    or_max_pts = 20.0
    breakout_margin_pts = 1.0
    min_delta_pct = 10.0
    tp_dollars = 350.0
    max_stop_dollars = 300.0
    trail_activate_dollars = 250.0
    trail_keep_pct = 50.0
