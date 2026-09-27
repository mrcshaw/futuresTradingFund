"""
ICT Concepts Intraday Strategies - for ES 2-5 minute charts.

Two modes built on the proven ORB framework with ICT overlays:
  ICTConceptsProfit     - More entries, wider fixed TP, trailing stops
  ICTConceptsConsistent - Strict filters: FVG + displacement + POC, daily limits

ICT concepts from the LuxAlgo indicator:
  - Opening Range Breakout (ORB) for structure
  - Fair Value Gaps (FVG) for entry confirmation
  - Displacement candles for momentum validation
  - Killzone awareness (NY session)
  - Delta Volume POC for directional bias
"""

import pandas as pd
import numpy as np
import datetime
from typing import List

from engine.strategy import Strategy
from engine.types import Direction, CommissionType, QtyType


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def _poc_from_bars(df, start_idx, end_idx, rows=24):
    """Compute POC price and delta % from volume profile."""
    if end_idx <= start_idx or end_idx >= len(df):
        return None, None
    prof_hi = df["high"].iloc[start_idx:end_idx + 1].max()
    prof_lo = df["low"].iloc[start_idx:end_idx + 1].min()
    span = prof_hi - prof_lo
    if span <= 0:
        return None, None
    step = span / rows
    vol_total = np.zeros(rows)
    vol_up = np.zeros(rows)
    vol_dn = np.zeros(rows)
    for j in range(start_idx, end_idx + 1):
        v = df["volume"].iloc[j]
        if v <= 0:
            continue
        lo_j, hi_j = df["low"].iloc[j], df["high"].iloc[j]
        is_up = df["close"].iloc[j] >= df["open"].iloc[j]
        s = max(int((lo_j - prof_lo) / step), 0)
        e = min(int((hi_j - prof_lo) / step), rows - 1)
        n = max(e - s + 1, 1)
        per = v / n
        for k in range(n):
            r = s + k
            if 0 <= r < rows:
                vol_total[r] += per
                if is_up:
                    vol_up[r] += per
                else:
                    vol_dn[r] += per
    poc_row = int(np.argmax(vol_total))
    poc_price = prof_lo + (poc_row + 0.5) * step
    poc_tot = vol_total[poc_row]
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot * 100.0) if poc_tot > 0 else 0.0
    return poc_price, delta_pct


