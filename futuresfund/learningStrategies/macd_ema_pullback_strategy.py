"""
MACD EMA Pullback Strategy — Evening Session ES 2min.

Trend: 15 SMA crosses 55 EMA sets direction.
Entry: Price pulls back near 55 EMA (within buffer), MACD histogram
       confirms momentum, ATR above threshold.
Confluence: configurable min_confluence (each factor = 1 point).
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


class MacdEmaPullbackStrategy(Strategy):
    title = "MACD EMA Pullback"
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

    fast_ma_len = 15
    slow_ma_len = 55
    macd_fast = 12
    macd_slow = 26
    macd_signal = 9
    atr_len = 14
    atr_threshold = 3.0
    pullback_buffer = 2.0
    min_confluence = 3
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
        self.fast_ma = ta.sma(df["close"], self.fast_ma_len)
        self.slow_ma = ta.ema(df["close"], self.slow_ma_len)
        self.macd_line, self.signal_line, self.histogram = ta.macd(
            df["close"], self.macd_fast, self.macd_slow, self.macd_signal
        )
        self.atr_val = ta.atr(df["high"], df["low"], df["close"], self.atr_len)
        self.cross_up = ta.crossover(self.fast_ma, self.slow_ma)
        self.cross_dn = ta.crossunder(self.fast_ma, self.slow_ma)

        self._trend = 0
        self._last_trade_bar = -9999
        self._current_stop = 0.0
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
        min_bars = max(self.slow_ma_len, self.macd_slow + self.macd_signal, self.atr_len) + 2
        if i < min_bars:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1]

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")
            self._trend = 0

        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]

        fast_now = self.fast_ma.iloc[i]
        slow_now = self.slow_ma.iloc[i]
        hist_now = self.histogram.iloc[i]
        hist_prev = self.histogram.iloc[i - 1] if i > 0 else 0
        atr_now = self.atr_val.iloc[i]

        if pd.isna(fast_now) or pd.isna(slow_now) or pd.isna(hist_now) or pd.isna(atr_now):
            return

        if self.cross_up.iloc[i]:
            self._trend = 1
        elif self.cross_dn.iloc[i]:
            self._trend = -1

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars

        # Confluence factors
        confluence_long = 0
        confluence_short = 0

        # 1) ATR above threshold
        atr_ok = atr_now >= self.atr_threshold
        if atr_ok:
            confluence_long += 1
            confluence_short += 1

        # 2) Trend direction (15 SMA crossed above/below 55 EMA)
        if self._trend == 1:
            confluence_long += 1
        elif self._trend == -1:
            confluence_short += 1

        # 3) Pullback: price near 55 EMA (within buffer)
        dist_to_slow = close - slow_now
        pullback_long = self._trend == 1 and abs(dist_to_slow) <= self.pullback_buffer
        pullback_short = self._trend == -1 and abs(dist_to_slow) <= self.pullback_buffer
        if pullback_long:
            confluence_long += 1
        if pullback_short:
            confluence_short += 1

        # 4) MACD histogram momentum (growing in trade direction)
        macd_momentum_long = hist_now > hist_prev and hist_now > 0
        macd_momentum_short = hist_now < hist_prev and hist_now < 0
        if macd_momentum_long:
            confluence_long += 1
        if macd_momentum_short:
            confluence_short += 1

        # 5) Fast MA still above/below slow (trend intact)
        if fast_now > slow_now:
            confluence_long += 1
        if fast_now < slow_now:
            confluence_short += 1

        long_sig = (confluence_long >= self.min_confluence and in_session
                    and cooled and self.position_size == 0)
        short_sig = (confluence_short >= self.min_confluence and in_session
                     and cooled and self.position_size == 0)

        if long_sig:
            self._current_stop = close - self.stop_pts
            self._entry_price = close
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close + self.tp_pts)
        elif short_sig:
            self._current_stop = close + self.stop_pts
            self._entry_price = close
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close - self.tp_pts)

        if self.position_size == 0:
            self._be_active = False

        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if close - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + self.tick_size
                    self.exit("XL", from_entry="Long", stop=self._current_stop,
                              limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                if self._entry_price - close >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - self.tick_size
                    self.exit("XS", from_entry="Short", stop=self._current_stop,
                              limit=self._entry_price - self.tp_pts)
