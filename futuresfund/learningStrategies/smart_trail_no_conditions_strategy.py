"""
Smart Trail NO CONDITIONS — ES Evening Session 2min.

Entry on trend flip (trail_up/trail_down style from the NO CONDITIONS indicator).
Risk: stop, TP, optional trailing stop, reversal on stop-out.
Session: 18:00–9:00 ET.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _smart_trail_no_conditions(high, low, close, length=14, multiplier=2.0, sensitivity=3):
    """Trail_up / trail_down style from NO CONDITIONS indicator."""
    n = len(close)
    atr_val = ta.atr(high, low, close, length)
    vol_factor = atr_val * multiplier * (sensitivity / 3.0)
    basic_up = close - vol_factor
    basic_dn = close + vol_factor

    trail_up = np.full(n, np.nan)
    trail_dn = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)

    for i in range(1, n):
        s = close.iloc[i]
        s_prev = close.iloc[i - 1]
        tu_prev = trail_up[i - 1] if not np.isnan(trail_up[i - 1]) else basic_up.iloc[i]
        td_prev = trail_dn[i - 1] if not np.isnan(trail_dn[i - 1]) else basic_dn.iloc[i]

        if s > tu_prev and s_prev > tu_prev:
            trail_up[i] = max(tu_prev, basic_up.iloc[i])
        else:
            trail_up[i] = basic_up.iloc[i]

        if s < td_prev and s_prev < td_prev:
            trail_dn[i] = min(td_prev, basic_dn.iloc[i])
        else:
            trail_dn[i] = basic_dn.iloc[i]

        if s > td_prev:
            trend[i] = 1
        elif s < tu_prev:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]

    return (
        pd.Series(trail_up, index=close.index),
        pd.Series(trail_dn, index=close.index),
        pd.Series(trend, index=close.index),
    )


class SmartTrailNoConditionsStrategy(Strategy):
    title = "Smart Trail NO CONDITIONS ES Evening"
    initial_capital = 25000.0
    default_qty_type = QtyType.FIXED
    default_qty_value = 1.0
    commission_type = CommissionType.PER_CONTRACT
    commission_value = 5.50
    slippage = 3
    pyramiding = 0
    process_orders_on_close = True
    is_futures = True
    point_value = 50.0
    tick_size = 0.25

    length = 14
    multiplier = 2.0
    sensitivity = 3

    stop_dollars = 300.0
    tp_dollars = 500.0
    use_trail = False
    trail_pct = 75.0
    trail_step_amt = 100.0
    trail_step_pct = 80.0
    trail_min_dollars = 200.0

    use_reversal = True
    rev_stop_dollars = 300.0
    rev_tp_dollars = 500.0

    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 9
    sess_end_mn = 0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.rev_stop_pts = self.rev_stop_dollars / self.point_value
        self.rev_tp_pts = self.rev_tp_dollars / self.point_value
        self.trail_min_pts = self.trail_min_dollars / self.point_value

    def init(self):
        df = self._data
        tu, td, tr = _smart_trail_no_conditions(
            df["high"], df["low"], df["close"], self.length, self.multiplier, self.sensitivity
        )
        self._trail_up = tu
        self._trail_dn = td
        self._trend = tr
        self._trend_changed = tr.diff().fillna(0) != 0
        self._smart_trail_value = tu.where(tr == 1, td)

        self._current_stop = 0.0
        self._peak_profit_pts = 0.0
        self._entry_price = 0.0
        self._last_dir = 0
        self._is_reversal = False
        self._reversal_pending = False

    def _in_session(self, ts):
        if not self.use_session:
            return True
        try:
            ts_et = ts.tz_convert("America/New_York") if hasattr(ts, "tz_convert") else ts
            hr, mn = ts_et.hour, ts_et.minute
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
        if i < self.length + 2:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1]

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")
            self._last_dir = 0

        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]

        trend_now = int(self._trend.iloc[i])
        trend_prev = int(self._trend.iloc[i - 1]) if i > 0 else 1
        changed = trend_now != trend_prev

        bull = changed and trend_now == 1
        bear = changed and trend_now == -1

        in_session = self._in_session(ts)

        if bull and in_session and self.position_size <= 0:
            self._entry_price = close
            self._current_stop = close - self.stop_pts
            self._peak_profit_pts = 0.0
            self._last_dir = 1
            self._is_reversal = False
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close + self.tp_pts)
        elif bear and in_session and self.position_size >= 0:
            self._entry_price = close
            self._current_stop = close + self.stop_pts
            self._peak_profit_pts = 0.0
            self._last_dir = -1
            self._is_reversal = False
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close - self.tp_pts)

        # Detect stop-out -> queue reversal
        _prev_pos = getattr(self, "_prev_pos", 0)
        if self.position_size == 0 and _prev_pos != 0:
            if not self._is_reversal and self._last_dir != 0:
                ct = self._broker.closed_trades
                if ct and ct[-1].profit < 0:
                    self._reversal_pending = True
            self._peak_profit_pts = 0.0
            self._is_reversal = False

        self._prev_pos = self.position_size

        # Reversal trade
        if self.use_reversal and self._reversal_pending and self.position_size == 0 and in_session:
            self._reversal_pending = False
            self._is_reversal = True
            self._peak_profit_pts = 0.0
            if self._last_dir == 1:
                self._current_stop = close + self.rev_stop_pts
                self._last_dir = -1
                self.entry("RevShort", Direction.SHORT)
                self.exit("XRS", from_entry="RevShort", stop=self._current_stop, limit=close - self.rev_tp_pts)
            elif self._last_dir == -1:
                self._current_stop = close - self.rev_stop_pts
                self._last_dir = 1
                self.entry("RevLong", Direction.LONG)
                self.exit("XRL", from_entry="RevLong", stop=self._current_stop, limit=close + self.rev_tp_pts)

        # Trailing stop
        if self.use_trail and self.position_size != 0:
            avg = self._broker.avg_entry_price
            if self.position_size > 0:
                profit_now = high - avg
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_min_pts:
                    peak_d = self._peak_profit_pts * self.point_value
                    steps = int(peak_d / self.trail_step_amt)
                    keep_pct = min(self.trail_pct + steps * self.trail_step_pct, 95.0)
                    new_stop = avg + self._peak_profit_pts * (keep_pct / 100.0)
                    if new_stop > self._current_stop:
                        self._current_stop = new_stop
                        self.exit("XL", from_entry="Long", stop=self._current_stop, limit=avg + self.tp_pts)
            else:
                profit_now = avg - low
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_min_pts:
                    peak_d = self._peak_profit_pts * self.point_value
                    steps = int(peak_d / self.trail_step_amt)
                    keep_pct = min(self.trail_pct + steps * self.trail_step_pct, 95.0)
                    new_stop = avg - self._peak_profit_pts * (keep_pct / 100.0)
                    if new_stop < self._current_stop:
                        self._current_stop = new_stop
                        self.exit("XS", from_entry="Short", stop=self._current_stop, limit=avg - self.tp_pts)
