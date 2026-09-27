"""
UAlgo Trend Signals — SuperTrend flip entries with multi-layer filters.

Core: ATR-based SuperTrend trend detection. Entry when trend flips direction.
Filters (all optional, toggle independently):
  - EMA slope: require EMA slope in trade direction above min threshold
  - MACD cloud: only trade when MACD histogram agrees with direction
  - RSI guard: block longs when overbought, shorts when oversold
  - Volume: only trade when volume > SMA(volume)
  - Cooldown: minimum bars between trades

Risk management:
  - Fixed dollar SL / TP
  - Breakeven trigger
  - Reversal on stop loss (half risk, 2:1 RR)

Trade legend: each entry comment describes the context (slope, RSI, MACD state).
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_supertrend(high, low, close, atr_length, multiplier):
    """SuperTrend matching the UAlgo indicator's band-ratcheting logic."""
    n = len(close)
    atr_val = ta.atr(high, low, close, atr_length)
    hl2 = (high + low) / 2.0
    raw_up = hl2 - multiplier * atr_val
    raw_dn = hl2 + multiplier * atr_val

    up_band = np.full(n, np.nan)
    dn_band = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)
    trail = np.full(n, np.nan)

    for i in range(atr_length, n):
        u = raw_up.iloc[i]
        d = raw_dn.iloc[i]
        c = close.iloc[i]
        c1 = close.iloc[i - 1]

        if i == atr_length:
            up_band[i] = u
            dn_band[i] = d
            trend[i] = 1
            trail[i] = u
            continue

        up_band[i] = max(u, up_band[i - 1]) if c1 > up_band[i - 1] else u
        dn_band[i] = min(d, dn_band[i - 1]) if c1 < dn_band[i - 1] else d

        prev_trend = trend[i - 1]
        if prev_trend == -1 and c > dn_band[i - 1]:
            trend[i] = 1
        elif prev_trend == 1 and c < up_band[i - 1]:
            trend[i] = -1
        else:
            trend[i] = prev_trend

        trail[i] = up_band[i] if trend[i] == 1 else dn_band[i]

    return (pd.Series(trail, index=close.index),
            pd.Series(trend, index=close.index))


