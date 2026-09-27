"""
Day Shift Delta Volume Breakout + VAH/VAL Zones
Python port of TradingView Pine Script strategy.

v3: Multi-mode entry with proper stop sizing for 2m ES.
  Entry modes:
    - zone_bounce: price wick breaks zone, closes back inside (original concept)
    - mean_revert: RSI oversold/overbought at VAH/VAL zones
    - ema_cross: EMA crossover with trend + ADX filter
  Key fix: stop distances sized for ES 2m volatility (6-10+ pts).
"""

import pandas as pd
import numpy as np
from typing import Optional, Tuple

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_volume_profile(
    df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    rows: int,
    va_pct: float,
) -> Tuple[Optional[float], Optional[float], Optional[int], Optional[float]]:
    if end_idx < start_idx or end_idx >= len(df):
        return None, None, None, None

    prof_hi = df["high"].iloc[start_idx : end_idx + 1].max()
    prof_lo = df["low"].iloc[start_idx : end_idx + 1].min()
    prof_span = prof_hi - prof_lo
    if prof_span <= 0:
        return None, None, None, None

    step_size = prof_span / rows
    vol_total = np.zeros(rows)
    vol_up = np.zeros(rows)
    vol_dn = np.zeros(rows)

    for j in range(start_idx, end_idx + 1):
        bar_vol = df["volume"].iloc[j]
        if bar_vol <= 0:
            continue
        bar_lo = df["low"].iloc[j]
        bar_hi = df["high"].iloc[j]
        bar_is_up = df["close"].iloc[j] >= df["open"].iloc[j]
        s_row = max(int((bar_lo - prof_lo) / step_size), 0)
        e_row = min(int((bar_hi - prof_lo) / step_size), rows - 1)
        span = max(e_row - s_row + 1, 1)
        per_row = bar_vol / span
        for k in range(span):
            r = s_row + k
            if 0 <= r < rows:
                vol_total[r] += per_row
                if bar_is_up:
                    vol_up[r] += per_row
                else:
                    vol_dn[r] += per_row

    poc_row = int(np.argmax(vol_total))
    total_vol = vol_total.sum()
    if total_vol <= 0:
        return None, None, None, None

    poc_tot = vol_total[poc_row]
    poc_delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot * 100.0) if poc_tot > 0 else 0.0

    va_target = total_vol * (va_pct / 100.0)
    va_lo_row = poc_row
    va_hi_row = poc_row
    va_vol = vol_total[poc_row]
    expand_count = 0

    while va_vol < va_target and expand_count < rows:
        expand_count += 1
        vol_above = vol_total[va_hi_row + 1] if va_hi_row < rows - 1 else -1.0
        vol_below = vol_total[va_lo_row - 1] if va_lo_row > 0 else -1.0
        if vol_above >= vol_below and va_hi_row < rows - 1:
            va_hi_row += 1
            va_vol += vol_total[va_hi_row]
        elif va_lo_row > 0:
            va_lo_row -= 1
            va_vol += vol_total[va_lo_row]
        elif va_hi_row < rows - 1:
            va_hi_row += 1
            va_vol += vol_total[va_hi_row]
        else:
            break

    val_price = prof_lo + va_lo_row * step_size
    vah_price = prof_lo + (va_hi_row + 1) * step_size
    return val_price, vah_price, poc_row, poc_delta_pct