class _ICTIntradayBase(Strategy):
    """Shared intraday ICT logic: ORB breakout + ICT confirmations."""

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

    # ORB
    gather_minutes = 15
    or_min_pts = 1.0
    or_max_pts = 30.0

    # Session
    rth_start_hr = 9
    rth_start_mn = 30
    entry_end_hr = 11
    entry_end_mn = 0
    close_eod_hr = 15
    close_eod_mn = 45

    # FVG
    require_fvg = True
    fvg_lookback = 20
    fvg_min_pts = 0.25

    # Displacement
    require_displacement = False
    displacement_lookback = 10
    displacement_body_mult = 1.5
    atr_len = 14

    # POC
    use_poc = False
    poc_rows = 24

    # Risk - tp_dollars > 0 = fixed dollar TP, tp_dollars = 0 = use tp_rr * risk
    tp_dollars = 500.0
    tp_rr = 1.5
    max_stop_dollars = 500.0
    breakout_margin_pts = 0.5
    confirm_bars = 1
    max_attempts = 2

    # Trailing
    use_trail = False
    trail_activate_dollars = 400.0
    trail_keep_pct = 40.0

    # Daily limits
    daily_loss_limit = 0.0
    consec_loss_max = 0
    consec_loss_pause = 0

    def configure(self):
        self.tp_pts = self.tp_dollars / self.point_value
        self.trail_activate_pts = self.trail_activate_dollars / self.point_value
        self.max_stop_pts = self.max_stop_dollars / self.point_value

    def init(self):
        df = self._data
        if len(df) < 50:
            self._ready = False
            return
        self._ready = True

        self._atr = _atr(df["high"], df["low"], df["close"], self.atr_len)
        body = (df["close"] - df["open"]).abs()
        self._avg_body = body.rolling(self.atr_len).mean()

        try:
            self._tz = "America/New_York" if hasattr(df.index[0], "tz_convert") else None
        except Exception:
            self._tz = None

        if self.use_poc:
            self._compute_prev_day_poc(df, len(df))
        else:
            self._prev_poc = pd.Series(np.nan, index=df.index)

        self._last_day = None
        self._day_attempts = 0
        self._or_high = 0.0
        self._or_low = 99999.0
        self._or_done = False
        self._or_valid = False
        self._peak_profit_pts = 0.0
        self._current_stop = 0.0
        self._current_tp = 0.0
        self._day_pnl = 0.0
        self._day_locked = False
        self._entry_equity = 0.0
        self._consec_losses = 0
        self._pause_until = None

    def _compute_prev_day_poc(self, df, n):
        rth_s = self.rth_start_hr * 100 + self.rth_start_mn
        rth_e = 16 * 100
        day_bars = {}
        for i in range(n):
            hr, mn, dt = self._parse_ts(df.index[i])
            t = hr * 100 + mn
            if rth_s <= t < rth_e and dt is not None:
                if dt not in day_bars:
                    day_bars[dt] = []
                day_bars[dt].append(i)

        day_poc = {}
        for dt, indices in day_bars.items():
            if len(indices) < 5:
                continue
            p, _ = _poc_from_bars(df, indices[0], indices[-1], self.poc_rows)
            if p is not None:
                day_poc[dt] = p

        dates_sorted = sorted(day_poc.keys())
        prev_pocs = [np.nan] * n
        for i in range(n):
            _, _, dt = self._parse_ts(df.index[i])
            if dt is None:
                continue
            prev_dt = None
            for d in dates_sorted:
                if d < dt:
                    prev_dt = d
                else:
                    break
            if prev_dt is not None:
                prev_pocs[i] = day_poc[prev_dt]
        self._prev_poc = pd.Series(prev_pocs, index=df.index)

    def _parse_ts(self, ts):
        try:
            if self._tz and hasattr(ts, "tz_convert"):
                et = ts.tz_convert("America/New_York")
            else:
                et = ts
            return et.hour, et.minute, et.date() if hasattr(et, "date") else None
        except Exception:
            return 0, 0, None

    def _has_bull_fvg(self, i):
        df = self._data
        for j in range(max(2, i - self.fvg_lookback), i + 1):
            lo_j = df["low"].iloc[j]
            hi_j2 = df["high"].iloc[j - 2]
            if lo_j > hi_j2 and (lo_j - hi_j2) >= self.fvg_min_pts:
                return True
        return False

    def _has_bear_fvg(self, i):
        df = self._data
        for j in range(max(2, i - self.fvg_lookback), i + 1):
            hi_j = df["high"].iloc[j]
            lo_j2 = df["low"].iloc[j - 2]
            if hi_j < lo_j2 and (lo_j2 - hi_j) >= self.fvg_min_pts:
                return True
        return False

    def _has_bull_displacement(self, i):
        df = self._data
        for j in range(max(0, i - self.displacement_lookback), i + 1):
            if pd.isna(self._avg_body.iloc[j]) or self._avg_body.iloc[j] <= 0:
                continue
            body = abs(df["close"].iloc[j] - df["open"].iloc[j])
            if df["close"].iloc[j] > df["open"].iloc[j] and body > self._avg_body.iloc[j] * self.displacement_body_mult:
                return True
        return False

    def _has_bear_displacement(self, i):
        df = self._data
        for j in range(max(0, i - self.displacement_lookback), i + 1):
            if pd.isna(self._avg_body.iloc[j]) or self._avg_body.iloc[j] <= 0:
                continue
            body = abs(df["close"].iloc[j] - df["open"].iloc[j])
            if df["close"].iloc[j] < df["open"].iloc[j] and body > self._avg_body.iloc[j] * self.displacement_body_mult:
                return True
        return False

    def next(self):
        if not self._ready:
            return
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

        # Day reset
        if today != self._last_day:
            if self._last_day is not None:
                if self._day_pnl < -1:
                    self._consec_losses += 1
                elif self._day_pnl > 1:
                    self._consec_losses = 0
            self._last_day = today
            self._day_attempts = 0
            self._or_high = 0.0
            self._or_low = 99999.0
            self._or_done = False
            self._or_valid = False
            self._day_pnl = 0.0
            self._day_locked = False
            self._entry_equity = self.equity

        # Consecutive loss pause
        if self._pause_until is not None and today <= self._pause_until:
            return
        if self.consec_loss_max > 0 and self._consec_losses >= self.consec_loss_max:
            self._pause_until = today + datetime.timedelta(days=self.consec_loss_pause)
            self._consec_losses = 0
            if self.position_size != 0:
                self.close_all(comment="PAUSE")
            return

        self._day_pnl = self.equity - self._entry_equity

        if self.daily_loss_limit > 0 and self._day_pnl <= -self.daily_loss_limit:
            if not self._day_locked:
                self._day_locked = True
                if self.position_size != 0:
                    self.close_all(comment="DayLimit")
            return

        # EOD flatten
        eod_t = self.close_eod_hr * 100 + self.close_eod_mn
        if bar_t >= eod_t and self.position_size != 0:
            self.close_all(comment="EOD")
            return

        # ORB gathering
        mins = (hr - self.rth_start_hr) * 60 + (mn - self.rth_start_mn)
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

        # Trailing stop
        if self.use_trail and self.position_size != 0:
            avg = self.position_avg_price
            if self.position_size > 0:
                profit_now = high - avg
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_activate_pts:
                    lock = self._peak_profit_pts * (self.trail_keep_pct / 100.0)
                    ns = avg + lock
                    if ns > self._current_stop:
                        self._current_stop = ns
                        self.exit("XL", from_entry="Long",
                                  stop=self._current_stop, limit=self._current_tp)
            else:
                profit_now = avg - low
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_activate_pts:
                    lock = self._peak_profit_pts * (self.trail_keep_pct / 100.0)
                    ns = avg - lock
                    if ns < self._current_stop:
                        self._current_stop = ns
                        self.exit("XS", from_entry="Short",
                                  stop=self._current_stop, limit=self._current_tp)

        # Entry window / limits
        end_t = self.entry_end_hr * 100 + self.entry_end_mn
        if bar_t >= end_t or self._day_attempts >= self.max_attempts:
            return
        if self._day_locked or self.position_size != 0:
            return

        # Breakout detection
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

        # ICT Filters
        if self.require_fvg:
            if breakout_long and confirmed_long and not self._has_bull_fvg(i):
                confirmed_long = False
            if breakout_short and confirmed_short and not self._has_bear_fvg(i):
                confirmed_short = False

        if self.require_displacement:
            if breakout_long and confirmed_long and not self._has_bull_displacement(i):
                confirmed_long = False
            if breakout_short and confirmed_short and not self._has_bear_displacement(i):
                confirmed_short = False

        if self.use_poc and not pd.isna(self._prev_poc.iloc[i]):
            prev_poc = self._prev_poc.iloc[i]
            if breakout_long and confirmed_long and close <= prev_poc:
                confirmed_long = False
            if breakout_short and confirmed_short and close >= prev_poc:
                confirmed_short = False

        long_sig = breakout_long and confirmed_long
        short_sig = breakout_short and confirmed_short

        if long_sig:
            self._day_attempts += 1
            self._peak_profit_pts = 0.0
            raw_stop = self._or_low
            risk_pts = close - raw_stop
            if risk_pts > self.max_stop_pts:
                raw_stop = close - self.max_stop_pts
                risk_pts = self.max_stop_pts
            self._current_stop = raw_stop
            if self.tp_dollars > 0:
                tp_price = close + self.tp_pts
            else:
                tp_price = close + risk_pts * self.tp_rr
            self._current_tp = tp_price
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=raw_stop, limit=tp_price)

        elif short_sig:
            self._day_attempts += 1
            self._peak_profit_pts = 0.0
            raw_stop = self._or_high
            risk_pts = raw_stop - close
            if risk_pts > self.max_stop_pts:
                raw_stop = close + self.max_stop_pts
                risk_pts = self.max_stop_pts
            self._current_stop = raw_stop
            if self.tp_dollars > 0:
                tp_price = close - self.tp_pts
            else:
                tp_price = close - risk_pts * self.tp_rr
            self._current_tp = tp_price
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=raw_stop, limit=tp_price)


