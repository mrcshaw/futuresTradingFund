"""
EMA + Bollinger Band Mean Reversion Strategy.

Logic from notebook:
- EMA trend filter: fast EMA above/below slow EMA for backcandles (all bars must align)
- Long: ema_signal==2 (uptrend) AND close <= BBL (price at/below lower band)
- Short: ema_signal==1 (downtrend) AND close >= BBU (price at/above upper band)

Uses ATR-based stop and take profit.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


class EMABBMeanReversionStrategy(Strategy):
    title = "EMA BB Mean Reversion"
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

    # EMA
    ema_slow = 50
    ema_fast = 30
    ema_backcandles = 7

    # Bollinger Bands
    bb_length = 15
    bb_std = 1.5

    # ATR for SL/TP
    atr_length = 7
    sl_atr_mult = 1.1
    tp_sl_ratio = 1.5

    # Cooldown
    cooldown_bars = 3

    def init(self):
        df = self._data
        close = df["close"]

        self.ema_slow_vals = ta.ema(close, self.ema_slow)
        self.ema_fast_vals = ta.ema(close, self.ema_fast)
        self.bb_upper, self.bb_middle, self.bb_lower = ta.bb(close, self.bb_length, self.bb_std)
        self.atr_vals = ta.atr(df["high"], df["low"], df["close"], self.atr_length)

        # Precompute EMA signal: 1=all fast<slow (bear), 2=all fast>slow (bull), 0=mixed
        n = len(df)
        self._ema_signal = np.zeros(n, dtype=int)
        bc = self.ema_backcandles
        for i in range(bc, n):
            fast_slice = self.ema_fast_vals.iloc[i - bc : i].values
            slow_slice = self.ema_slow_vals.iloc[i - bc : i].values
            if np.any(np.isnan(fast_slice)) or np.any(np.isnan(slow_slice)):
                continue
            if np.all(fast_slice < slow_slice):
                self._ema_signal[i] = 1
            elif np.all(fast_slice > slow_slice):
                self._ema_signal[i] = 2

        self._last_trade_bar = -9999

    def next(self):
        i = self.bar_index
        df = self._data

        min_bars = max(self.ema_slow, self.ema_fast, self.bb_length, self.atr_length) + self.ema_backcandles
        if i < min_bars:
            return

        cl = df["close"].iloc[i]
        ema_sig = self._ema_signal[i]
        bbl = self.bb_lower.iloc[i]
        bbu = self.bb_upper.iloc[i]
        atr = self.atr_vals.iloc[i]

        if pd.isna(bbl) or pd.isna(bbu) or pd.isna(atr) or atr <= 0:
            return

        cooled_down = i - self._last_trade_bar >= self.cooldown_bars

        # Long: uptrend + price at/below lower BB
        long_sig = ema_sig == 2 and cl <= bbl and cooled_down and self.position_size == 0

        # Short: downtrend + price at/above upper BB
        short_sig = ema_sig == 1 and cl >= bbu and cooled_down and self.position_size == 0

        sl_dist = atr * self.sl_atr_mult
        tp_dist = sl_dist * self.tp_sl_ratio

        if long_sig:
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=cl - sl_dist, limit=cl + tp_dist)
        elif short_sig:
            self._last_trade_bar = i
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=cl + sl_dist, limit=cl - tp_dist)
