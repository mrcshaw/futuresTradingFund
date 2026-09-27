"""
Outside Bar Reversal Strategy (from SimpleCandleStrategy01 notebook).

Long: bearish outside bar closing below prev low (reversal up expected)
Short: bullish outside bar closing above prev high (reversal down expected)
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.types import Direction, CommissionType, QtyType


class OutsideBarReversalStrategy(Strategy):
    title = "Outside Bar Reversal"
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

    stop_dollars = 350.0
    tp_dollars = 700.0
    cooldown_bars = 3
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
        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
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

    def next(self):
        i = self.bar_index
        df = self._data

        if i < 1:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1]

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        open_ = df["open"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        close = df["close"].iloc[i]
        prev_high = df["high"].iloc[i - 1]
        prev_low = df["low"].iloc[i - 1]

        valid_bar = high != low
        is_bearish = open_ > close
        is_bullish = open_ < close
        engulfs_high = high > prev_high
        engulfs_low = low < prev_low
        close_below = close < prev_low
        close_above = close > prev_high

        long_pattern = valid_bar and is_bearish and engulfs_high and engulfs_low and close_below
        short_pattern = valid_bar and is_bullish and engulfs_high and engulfs_low and close_above

        cooled_down = i - self._last_trade_bar >= self.cooldown_bars
        in_session = self._in_session(ts)

        long_sig = long_pattern and in_session and cooled_down and self.position_size == 0
        short_sig = short_pattern and in_session and cooled_down and self.position_size == 0

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
                unrealized = close - self._entry_price
                if unrealized >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + self.tick_size
                    self.exit("XL", from_entry="Long", stop=self._current_stop, limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                unrealized = self._entry_price - close
                if unrealized >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - self.tick_size
                    self.exit("XS", from_entry="Short", stop=self._current_stop, limit=self._entry_price - self.tp_pts)
