"""
ICT Unicorn + ORB Hybrid - Combines Post-Open POC with FVG confirmation.

Uses ORB for timing/structure, ICT Unicorn FVG for direction confirmation.
- Consistent: require FVG in breakout direction, 1:1 RR, fewer trades
- Profit: FVG optional, 1:1.5 RR, more trades
"""

import pandas as pd
import numpy as np
from typing import Optional

from engine.strategy import Strategy
from engine.types import Direction, CommissionType, QtyType


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def _has_bull_fvg(df: pd.DataFrame, bar: int, atr: pd.Series, min_atr: float, lookback: int) -> bool:
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


def _has_bear_fvg(df: pd.DataFrame, bar: int, atr: pd.Series, min_atr: float, lookback: int) -> bool:
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


class ICTUnicornStrategy(Strategy):
    """ORB + ICT Unicorn FVG hybrid."""

    title = "ICT Unicorn + ORB"
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

    mode = "Profit"
    require_fvg = False  # Consistent: True, Profit: False
    fvg_lookback = 30
    fvg_min_atr = 0.04
    atr_len = 14

    gather_minutes = 15
    entry_end_hr = 11
    entry_end_mn = 0
    close_eod_hr = 15
    close_eod_mn = 45
    rth_start_hr = 9
    rth_start_mn = 30
    rth_end_hr = 16
    rth_end_mn = 0

    confirm_bars = 1
    max_attempts = 2
    or_min_pts = 0.0
    or_max_pts = 9999.0
    breakout_margin_pts = 1.0
    max_stop_dollars = 9999.0
    stop_mode = "range"
    stop_dollars = 200.0
    tp_dollars = 500.0
    use_trail = True
    trail_activate_dollars = 400.0
    trail_keep_pct = 40.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.trail_activate_pts = self.trail_activate_dollars / self.point_value
        self.max_stop_pts = self.max_stop_dollars / self.point_value
        if self.mode == "Consistent":
            self.require_fvg = True
            self.fvg_min_atr = 0.05
        else:
            self.require_fvg = False
            self.fvg_min_atr = 0.04

    def init(self):
        df = self._data
        if len(df) < 50:
            self._atr = pd.Series(0.0, index=df.index)
            return
        self._atr = _atr(df["high"], df["low"], df["close"], self.atr_len)
        self._last_day = None
        self._day_attempts = 0
        self._or_high = 0.0
        self._or_low = 99999.0
        self._or_done = False
        self._or_valid = False
        self._peak_profit_pts = 0.0
        self._current_stop = 0.0

    def _parse_ts(self, ts):
        try:
            df = self._data
            if len(df) > 0 and hasattr(df.index[0], "tz_convert") and hasattr(ts, "tz_convert"):
                et = ts.tz_convert("America/New_York")
            else:
                et = ts
            return et.hour, et.minute, et.date() if hasattr(et, "date") else None
        except Exception:
            return 0, 0, None

    def next(self):
        i = self.bar_index
        df = self._data
        if i < 1:
            return

        hr, mn, today = self._parse_ts(df.index[i])
        if today is None:
            return

        bar_t = hr * 100 + mn
        close = float(df["close"].iloc[i])
        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])
        rth_s = self.rth_start_hr * 100 + self.rth_start_mn

        if bar_t < rth_s:
            return

        if today != self._last_day:
            self._last_day = today
            self._day_attempts = 0
            self._or_high = 0.0
            self._or_low = 99999.0
            self._or_done = False
            self._or_valid = False

        mins = (hr - 9) * 60 + (mn - 30)
        if mins < self.gather_minutes:
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

        if bar_t >= (self.close_eod_hr * 100 + self.close_eod_mn) and self.position_size != 0:
            self.close_all(comment="EOD")
            return

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

        end_t = self.entry_end_hr * 100 + self.entry_end_mn
        if bar_t >= end_t or self._day_attempts >= self.max_attempts:
            return

        breakout_long = close > (self._or_high + self.breakout_margin_pts)
        breakout_short = close < (self._or_low - self.breakout_margin_pts)

        confirmed_long = confirmed_short = True
        for k in range(self.confirm_bars):
            idx = i - k
            if idx < 0:
                confirmed_long = confirmed_short = False
                break
            if df["close"].iloc[idx] <= self._or_high:
                confirmed_long = False
            if df["close"].iloc[idx] >= self._or_low:
                confirmed_short = False

        if self.require_fvg:
            if breakout_long and not _has_bull_fvg(df, i, self._atr, self.fvg_min_atr, self.fvg_lookback):
                confirmed_long = False
            if breakout_short and not _has_bear_fvg(df, i, self._atr, self.fvg_min_atr, self.fvg_lookback):
                confirmed_short = False

        long_sig = breakout_long and confirmed_long and self.position_size <= 0
        short_sig = breakout_short and confirmed_short and self.position_size >= 0

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
        elif self.position_size > 0 and not self.use_trail:
            avg = self.position_avg_price
            self.exit("XL", from_entry="Long", stop=avg - self.stop_pts, limit=avg + self.tp_pts)
        elif self.position_size < 0 and not self.use_trail:
            avg = self.position_avg_price
            self.exit("XS", from_entry="Short", stop=avg + self.stop_pts, limit=avg - self.tp_pts)
