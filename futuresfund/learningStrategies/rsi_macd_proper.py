"""
RSI + MACD Strategy — Proper Pivot-Based Implementation.

Uses exact logic from TradingView's built-in RSI Divergence and MACD indicators.

RSI Divergence (pivot-based):
  - ta.pivotlow/ta.pivothigh with lookback left/right
  - Regular bullish: price lower low + RSI higher low at pivots
  - Regular bearish: price higher high + RSI lower high at pivots
  - Pivots must be within configurable range (5-60 bars default)

MACD:
  - Standard 12/26/9 EMA-based
  - Histogram 4-state coloring (momentum analysis)
  - Signal line crossovers

Architecture: Filters gate direction, Triggers fire entries.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


class RsiMacdProperStrategy(Strategy):
    title = "RSI+MACD Proper"
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

    # RSI
    rsi_len = 14
    htf_rsi_len = 70

    # Divergence (pivot-based, matching TradingView indicator)
    pivot_left = 5
    pivot_right = 5
    range_lower = 5
    range_upper = 60
    div_window = 3  # bars after divergence detected to keep signal active

    # RSI pullback
    pullback_lo = 42.0
    pullback_hi = 58.0

    # MACD
    macd_fast = 12
    macd_slow = 26
    macd_signal = 9

    # Filter toggles
    use_rsi_50_filter = True
    use_htf_filter = True

    # Trigger toggles
    use_rsi_divergence = True
    use_rsi_pullback = True
    use_macd_cross = True
    use_macd_hist_flip = True
    use_macd_histogram = True

    min_triggers = 1

    cooldown_bars = 20
    stop_dollars = 300.0
    tp_dollars = 1000.0

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
        close = df["close"]
        low = df["low"]
        high = df["high"]

        # ── RSI ──
        self.rsi = ta.rsi(close, self.rsi_len)
        self.htf_rsi = ta.rsi(close, self.htf_rsi_len)

        # ── Pivot-based divergence (matching Pine Script indicator exactly) ──
        lb_l = self.pivot_left
        lb_r = self.pivot_right

        # Raw pivot detection on RSI series
        pl_raw = ta.pivotlow(self.rsi, lb_l, lb_r)   # NaN where no pivot, value where pivot
        ph_raw = ta.pivothigh(self.rsi, lb_l, lb_r)

        # The engine marks pivots at the actual bar. Pine marks them at
        # confirmation (lb_r bars later). Shift to match Pine behavior.
        pl_at_actual = ~pl_raw.isna()
        ph_at_actual = ~ph_raw.isna()
        pl_confirmed = pl_at_actual.shift(lb_r).fillna(False).astype(bool)
        ph_confirmed = ph_at_actual.shift(lb_r).fillna(False).astype(bool)

        # Values at pivot point (on confirmation bar = rsi[lbR] in Pine)
        rsi_at_pl = self.rsi.shift(lb_r)
        rsi_at_ph = self.rsi.shift(lb_r)
        low_at_pl = low.shift(lb_r)
        high_at_ph = high.shift(lb_r)

        # Previous pivot values (occurrence=1 = previous time condition was true)
        prev_pl_rsi = ta.valuewhen(pl_confirmed, rsi_at_pl, 1)
        prev_ph_rsi = ta.valuewhen(ph_confirmed, rsi_at_ph, 1)
        prev_pl_low = ta.valuewhen(pl_confirmed, low_at_pl, 1)
        prev_ph_high = ta.valuewhen(ph_confirmed, high_at_ph, 1)

        # _inRange: bars since PREVIOUS pivot must be within [range_lower, range_upper]
        barssince_prev_pl = ta.barssince(pl_confirmed.shift(1).fillna(False))
        barssince_prev_ph = ta.barssince(ph_confirmed.shift(1).fillna(False))
        in_range_pl = (barssince_prev_pl >= self.range_lower) & (barssince_prev_pl <= self.range_upper)
        in_range_ph = (barssince_prev_ph >= self.range_lower) & (barssince_prev_ph <= self.range_upper)

        # Regular Bullish Divergence: price lower low + RSI higher low
        rsi_hl = (rsi_at_pl > prev_pl_rsi) & in_range_pl
        price_ll = low_at_pl < prev_pl_low
        self.bull_div_raw = (pl_confirmed & rsi_hl & price_ll).fillna(False)

        # Regular Bearish Divergence: price higher high + RSI lower high
        rsi_lh = (rsi_at_ph < prev_ph_rsi) & in_range_ph
        price_hh = high_at_ph > prev_ph_high
        self.bear_div_raw = (ph_confirmed & rsi_lh & price_hh).fillna(False)

        # Divergence window: keep signal active for N bars after detection
        self.bull_div = self.bull_div_raw.copy()
        self.bear_div = self.bear_div_raw.copy()
        if self.div_window > 0:
            for shift in range(1, self.div_window + 1):
                self.bull_div = self.bull_div | self.bull_div_raw.shift(shift).fillna(False)
                self.bear_div = self.bear_div | self.bear_div_raw.shift(shift).fillna(False)

        # ── MACD ──
        self.macd_line, self.signal_line, self.histogram = ta.macd(
            close, self.macd_fast, self.macd_slow, self.macd_signal
        )
        self.macd_cross_up = ta.crossover(self.macd_line, self.signal_line)
        self.macd_cross_dn = ta.crossunder(self.macd_line, self.signal_line)

        # ── State ──
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

    def next(self):
        i = self.bar_index
        df = self._data
        warmup = max(self.rsi_len, self.htf_rsi_len,
                     self.macd_slow + self.macd_signal,
                     self.pivot_left + self.pivot_right + self.range_upper) + 10
        if i < warmup:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1]
        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        rsi_now = self.rsi.iloc[i]
        htf_rsi_now = self.htf_rsi.iloc[i]
        hist_now = self.histogram.iloc[i]
        hist_prev = self.histogram.iloc[i - 1]
        rsi_prev = self.rsi.iloc[i - 1]

        if pd.isna(rsi_now) or pd.isna(htf_rsi_now) or pd.isna(hist_now):
            return

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars
        close_now = df["close"].iloc[i]

        # ─── TREND FILTERS ───
        filter_long = True
        filter_short = True
        if self.use_rsi_50_filter:
            filter_long = filter_long and rsi_now > 50
            filter_short = filter_short and rsi_now < 50
        if self.use_htf_filter:
            filter_long = filter_long and htf_rsi_now > 50
            filter_short = filter_short and htf_rsi_now < 50

        # ─── ENTRY TRIGGERS ───
        trig_long = 0
        trig_short = 0

        # T1: RSI Divergence (proper pivot-based)
        if self.use_rsi_divergence:
            if self.bull_div.iloc[i]:
                trig_long += 1
            if self.bear_div.iloc[i]:
                trig_short += 1

        # T2: RSI Pullback zone crossover
        if self.use_rsi_pullback:
            if rsi_prev <= 50 and rsi_now > 50 and rsi_prev >= self.pullback_lo:
                trig_long += 1
            if rsi_prev >= 50 and rsi_now < 50 and rsi_prev <= self.pullback_hi:
                trig_short += 1

        # T3: MACD signal cross
        if self.use_macd_cross:
            if self.macd_cross_up.iloc[i]:
                trig_long += 1
            if self.macd_cross_dn.iloc[i]:
                trig_short += 1

        # T4: MACD histogram sign flip
        if self.use_macd_hist_flip:
            if hist_now > 0 and hist_prev <= 0:
                trig_long += 1
            if hist_now < 0 and hist_prev >= 0:
                trig_short += 1

        # T5: MACD histogram momentum
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
