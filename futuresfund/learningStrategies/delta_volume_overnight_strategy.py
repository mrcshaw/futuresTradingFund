"""
Delta Volume Breakout — Overnight (18:00–8:00 EST) low-risk variant.
Tighter stops, breakeven, stricter filters for evening volatility.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType

from strategies.delta_volume_breakout_strategy import DeltaVolumeBreakoutStrategy


class DeltaVolumeOvernightStrategy(DeltaVolumeBreakoutStrategy):
    """
    Overnight DVB with low-risk defaults:
    - Tighter stop (250)
    - Breakeven at 100
    - Higher min_delta (12%)
    - More confirm candles (4)
    - Longer cooldown (15)
    - Max 2 trades per session
    """

    title = "Delta Volume Breakout Night Shift"

    # Overnight 18:00–8:00 EST (includes Asian session ~2am)
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    # Very strict filters
    min_delta_pct = 18.0
    confirm_candles = 5
    cooldown_bars = 30
    profile_lookback = 80

    # Tight risk params
    stop_dollars = 200.0
    tp_dollars = 350.0
    use_trail = False
    use_breakeven = True
    be_trigger_dollars = 75.0

    # Max 1 trade per session
    max_trades_per_session = 1

    ema_len = 50
    min_slope = 0.0

    # Require price on correct side of EMA for direction conviction
    require_price_above_ema_long = True
    require_price_below_ema_short = True

    def init(self):
        super().init()
        self._session_trades = 0
        self._last_session_date = None

    def next(self):
        i = self.bar_index
        df = self._data
        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts
        pos_before = self.position_size

        # Reset session trade count when we enter a new overnight session
        if self.use_session:
            try:
                if hasattr(ts, "tz_convert"):
                    ts_et = ts.tz_convert("America/New_York")
                else:
                    ts_et = ts
                hr, mn = ts_et.hour, ts_et.minute
                bar_t = hr * 100 + mn
                s_start = self.sess_start_hr * 100 + self.sess_start_mn
                s_end = self.sess_end_hr * 100 + self.sess_end_mn
                in_now = bar_t >= s_start or bar_t < s_end
                if hasattr(prev_ts, "tz_convert"):
                    prev_et = prev_ts.tz_convert("America/New_York")
                else:
                    prev_et = prev_ts
                prev_t = prev_et.hour * 100 + prev_et.minute
                in_prev = prev_t >= s_start or prev_t < s_end
                if in_now and not in_prev:
                    self._session_trades = 0
            except Exception:
                pass

        # Block new entries if we've hit max trades this session
        if getattr(self, "_session_trades", 0) >= self.max_trades_per_session:
            old_cooldown = self.cooldown_bars
            self.cooldown_bars = 9999  # Effectively block
            super().next()
            self.cooldown_bars = old_cooldown
        else:
            super().next()

        # Count new entry this session
        if pos_before == 0 and self.position_size != 0:
            self._session_trades = getattr(self, "_session_trades", 0) + 1
