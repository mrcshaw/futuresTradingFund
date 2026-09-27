"""
RSI + MACD Reversal Strategy v2 — MACD as Filter, not noise.

Architecture: Trend Filters + Entry Triggers

TREND FILTERS (ALL must agree for direction):
  - RSI above/below 50
  - HTF RSI above/below 50
  - MACD line above/below zero

ENTRY TRIGGERS (need min_triggers to fire):
  - RSI pullback zone crossover
  - RSI divergence at extremes
  - MACD signal line cross
  - MACD histogram flips sign
  - MACD histogram momentum building

Filters set direction. Triggers fire the entry.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


class RsiMacdReversalStrategy(Strategy):
    title = "RSI+MACD Reversal"
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

    rsi_len = 14
    htf_rsi_len = 70
    pullback_lo = 42.0
    pullback_hi = 58.0
    extreme_hi = 80.0
    extreme_lo = 20.0
    div_lookback = 30

    macd_fast = 12
    macd_slow = 26
    macd_signal = 9

    # Filter toggles
    use_htf_filter = True
    use_macd_zero_filter = True

    # Trigger toggles
    use_rsi_pullback = True
    use_rsi_divergence = True
    use_macd_cross = True
    use_macd_hist_flip = True
    use_macd_histogram = True

    min_triggers = 2

    cooldown_bars = 15
    stop_dollars = 300.0
    tp_dollars = 900.0

    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    use_breakeven = False
    be_trigger_dollars = 200.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        self.rsi = ta.rsi(df["close"], self.rsi_len)
        self.htf_rsi = ta.rsi(df["close"], self.htf_rsi_len)
        self.macd_line, self.signal_line, self.histogram = ta.macd(
            df["close"], self.macd_fast, self.macd_slow, self.macd_signal
        )
        self.macd_cross_up = ta.crossover(self.macd_line, self.signal_line)
        self.macd_cross_dn = ta.crossunder(self.macd_line, self.signal_line)
        self._last_trade_bar = -9999
        self._entry_price = 0.0
        self._be_active = False

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

    def _check_divergence(self, i, direction):
        df = self._data
        lb = self.div_lookback
        if i < lb + 2:
            return False
        close = df["close"].values
        rsi_vals = self.rsi.values
        if direction == "bull":
            return (close[i] < close[i - lb] and
                    rsi_vals[i] > rsi_vals[i - lb] and
                    rsi_vals[i - lb] < self.extreme_lo + 10)
        else:
            return (close[i] > close[i - lb] and
                    rsi_vals[i] < rsi_vals[i - lb] and
                    rsi_vals[i - lb] > self.extreme_hi - 10)

    def next(self):
        i = self.bar_index
        df = self._data
        min_bars = max(self.rsi_len, self.htf_rsi_len, self.div_lookback,
                       self.macd_slow + self.macd_signal) + 5
        if i < min_bars:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1]
        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        close_now = df["close"].iloc[i]
        rsi_now = self.rsi.iloc[i]
        rsi_prev = self.rsi.iloc[i - 1]
        htf_rsi_now = self.htf_rsi.iloc[i]
        hist_now = self.histogram.iloc[i]
        hist_prev = self.histogram.iloc[i - 1]
        macd_now = self.macd_line.iloc[i]

        if pd.isna(rsi_now) or pd.isna(htf_rsi_now) or pd.isna(hist_now) or pd.isna(macd_now):
            return

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars

        # ─── TREND FILTERS (all must agree) ───
        filter_long = rsi_now > 50
        filter_short = rsi_now < 50
        if self.use_htf_filter:
            filter_long = filter_long and htf_rsi_now > 50
            filter_short = filter_short and htf_rsi_now < 50
        if self.use_macd_zero_filter:
            filter_long = filter_long and macd_now > 0
            filter_short = filter_short and macd_now < 0

        # ─── ENTRY TRIGGERS (count events) ───
        trig_long = 0
        trig_short = 0

        if self.use_rsi_pullback:
            if rsi_prev <= 50 and rsi_now > 50 and rsi_prev >= self.pullback_lo:
                trig_long += 1
            if rsi_prev >= 50 and rsi_now < 50 and rsi_prev <= self.pullback_hi:
                trig_short += 1

        if self.use_rsi_divergence:
            if self._check_divergence(i, "bull"):
                trig_long += 1
            if self._check_divergence(i, "bear"):
                trig_short += 1

        if self.use_macd_cross:
            if self.macd_cross_up.iloc[i]:
                trig_long += 1
            if self.macd_cross_dn.iloc[i]:
                trig_short += 1

        if self.use_macd_hist_flip:
            if hist_now > 0 and hist_prev <= 0:
                trig_long += 1
            if hist_now < 0 and hist_prev >= 0:
                trig_short += 1

        if self.use_macd_histogram:
            if hist_now > hist_prev and hist_now > 0:
                trig_long += 1
            if hist_now < hist_prev and hist_now < 0:
                trig_short += 1

        long_sig = (filter_long and trig_long >= self.min_triggers and
                    in_session and cooled and self.position_size == 0)
        short_sig = (filter_short and trig_short >= self.min_triggers and
                     in_session and cooled and self.position_size == 0)

        if long_sig and short_sig:
            if trig_long >= trig_short:
                short_sig = False
            else:
                long_sig = False

        if long_sig:
            self._entry_price = close_now
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long",
                      stop=close_now - self.stop_pts, limit=close_now + self.tp_pts)
        elif short_sig:
            self._entry_price = close_now
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short",
                      stop=close_now + self.stop_pts, limit=close_now - self.tp_pts)

        if self.position_size == 0:
            self._be_active = False

        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if close_now - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self.exit("XL", from_entry="Long",
                              stop=self._entry_price + self.tick_size,
                              limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                if self._entry_price - close_now >= self.be_trigger_pts:
                    self._be_active = True
                    self.exit("XS", from_entry="Short",
                              stop=self._entry_price - self.tick_size,
                              limit=self._entry_price - self.tp_pts)