class ICTConceptsProfit(_ICTIntradayBase):
    """
    Profit Mode: More trades, wider fixed TP, trailing to capture runners.
    - FVG optional (more entry opportunities)
    - Wider entry window (until 12:00)
    - Fixed TP: $600 (12 points)
    - Trailing stop: activate at $500, keep 50%
    - Max 3 entries/day
    """
    title = "ICT Concepts - Profit"

    gather_minutes = 15
    or_min_pts = 1.0
    or_max_pts = 30.0
    entry_end_hr = 12
    entry_end_mn = 0

    require_fvg = False
    require_displacement = False
    use_poc = False

    breakout_margin_pts = 0.5
    confirm_bars = 1
    max_attempts = 3
    tp_dollars = 500.0
    max_stop_dollars = 500.0

    use_trail = True
    trail_activate_dollars = 400.0
    trail_keep_pct = 40.0

    daily_loss_limit = 0.0
    consec_loss_max = 0


class ICTConceptsConsistent(_ICTIntradayBase):
    """
    Consistent Mode: High win rate, strict ICT filters, tight risk.
    - FVG required in breakout direction
    - Displacement candle required
    - Previous-day POC directional filter
    - NY killzone only (9:45-11:00)
    - Fixed TP: $400 (8 points)
    - Max 1 entry/day
    - Daily loss limit $400
    - Consecutive loss pause (3 losses -> 1 day off)
    """
    title = "ICT Concepts - Consistent"

    gather_minutes = 15
    or_min_pts = 2.0
    or_max_pts = 20.0
    entry_end_hr = 11
    entry_end_mn = 0

    require_fvg = True
    fvg_lookback = 25
    fvg_min_pts = 0.5
    require_displacement = True
    displacement_lookback = 15
    displacement_body_mult = 1.3

    use_poc = True
    poc_rows = 24

    breakout_margin_pts = 0.75
    confirm_bars = 1
    max_attempts = 1
    tp_dollars = 0.0
    tp_rr = 1.5
    max_stop_dollars = 400.0

    use_trail = False

    daily_loss_limit = 400.0
    consec_loss_max = 3
    consec_loss_pause = 1