class TrendSignalsStrategy(Strategy):
    title = "UAlgo Trend Signals"
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

    # -- SuperTrend params --
    multiplier = 2.0
    atr_length = 12

    # -- EMA slope filter --
    use_slope_filter = True
    slope_ema_len = 30
    slope_lookback = 5
    min_slope = 0.75

    # -- MACD cloud filter --
    use_macd_filter = False
    macd_fast = 12
    macd_slow = 26
    macd_signal = 9

    # -- RSI guard --
    use_rsi_filter = False
    rsi_length = 14
    rsi_ob = 70
    rsi_os = 30

    # -- Volume filter --
    use_volume_filter = False
    vol_ma_len = 20

    # -- Cooldown --
    cooldown_bars = 7

    # -- Session --
    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    # -- Risk --
    stop_dollars = 550.0
    tp_dollars = 600.0
    use_breakeven = True
    be_trigger_dollars = 200.0
    use_reversal = False

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data

        self.trail_line, self.trend_line = _compute_supertrend(
            df["high"], df["low"], df["close"], self.atr_length, self.multiplier)

        self.ema_slope_series = pd.Series(np.nan, index=df.index)
        if self.use_slope_filter:
            ema = ta.ema(df["close"], self.slope_ema_len)
            self.ema_slope_series = ema - ema.shift(self.slope_lookback)

        self.macd_hist = pd.Series(0.0, index=df.index)
        if self.use_macd_filter:
            _, _, self.macd_hist = ta.macd(df["close"], self.macd_fast, self.macd_slow, self.macd_signal)

        self.rsi_series = pd.Series(50.0, index=df.index)
        if self.use_rsi_filter:
            self.rsi_series = ta.rsi(df["close"], self.rsi_length)

        self.vol_above_ma = pd.Series(True, index=df.index)
        if self.use_volume_filter:
            vol_ma = ta.sma(df["volume"], self.vol_ma_len)
            self.vol_above_ma = df["volume"] > vol_ma

        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._be_active = False
        self._last_dir = 0
        self._is_reversal = False
        self._reversal_pending = False
        self._prev_pos = 0.0

    def _in_session(self, ts):
        if not self.use_session:
            return True
        try:
            ts_et = ts.tz_convert("America/New_York") if hasattr(ts, "tz_convert") else ts
            hr, mn = ts_et.hour, ts_et.minute
        except Exception:
            hr, mn = getattr(ts, "hour", 0), getattr(ts, "minute", 0)
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

    def _build_legend(self, direction, trend_val, slope_val, rsi_val, macd_val):
        parts = [f"{'LONG' if direction == 1 else 'SHORT'} flip"]
        parts.append(f"trend={trend_val}")
        if self.use_slope_filter:
            parts.append(f"slope={slope_val:+.2f}")
        if self.use_rsi_filter:
            parts.append(f"RSI={rsi_val:.0f}")
        if self.use_macd_filter:
            parts.append(f"MACD={'bull' if macd_val > 0 else 'bear'}")
        if self._is_reversal:
            parts.insert(0, "REV")
        return " | ".join(parts)

    def next(self):
        i = self.bar_index
        df = self._data
        warmup = max(self.atr_length + 1, self.slope_ema_len + self.slope_lookback, 30)
        if i < warmup:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        trend_now = int(self.trend_line.iloc[i])
        trend_prev = int(self.trend_line.iloc[i - 1])

        buy_signal = trend_now == 1 and trend_prev == -1
        sell_signal = trend_now == -1 and trend_prev == 1

        # -- Filters --
        slope_val = self.ema_slope_series.iloc[i] if not pd.isna(self.ema_slope_series.iloc[i]) else 0.0
        rsi_val = self.rsi_series.iloc[i] if not pd.isna(self.rsi_series.iloc[i]) else 50.0
        macd_val = self.macd_hist.iloc[i] if not pd.isna(self.macd_hist.iloc[i]) else 0.0
        vol_ok = bool(self.vol_above_ma.iloc[i])

        if self.use_slope_filter:
            if buy_signal and slope_val < self.min_slope:
                buy_signal = False
            if sell_signal and slope_val > -self.min_slope:
                sell_signal = False

        if self.use_macd_filter:
            if buy_signal and macd_val <= 0:
                buy_signal = False
            if sell_signal and macd_val >= 0:
                sell_signal = False

        if self.use_rsi_filter:
            if buy_signal and rsi_val > self.rsi_ob:
                buy_signal = False
            if sell_signal and rsi_val < self.rsi_os:
                sell_signal = False

        if self.use_volume_filter and not vol_ok:
            buy_signal = False
            sell_signal = False

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars

        if not in_session:
            buy_signal = False
            sell_signal = False
        if not cooled:
            buy_signal = False
            sell_signal = False

        if buy_signal and self.position_size > 0:
            buy_signal = False
        if sell_signal and self.position_size < 0:
            sell_signal = False

        # -- Reversal detection --
        prev_pos = self._prev_pos
        if self.position_size == 0 and prev_pos != 0:
            if self.use_reversal and not self._is_reversal and self._last_dir != 0 and not self._be_active:
                if len(self._broker.closed_trades) > 0:
                    last_trade = self._broker.closed_trades[-1]
                    if last_trade.profit < 0:
                        self._reversal_pending = True
            self._be_active = False
            self._is_reversal = False

        close_now = df["close"].iloc[i]
        stop_pts = self.stop_pts
        tp_pts = self.tp_pts

        if buy_signal:
            legend = self._build_legend(1, trend_now, slope_val, rsi_val, macd_val)
            self._current_stop = close_now - stop_pts
            self._entry_price = close_now
            self._be_active = False
            self._is_reversal = False
            self._last_trade_bar = i
            self._last_dir = 1
            self.entry("Long", Direction.LONG, comment=legend)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close_now + tp_pts)
        elif sell_signal:
            legend = self._build_legend(-1, trend_now, slope_val, rsi_val, macd_val)
            self._current_stop = close_now + stop_pts
            self._entry_price = close_now
            self._be_active = False
            self._is_reversal = False
            self._last_trade_bar = i
            self._last_dir = -1
            self.entry("Short", Direction.SHORT, comment=legend)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close_now - tp_pts)
        elif self._reversal_pending and self.position_size == 0 and in_session:
            self._reversal_pending = False
            self._is_reversal = True
            self._be_active = False
            self._last_trade_bar = i
            rev_stop_pts = stop_pts / 2.0
            rev_tp_pts = stop_pts
            if self._last_dir == 1:
                legend = self._build_legend(-1, trend_now, slope_val, rsi_val, macd_val)
                self._current_stop = close_now + rev_stop_pts
                self._entry_price = close_now
                self._last_dir = -1
                self.entry("RevShort", Direction.SHORT, comment=legend)
                self.exit("XRS", from_entry="RevShort", stop=self._current_stop, limit=close_now - rev_tp_pts)
            elif self._last_dir == -1:
                legend = self._build_legend(1, trend_now, slope_val, rsi_val, macd_val)
                self._current_stop = close_now - rev_stop_pts
                self._entry_price = close_now
                self._last_dir = 1
                self.entry("RevLong", Direction.LONG, comment=legend)
                self.exit("XRL", from_entry="RevLong", stop=self._current_stop, limit=close_now + rev_tp_pts)

        # -- Breakeven --
        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0 and (close_now - self._entry_price) >= self.be_trigger_pts:
                self._be_active = True
                self._current_stop = self._entry_price + self.tick_size
                self.exit("XL", from_entry="Long", stop=self._current_stop, limit=self._entry_price + tp_pts)
            elif self.position_size < 0 and (self._entry_price - close_now) >= self.be_trigger_pts:
                self._be_active = True
                self._current_stop = self._entry_price - self.tick_size
                self.exit("XS", from_entry="Short", stop=self._current_stop, limit=self._entry_price - tp_pts)

        if self.position_size == 0:
            self._be_active = False

        self._prev_pos = self.position_size