class VAHVALStrategy(Strategy):
    title = "Day Shift Delta Volume Breakout + VAH/VAL Zones"
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

    # Volume profile
    profile_lookback = 120
    profile_rows = 24
    va_pct = 90.0  # 90% = accurate to price range
    zone_width_pts = 2.0
    touch_tol_pts = 1.0
    min_bounces_to_enter = 1  # Require zone tested before trade (high-volume bounce)
    min_delta_pct = 10.0  # POC delta filter: long when delta > -X, short when delta < +X. 0=disabled. 10 = improved in backtest.

    # Entry mode: "zone_bounce", "mean_revert", "ema_cross"
    entry_mode = "zone_bounce"

    # Max slope filter: avoid trading in strong trends (mean reversion loses)
    ema_trend_len = 50
    ema_slope_len = 5
    max_slope_pts = 5.0  # If |ema_slope| > this, skip (trending). 5 = profitable in backtest.

    # EMA cross params (when entry_mode="ema_cross")
    ema_fast = 21
    ema_slow = 55
    adx_len = 14
    adx_thresh = 20

    # RSI params (when entry_mode="mean_revert")
    rsi_len = 14
    rsi_ob = 70
    rsi_os = 30

    # Entry
    cooldown_bars = 10
    max_daily_trades = 4

    # Session (ET)
    use_session = True
    sess_start_hr = 8
    sess_start_mn = 30
    sess_end_hr = 15
    sess_end_mn = 0

    # Risk
    stop_dollars = 300.0
    tp_dollars = 300.0

    # Breakeven
    use_breakeven = True
    be_trigger_pct = 60.0

    # Daily limits
    use_daily_limits = True
    daily_loss_limit = 600.0
    daily_profit_cap = 2500.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.tp_pts * (self.be_trigger_pct / 100.0)

    def init(self):
        df = self._data
        n = len(df)

        self.rsi = ta.rsi(df["close"], self.rsi_len)
        self.ema_trend = ta.ema(df["close"], self.ema_trend_len)
        self.ema_slope = self.ema_trend - self.ema_trend.shift(self.ema_slope_len)
        self.ema_f = ta.ema(df["close"], self.ema_fast)
        self.ema_s = ta.ema(df["close"], self.ema_slow)
        self.adx_val, _, _ = ta.adx(df["high"], df["low"], df["close"], self.adx_len)

        val_prices = []
        vah_prices = []
        poc_deltas = []
        val_bounces = []
        vah_bounces = []
        last_poc_row = -999
        vbc, vhbc = 0, 0

        for i in range(n):
            lb = min(self.profile_lookback, i)
            if lb < 1:
                val_prices.append(np.nan)
                vah_prices.append(np.nan)
                poc_deltas.append(np.nan)
                val_bounces.append(0)
                vah_bounces.append(0)
                continue

            start_idx = max(0, i - lb)
            val_price, vah_price, poc_row, poc_delta = _compute_volume_profile(
                df, start_idx, i, self.profile_rows, self.va_pct
            )
            val_prices.append(val_price if val_price is not None else np.nan)
            vah_prices.append(vah_price if vah_price is not None else np.nan)
            poc_deltas.append(poc_delta if poc_delta is not None else np.nan)

            if poc_row != last_poc_row:
                vbc, vhbc = 0, 0
                last_poc_row = poc_row

            if val_price is not None and vah_price is not None and i >= 1:
                low_i = df["low"].iloc[i]
                high_i = df["high"].iloc[i]
                close_i = df["close"].iloc[i]
                close_1 = df["close"].iloc[i - 1]
                val_touch = low_i <= val_price + self.touch_tol_pts and close_i > val_price and close_1 > val_price
                vah_touch = high_i >= vah_price - self.touch_tol_pts and close_i < vah_price and close_1 < vah_price
                if val_touch:
                    vbc = min(vbc + 1, 20)
                if vah_touch:
                    vhbc = min(vhbc + 1, 20)
            val_bounces.append(vbc)
            vah_bounces.append(vhbc)

        self.val_price = pd.Series(val_prices, index=df.index)
        self.vah_price = pd.Series(vah_prices, index=df.index)
        self.poc_delta_pct = pd.Series(poc_deltas, index=df.index)
        self.val_bounce_count = pd.Series(val_bounces, index=df.index)
        self.vah_bounce_count = pd.Series(vah_bounces, index=df.index)

        self._entry_price_track = np.nan
        self._be_active = False
        self._last_trade_bar = -9999

    def _in_session(self, ts) -> bool:
        if not self.use_session:
            return True
        try:
            if hasattr(ts, "tz_convert"):
                ts_et = ts.tz_convert("America/New_York")
            else:
                ts_et = ts
            hr = ts_et.hour
            mn = ts_et.minute
        except Exception:
            hr = getattr(ts, "hour", 0)
            mn = getattr(ts, "minute", 0)
        bar_t = hr * 100 + mn
        s_start = self.sess_start_hr * 100 + self.sess_start_mn
        s_end = self.sess_end_hr * 100 + self.sess_end_mn
        if s_start > s_end:
            return bar_t >= s_start or bar_t < s_end
        return s_start <= bar_t < s_end

    def _session_ended(self, prev_ts, ts) -> bool:
        if not self.use_session:
            return False
        return self._in_session(prev_ts) and not self._in_session(ts)

    def _get_daily_pnl(self) -> float:
        try:
            ts = self._data.index[self.bar_index]
            if hasattr(ts, "date"):
                today_date = ts.date()
            elif hasattr(ts, "tz_convert"):
                today_date = ts.tz_convert("America/New_York").date()
            else:
                return 0.0
        except Exception:
            return 0.0
        total = 0.0
        for t in self._broker.closed_trades:
            if t.exit_time and hasattr(t.exit_time, "date"):
                if t.exit_time.date() == today_date:
                    total += t.profit
        return total

    def _get_daily_trade_count(self) -> int:
        try:
            ts = self._data.index[self.bar_index]
            if hasattr(ts, "date"):
                today_date = ts.date()
            elif hasattr(ts, "tz_convert"):
                today_date = ts.tz_convert("America/New_York").date()
            else:
                return 0
        except Exception:
            return 0
        count = 0
        for t in self._broker.closed_trades:
            if t.exit_time and hasattr(t.exit_time, "date"):
                if t.exit_time.date() == today_date:
                    count += 1
        return count

    def _delta_ok(self, i, for_long: bool) -> bool:
        """POC delta filter: long when not heavy selling, short when not heavy buying."""
        if self.min_delta_pct <= 0:
            return True
        delta = self.poc_delta_pct.iloc[i] if i < len(self.poc_delta_pct) else np.nan
        if pd.isna(delta):
            return True
        if for_long:
            return delta > -self.min_delta_pct  # avoid buying into heavy selling
        return delta < self.min_delta_pct  # avoid shorting into heavy buying

    def _zone_bounce_signals(self, i, df, val_p, vah_p):
        """Price wick breaks zone, closes back inside = reversal signal."""
        close = df["close"].iloc[i]
        low = df["low"].iloc[i]
        high = df["high"].iloc[i]
        open_ = df["open"].iloc[i]
        vbc = self.val_bounce_count.iloc[i] if i < len(self.val_bounce_count) else 0
        vhbc = self.vah_bounce_count.iloc[i] if i < len(self.vah_bounce_count) else 0

        val_lo = val_p
        val_hi = val_p + self.zone_width_pts
        vah_lo = vah_p - self.zone_width_pts
        vah_hi = vah_p

        bullish_close = close > open_
        val_tested = vbc >= self.min_bounces_to_enter
        vah_tested = vhbc >= self.min_bounces_to_enter

        long_sig = (
            low <= val_lo + self.touch_tol_pts
            and close > val_lo
            and close <= val_hi + 2.0
            and bullish_close
            and val_tested
            and self._delta_ok(i, True)
        )

        bearish_close = close < open_
        short_sig = (
            high >= vah_hi - self.touch_tol_pts
            and close < vah_hi
            and close >= vah_lo - 2.0
            and bearish_close
            and vah_tested
            and self._delta_ok(i, False)
        )
        return long_sig, short_sig

    def _mean_revert_signals(self, i, df, val_p, vah_p):
        """RSI oversold at VAL = long, RSI overbought at VAH = short."""
        close = df["close"].iloc[i]
        rsi_val = self.rsi.iloc[i]
        if pd.isna(rsi_val):
            return False, False

        vbc = self.val_bounce_count.iloc[i] if i < len(self.val_bounce_count) else 0
        vhbc = self.vah_bounce_count.iloc[i] if i < len(self.vah_bounce_count) else 0
        val_tested = vbc >= self.min_bounces_to_enter
        vah_tested = vhbc >= self.min_bounces_to_enter

        near_val = close <= val_p + self.zone_width_pts + 2.0
        near_vah = close >= vah_p - self.zone_width_pts - 2.0

        long_sig = rsi_val <= self.rsi_os and near_val and val_tested and self._delta_ok(i, True)
        short_sig = rsi_val >= self.rsi_ob and near_vah and vah_tested and self._delta_ok(i, False)
        return long_sig, short_sig

    def _ema_cross_signals(self, i, df, val_p, vah_p):
        """EMA crossover with trend + ADX."""
        if i < 2:
            return False, False
        close = df["close"].iloc[i]

        ef = self.ema_f.iloc[i]
        es = self.ema_s.iloc[i]
        ef_1 = self.ema_f.iloc[i - 1]
        es_1 = self.ema_s.iloc[i - 1]
        if pd.isna(ef) or pd.isna(es) or pd.isna(ef_1) or pd.isna(es_1):
            return False, False

        cross_up = ef > es and ef_1 <= es_1
        cross_dn = ef < es and ef_1 >= es_1

        ema_t = self.ema_trend.iloc[i]
        ema_t5 = self.ema_trend.iloc[i - 5] if i >= 5 else np.nan
        if pd.isna(ema_t) or pd.isna(ema_t5):
            return False, False
        trend_up = close > ema_t and ema_t > ema_t5
        trend_dn = close < ema_t and ema_t < ema_t5

        adx_now = self.adx_val.iloc[i]
        adx_ok = not pd.isna(adx_now) and adx_now >= self.adx_thresh

        long_sig = cross_up and trend_up and adx_ok and close > val_p
        short_sig = cross_dn and trend_dn and adx_ok and close < vah_p
        return long_sig, short_sig

    def next(self):
        i = self.bar_index
        df = self._data

        if i < max(self.profile_lookback, self.ema_slow, self.adx_len * 2):
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        val_p = self.val_price.iloc[i]
        vah_p = self.vah_price.iloc[i]
        if pd.isna(val_p) or pd.isna(vah_p):
            return

        close = df["close"].iloc[i]

        # Daily limits
        daily_pnl = self._get_daily_pnl()
        daily_locked = False
        if self.use_daily_limits:
            if self.daily_loss_limit > 0 and daily_pnl <= -self.daily_loss_limit:
                daily_locked = True
            if self.daily_profit_cap > 0 and daily_pnl >= self.daily_profit_cap:
                daily_locked = True
            if daily_locked and self.position_size != 0:
                self.close_all(comment="Daily Limit")

        at_max_trades = self._get_daily_trade_count() >= self.max_daily_trades
        in_session = self._in_session(ts)

        # Max slope filter: skip when trending (|ema_slope| > max_slope_pts)
        slope_ok = True
        if self.max_slope_pts > 0 and i >= self.ema_slope_len:
            slope = self.ema_slope.iloc[i]
            if not pd.isna(slope) and abs(slope) > self.max_slope_pts:
                slope_ok = False

        # Breakeven management
        if self.position_size != 0 and not np.isnan(self._entry_price_track) and self.use_breakeven and not self._be_active:
            if self.position_size > 0:
                unrealized_pts = close - self._entry_price_track
            else:
                unrealized_pts = self._entry_price_track - close
            if unrealized_pts >= self.be_trigger_pts:
                self._be_active = True
                if self.position_size > 0:
                    be_price = self._entry_price_track + 0.25
                    self.exit("XL", from_entry="Long", stop=be_price, limit=self._entry_price_track + self.tp_pts)
                else:
                    be_price = self._entry_price_track - 0.25
                    self.exit("XS", from_entry="Short", stop=be_price, limit=self._entry_price_track - self.tp_pts)

        if self.position_size == 0:
            self._entry_price_track = np.nan
            self._be_active = False

        # Generate entry signals based on mode
        if self.entry_mode == "zone_bounce":
            long_sig, short_sig = self._zone_bounce_signals(i, df, val_p, vah_p)
        elif self.entry_mode == "mean_revert":
            long_sig, short_sig = self._mean_revert_signals(i, df, val_p, vah_p)
        elif self.entry_mode == "ema_cross":
            long_sig, short_sig = self._ema_cross_signals(i, df, val_p, vah_p)
        else:
            return

        # Apply common filters (incl. max slope = avoid trending markets)
        long_sig = long_sig and slope_ok and in_session and not daily_locked and not at_max_trades and self.position_size <= 0
        short_sig = short_sig and slope_ok and in_session and not daily_locked and not at_max_trades and self.position_size >= 0

        cooled_down = i - self._last_trade_bar >= self.cooldown_bars

        if long_sig and cooled_down:
            self._last_trade_bar = i
            self._entry_price_track = close
            self._be_active = False
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=close - self.stop_pts, limit=close + self.tp_pts)
        elif short_sig and cooled_down:
            self._last_trade_bar = i
            self._entry_price_track = close
            self._be_active = False
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=close + self.stop_pts, limit=close - self.tp_pts)
