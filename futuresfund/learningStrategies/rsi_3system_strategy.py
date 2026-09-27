"""
RSI 3-System Strategy — based on The Secret Mindset method.

System 1 - Trend: RSI 50 line determines direction.
  Above 50 = only longs. Below 50 = only shorts.

System 2 - Pullback Zone Entry: Wait for RSI pullback to 45-50 zone
  (uptrend) or 50-55 (downtrend), then enter when RSI crosses back
  through 50. Acceleration signal if RSI breaks 60/40 quickly.

System 3 - Divergence: Bearish divergence (price HH, RSI LH) near 80.
  Bullish divergence (price LL, RSI HL) near 20.
  Double-tap failures at extremes. Min 20-bar development.

Higher-timeframe bias simulated with a longer-period RSI.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


class Rsi3SystemStrategy(Strategy):
    title = "RSI 3-System"
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
    htf_rsi_len = 50
    pullback_lo = 45.0
    pullback_hi = 55.0
    extreme_hi = 80.0
    extreme_lo = 20.0
    div_lookback = 20
    use_divergence = True
    use_pullback = True
    use_htf_bias = True
    min_systems = 2

    cooldown_bars = 5
    stop_dollars = 300.0
    tp_dollars = 600.0

    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    use_breakeven = False
    be_trigger_dollars = 150.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        self.rsi = ta.rsi(df["close"], self.rsi_len)
        self.htf_rsi = ta.rsi(df["close"], self.htf_rsi_len)

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
        """Check for RSI divergence over div_lookback bars."""
        df = self._data
        lb = self.div_lookback
        if i < lb + 2:
            return False

        close = df["close"].values
        rsi_vals = self.rsi.values

        if direction == "bull":
            if close[i] >= close[i - lb]:
                return False
            if rsi_vals[i] <= rsi_vals[i - lb]:
                return False
            if rsi_vals[i - lb] > self.extreme_lo + 10:
                return False
            return True
        else:
            if close[i] <= close[i - lb]:
                return False
            if rsi_vals[i] >= rsi_vals[i - lb]:
                return False
            if rsi_vals[i - lb] < self.extreme_hi - 10:
                return False
            return True

    def next(self):
        i = self.bar_index
        df = self._data
        min_bars = max(self.rsi_len, self.htf_rsi_len, self.div_lookback) + 5
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

        if pd.isna(rsi_now) or pd.isna(htf_rsi_now):
            return

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars

        # System 1: RSI 50-line trend
        rsi_bullish = rsi_now > 50
        rsi_bearish = rsi_now < 50

        # HTF bias
        htf_bullish = htf_rsi_now > 50 if self.use_htf_bias else True
        htf_bearish = htf_rsi_now < 50 if self.use_htf_bias else True

        # System 2: Pullback zone crossover
        pullback_long = False
        pullback_short = False
        if self.use_pullback:
            pullback_long = rsi_prev <= 50 and rsi_now > 50 and rsi_prev >= self.pullback_lo
            pullback_short = rsi_prev >= 50 and rsi_now < 50 and rsi_prev <= self.pullback_hi

        # System 3: Divergence
        div_long = self._check_divergence(i, "bull") if self.use_divergence else False
        div_short = self._check_divergence(i, "bear") if self.use_divergence else False

        # Count systems confirming
        long_score = 0
        short_score = 0

        if rsi_bullish:
            long_score += 1
        if rsi_bearish:
            short_score += 1
        if htf_bullish:
            long_score += 1
        if htf_bearish:
            short_score += 1
        if pullback_long:
            long_score += 1
        if pullback_short:
            short_score += 1
        if div_long:
            long_score += 1
        if div_short:
            short_score += 1

        long_sig = long_score >= self.min_systems and in_session and cooled and self.position_size == 0
        short_sig = short_score >= self.min_systems and in_session and cooled and self.position_size == 0

        if long_sig and not short_sig:
            self._entry_price = close_now
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long",
                      stop=close_now - self.stop_pts, limit=close_now + self.tp_pts)
        elif short_sig and not long_sig:
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
