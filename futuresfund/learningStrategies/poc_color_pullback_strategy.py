"""
POC Color + Pullback — Python port matching smart_trail_poc_combined.pine (logic).

- POC delta: green (>0) long bias, red (<0) short bias
- VWMA slope in degrees: atan(rise / ATR_slope)
- Entry: POC depth + POC color + slope + candle + session + cooldown + flat
- Risk: ATR initial SL, optional ATR limit TP, chandelier runner trail
"""

import numpy as np
import pandas as pd

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _compute_poc_row(df, start_idx, end_idx, rows):
    """Returns poc_lo, poc_hi, poc_mid, delta_pct or Nones."""
    if end_idx < start_idx or end_idx >= len(df):
        return None, None, None, None
    
    # Use numpy arrays for speed
    if not hasattr(df, '_cached_arrays'):
        df._cached_arrays = {
            'close': df["close"].values,
            'open': df["open"].values,
            'high': df["high"].values,
            'low': df["low"].values,
            'volume': df["volume"].values
        }
    
    c_a = df._cached_arrays['close']
    o_a = df._cached_arrays['open']
    h_a = df._cached_arrays['high']
    l_a = df._cached_arrays['low']
    v_a = df._cached_arrays['volume']

    prof_hi = np.max(h_a[start_idx:end_idx+1])
    prof_lo = np.min(l_a[start_idx:end_idx+1])
    prof_span = prof_hi - prof_lo
    if prof_span <= 0:
        return None, None, None, None
    step_size = prof_span / rows
    vol_total = np.zeros(rows)
    vol_up = np.zeros(rows)
    vol_dn = np.zeros(rows)
    for j in range(start_idx, end_idx + 1):
        bar_vol = v_a[j]
        if bar_vol <= 0:
            continue
        bar_lo = l_a[j]
        bar_hi = h_a[j]
        bar_is_up = c_a[j] >= o_a[j]
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
    poc_tot = vol_total[poc_row]
    if poc_tot <= 0:
        return None, None, None, None
    poc_lo_price = prof_lo + poc_row * step_size
    poc_hi_price = prof_lo + (poc_row + 1) * step_size
    poc_mid = prof_lo + (poc_row + 0.5) * step_size
    delta_pct = ((vol_up[poc_row] - vol_dn[poc_row]) / poc_tot) * 100.0
    return poc_lo_price, poc_hi_price, poc_mid, delta_pct


_POC_CACHE = {}
_PIVOT_CACHE = {}

class PocColorPullbackStrategy(Strategy):
    title = "POC Color + Pullback"
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

    # VWMA slope
    vwma_len = 50
    vwma_slope_len = 5
    min_slope_deg = 3.0
    atr_len_slope = 14

    # EMA filter
    use_ema_filter = True
    ema_len = 500

    # Volume profile
    profile_lookback = 120
    profile_rows = 10

    # Pivot progression
    use_pivot_filter = True
    pivot_left = 15
    pivot_right = 7

    # POC Trend
    use_poc_trend = True
    poc_trend_bars = 60

    # Opposing POC S/R
    use_opposing_poc = True
    prior_poc_bars = 60
    opposing_atr_mult = 2.0

    # Entry
    cooldown_bars = 10
    use_time_cooldown = True
    poc_entry_pct = 10.0

    use_session = True
    sess_start_hr = 8
    sess_start_mn = 0
    sess_end_hr = 16
    sess_end_mn = 0

    # Daily Limits
    use_daily_cap = False
    daily_profit_cap = 1000.0
    close_on_cap = False

    use_poc_stop = False
    poc_stop_buffer = 5.0
    use_atr_risk = True
    atr_risk_len = 14
    sl_atr_mult = 1.5
    sl_atr_mult_reduced = 1.0
    tp_atr_mult = 0.0
    tp_atr_mult_reduced = 2.0
    runner_activate_atr_mult = 0.25
    runner_trail_atr_mult = 1.0

    use_reduced_risk = False
    reduced_start_hr = 18
    reduced_start_mn = 0
    reduced_end_hr = 8
    reduced_end_mn = 0

    stop_dollars = 500.0
    tp_dollars = 2500.0
    reduced_stop_dollars = 300.0
    reduced_tp_dollars = 500.0

    use_breakeven = False
    be_atr_mult = 0.5

    use_trail_stop = False
    trail_pct = 80.0
    trail_step_amt = 500.0
    trail_step_pct = 10.0
    trail_min_dollars = 500.0
    be_trigger_dollars = 150.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)
        high, low, close, vol = df["high"], df["low"], df["close"], df["volume"]

        self.vwma = ta.ema(close, self.vwma_len)
        vwma_rise = self.vwma - self.vwma.shift(self.vwma_slope_len)
        self.atr_slope = ta.atr(high, low, close, self.atr_len_slope)
        self.atr_risk = ta.atr(high, low, close, self.atr_risk_len)
        self.vwma_slope_deg = pd.Series(np.nan, index=df.index)
        mask = self.atr_slope > 0
        self.vwma_slope_deg.loc[mask] = np.degrees(np.arctan((vwma_rise / self.atr_slope).loc[mask]))

        self.ema_val = ta.ema(close, self.ema_len) if self.ema_len else pd.Series(np.nan, index=df.index)

        cache_key = (n, df.index[-1], self.profile_lookback, self.profile_rows)
        if cache_key in _POC_CACHE:
            poc_lo_a, poc_hi_a, delta_a = _POC_CACHE[cache_key]
        else:
            poc_lo_a = np.full(n, np.nan)
            poc_hi_a = np.full(n, np.nan)
            delta_a = np.full(n, np.nan)
            for i in range(n):
                lb = min(self.profile_lookback, i)
                if lb < 2:
                    continue
                start_idx = max(0, i - lb)
                plo, phi, _, dpct = _compute_poc_row(df, start_idx, i, self.profile_rows)
                if plo is not None:
                    poc_lo_a[i] = plo
                    poc_hi_a[i] = phi
                    delta_a[i] = dpct
            _POC_CACHE[cache_key] = (poc_lo_a, poc_hi_a, delta_a)

        self.poc_lo = pd.Series(poc_lo_a, index=df.index)
        self.poc_hi = pd.Series(poc_hi_a, index=df.index)
        self.poc_delta = pd.Series(delta_a, index=df.index)
        self.poc_mid = (self.poc_lo + self.poc_hi) / 2.0

        self._compute_pivot_progression(df, n)

        self._last_trade_bar = -9999
        self._last_trade_time = pd.NaT
        self._start_of_day_equity = np.nan
        self._last_day = None
        self._prev_pos = 0.0  # position at end of previous bar (for next() start)
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._be_active = False
        self._peak_px = np.nan
        self._trough_px = np.nan
        self._atr_at_entry = np.nan
        self._peak_profit_pts = 0.0

    def _compute_pivot_progression(self, df, n):
        """Pre-compute pivot highs/lows and lower-highs / higher-lows flags per bar."""
        cache_key = (n, df.index[-1], self.pivot_left, self.pivot_right)
        if cache_key in _PIVOT_CACHE:
            self.has_higher_lows, self.has_lower_highs = _PIVOT_CACHE[cache_key]
            return

        hi = df["high"].values
        lo = df["low"].values
        close_a = df["close"].values
        left = self.pivot_left
        right = self.pivot_right

        pivot_hi = np.full(n, np.nan)
        pivot_lo = np.full(n, np.nan)

        for i in range(left, n - right):
            is_ph = True
            for j in range(1, left + 1):
                if hi[i - j] >= hi[i]:
                    is_ph = False
                    break
            if is_ph:
                for j in range(1, right + 1):
                    if hi[i + j] >= hi[i]:
                        is_ph = False
                        break
            if is_ph:
                pivot_hi[i + right] = hi[i]

            is_pl = True
            for j in range(1, left + 1):
                if lo[i - j] <= lo[i]:
                    is_pl = False
                    break
            if is_pl:
                for j in range(1, right + 1):
                    if lo[i + j] <= lo[i]:
                        is_pl = False
                        break
            if is_pl:
                pivot_lo[i + right] = lo[i]

        self.has_lower_highs = np.zeros(n, dtype=bool)
        self.has_higher_lows = np.zeros(n, dtype=bool)

        last_ph = np.nan
        last_pl = np.nan

        for i in range(n):
            if not np.isnan(pivot_hi[i]):
                last_ph = pivot_hi[i]
            if not np.isnan(pivot_lo[i]):
                last_pl = pivot_lo[i]

            if not np.isnan(last_ph):
                self.has_lower_highs[i] = close_a[i] < last_ph
            if not np.isnan(last_pl):
                self.has_higher_lows[i] = close_a[i] > last_pl

        _PIVOT_CACHE[cache_key] = (self.has_higher_lows, self.has_lower_highs)

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

    def _in_reduced(self, ts):
        if not self.use_reduced_risk:
            return False
        try:
            ts_et = ts.tz_convert("America/New_York") if hasattr(ts, "tz_convert") else ts
            hr, mn = ts_et.hour, ts_et.minute
        except Exception:
            hr, mn = getattr(ts, "hour", 0), getattr(ts, "minute", 0)
        bar_t = hr * 100 + mn
        r_start = self.reduced_start_hr * 100 + self.reduced_start_mn
        r_end = self.reduced_end_hr * 100 + self.reduced_end_mn
        if r_start > r_end:
            return bar_t >= r_start or bar_t < r_end
        return r_start <= bar_t < r_end

    def next(self):
        i = self.bar_index
        df = self._data
        # Position at end of previous bar (Pine: position_size[1] at bar open)
        prev_pos = self._prev_pos
        warmup = max(self.profile_lookback + 2, self.vwma_len + 5, self.atr_risk_len + 2)
        if i < warmup:
            self._prev_pos = self.position_size
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        current_day = ts.date()
        if self._last_day != current_day:
            self._start_of_day_equity = self.equity
            self._last_day = current_day
        if pd.isna(self._start_of_day_equity):
            self._start_of_day_equity = self.equity
            self._last_day = current_day

        daily_profit = self.equity - self._start_of_day_equity
        daily_cap_hit = self.use_daily_cap and (daily_profit >= self.daily_profit_cap)

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        if daily_cap_hit and self.close_on_cap and self.position_size != 0:
            self.close_all(comment="Daily Cap Hit")

        c = df["close"].iloc[i]
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        lo = df["low"].iloc[i]

        poc_lo = self.poc_lo.iloc[i]
        poc_hi = self.poc_hi.iloc[i]
        poc_delta = self.poc_delta.iloc[i]
        atr_r = self.atr_risk.iloc[i]
        atr_ok = not pd.isna(atr_r) and atr_r > 0

        poc_valid = not pd.isna(poc_hi) and not pd.isna(poc_lo)
        poc_green = not pd.isna(poc_delta) and poc_delta > 0
        poc_red = not pd.isna(poc_delta) and poc_delta < 0

        poc_width = poc_hi - poc_lo if poc_valid else 0.0
        long_depth = poc_hi - poc_width * self.poc_entry_pct / 100.0
        short_depth = poc_lo + poc_width * self.poc_entry_pct / 100.0

        price_in_poc = poc_valid and h >= poc_lo and lo <= poc_hi
        depth_ok_long = price_in_poc and lo <= long_depth
        depth_ok_short = price_in_poc and h >= short_depth

        bearish = c < o
        bullish = c > o

        vdeg = self.vwma_slope_deg.iloc[i]
        slope_ok_long = self.min_slope_deg <= 0 or (
            not pd.isna(vdeg) and vdeg > self.min_slope_deg
        )
        slope_ok_short = self.min_slope_deg <= 0 or (
            not pd.isna(vdeg) and vdeg < -self.min_slope_deg
        )

        ema = self.ema_val.iloc[i]
        ema_ok_long = not self.use_ema_filter or pd.isna(ema) or (c > ema)
        ema_ok_short = not self.use_ema_filter or pd.isna(ema) or (c < ema)

        pivot_ok_long = not self.use_pivot_filter or self.has_higher_lows[i]
        pivot_ok_short = not self.use_pivot_filter or self.has_lower_highs[i]

        past_poc_mid = self.poc_mid.iloc[i - self.poc_trend_bars] if i >= self.poc_trend_bars else np.nan
        poc_rising = not self.use_poc_trend or pd.isna(past_poc_mid) or (self.poc_mid.iloc[i] >= past_poc_mid)
        poc_falling = not self.use_poc_trend or pd.isna(past_poc_mid) or (self.poc_mid.iloc[i] <= past_poc_mid)

        # Opposing POC S/R: prior POC zone acts as support (blocks shorts) or resistance (blocks longs)
        opposing_ok_long = True
        opposing_ok_short = True
        if self.use_opposing_poc and atr_ok:
            prior_i = i - self.prior_poc_bars
            if prior_i >= 0:
                pr_hi = self.poc_hi.iloc[prior_i]
                pr_lo = self.poc_lo.iloc[prior_i]
                if not pd.isna(pr_hi) and not pd.isna(pr_lo) and poc_valid:
                    poc_mid_now = (poc_hi + poc_lo) / 2.0
                    prior_mid = (pr_hi + pr_lo) / 2.0
                    distinct = abs(poc_mid_now - prior_mid) > atr_r * 0.5
                    if distinct:
                        if pr_hi < c and (c - pr_hi) < self.opposing_atr_mult * atr_r:
                            opposing_ok_short = False
                        if pr_lo > c and (pr_lo - c) < self.opposing_atr_mult * atr_r:
                            opposing_ok_long = False

        in_sess = self._in_session(ts)
        cooled_time = True
        if self.use_time_cooldown and not pd.isna(self._last_trade_time):
            cooled_time = (ts - self._last_trade_time).total_seconds() >= 3600
        cooled = (i - self._last_trade_bar >= self.cooldown_bars) and cooled_time
        is_flat = self.position_size == 0 and prev_pos == 0

        long_sig = (
            depth_ok_long
            and poc_green
            and poc_rising
            and pivot_ok_long
            and opposing_ok_long
            and slope_ok_long
            and ema_ok_long
            and bullish
            and in_sess
            and cooled
            and is_flat
            and not daily_cap_hit
        )
        short_sig = (
            depth_ok_short
            and poc_red
            and poc_falling
            and pivot_ok_short
            and opposing_ok_short
            and slope_ok_short
            and ema_ok_short
            and bearish
            and in_sess
            and cooled
            and is_flat
            and not daily_cap_hit
        )

        in_red = self._in_reduced(ts)
        sl_atr_m = self.sl_atr_mult_reduced if in_red else self.sl_atr_mult
        tp_atr_m = self.tp_atr_mult_reduced if in_red else self.tp_atr_mult
        stop_pts = (
            (self.reduced_stop_dollars if in_red else self.stop_dollars) / self.point_value
        )
        tp_pts = (self.reduced_tp_dollars if in_red else self.tp_dollars) / self.point_value

        if long_sig:
            if self.use_atr_risk and atr_ok:
                sl_atr = c - sl_atr_m * atr_r
                if self.use_poc_stop and poc_valid:
                    sl0 = max(poc_lo - self.poc_stop_buffer, sl_atr)
                else:
                    sl0 = sl_atr
                lim = c + tp_atr_m * atr_r if tp_atr_m > 0 else None
            else:
                sl0 = poc_lo - self.poc_stop_buffer if self.use_poc_stop and poc_valid else c - stop_pts
                lim = c + tp_pts if tp_pts > 0 else None
            self._current_stop = sl0
            self._entry_price = c
            self._be_active = False
            self._last_trade_bar = i
            self._peak_px = h
            self._atr_at_entry = atr_r
            self._peak_profit_pts = 0.0
            self.entry("Long", Direction.LONG, comment="POC green | VWMA+")
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=lim)
        elif short_sig:
            if self.use_atr_risk and atr_ok:
                sl_atr = c + sl_atr_m * atr_r
                if self.use_poc_stop and poc_valid:
                    sl0 = min(poc_hi + self.poc_stop_buffer, sl_atr)
                else:
                    sl0 = sl_atr
                lim = c - tp_atr_m * atr_r if tp_atr_m > 0 else None
            else:
                sl0 = poc_hi + self.poc_stop_buffer if self.use_poc_stop and poc_valid else c + stop_pts
                lim = c - tp_pts if tp_pts > 0 else None
            self._current_stop = sl0
            self._entry_price = c
            self._be_active = False
            self._last_trade_bar = i
            self._trough_px = lo
            self._atr_at_entry = atr_r
            self._peak_profit_pts = 0.0
            self.entry("Short", Direction.SHORT, comment="POC red | VWMA-")
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=lim)

        if self.position_size == 0 and prev_pos != 0:
            self._last_trade_bar = i
            self._last_trade_time = ts
            self._be_active = False
            self._peak_px = np.nan
            self._trough_px = np.nan
            self._atr_at_entry = np.nan

        if self.use_atr_risk and atr_ok and self.position_size != 0:
            avg = self.position_avg_price
            a0 = self._atr_at_entry if not pd.isna(self._atr_at_entry) else atr_r
            if self.position_size > 0:
                if prev_pos == 0:
                    self._peak_px = h
                else:
                    self._peak_px = max(
                        self._peak_px if not pd.isna(self._peak_px) else h, h
                    )
                if (
                    self.use_breakeven
                    and not self._be_active
                    and (c - avg) >= self.be_atr_mult * atr_r
                ):
                    self._be_active = True
                    self._current_stop = max(
                        self._current_stop, avg + self.tick_size
                    )
                arm = self.runner_activate_atr_mult <= 0 or (
                    self._peak_px - avg
                ) >= self.runner_activate_atr_mult * atr_r
                if arm:
                    tline = self._peak_px - self.runner_trail_atr_mult * atr_r
                    self._current_stop = max(self._current_stop, tline)
                lim_tp = avg + tp_atr_m * a0 if tp_atr_m > 0 else None
                self.exit("XL", from_entry="Long", stop=self._current_stop, limit=lim_tp)
            elif self.position_size < 0:
                if prev_pos == 0:
                    self._trough_px = lo
                else:
                    self._trough_px = min(
                        self._trough_px if not pd.isna(self._trough_px) else lo, lo
                    )
                if (
                    self.use_breakeven
                    and not self._be_active
                    and (avg - c) >= self.be_atr_mult * atr_r
                ):
                    self._be_active = True
                    self._current_stop = min(
                        self._current_stop, avg + self.tick_size
                    )
                arm = self.runner_activate_atr_mult <= 0 or (
                    avg - self._trough_px
                ) >= self.runner_activate_atr_mult * atr_r
                if arm:
                    tline = self._trough_px + self.runner_trail_atr_mult * atr_r
                    self._current_stop = min(self._current_stop, tline)
                lim_tp = avg - tp_atr_m * a0 if tp_atr_m > 0 else None
                self.exit("XS", from_entry="Short", stop=self._current_stop, limit=lim_tp)

        if not self.use_atr_risk and self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0 and (c - self._entry_price) >= self.be_trigger_pts:
                self._be_active = True
                self._current_stop = self._entry_price + self.tick_size
                self.exit(
                    "XL",
                    from_entry="Long",
                    stop=self._current_stop,
                    limit=self._entry_price + self.tp_pts,
                )
            elif self.position_size < 0 and (self._entry_price - c) >= self.be_trigger_pts:
                self._be_active = True
                self._current_stop = self._entry_price + self.tick_size
                self.exit(
                    "XS",
                    from_entry="Short",
                    stop=self._current_stop,
                    limit=self._entry_price - self.tp_pts,
                )

        if self.position_size == 0:
            self._be_active = False

        if not self.use_atr_risk and self.use_trail_stop and self.position_size != 0:
            avg = self.position_avg_price
            if self.position_size > 0:
                profit_now = h - avg
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_min_dollars / self.point_value:
                    peak_d = self._peak_profit_pts * self.point_value
                    steps = int(peak_d // self.trail_step_amt)
                    keep_pct = min(self.trail_pct + steps * self.trail_step_pct, 95.0)
                    new_stop = avg + self._peak_profit_pts * (keep_pct / 100.0)
                    if new_stop > self._current_stop:
                        self._current_stop = new_stop
                        self.exit(
                            "XL",
                            from_entry="Long",
                            stop=self._current_stop,
                            limit=avg + self.tp_pts,
                        )
            else:
                profit_now = avg - lo
                if profit_now > self._peak_profit_pts:
                    self._peak_profit_pts = profit_now
                if self._peak_profit_pts >= self.trail_min_dollars / self.point_value:
                    peak_d = self._peak_profit_pts * self.point_value
                    steps = int(peak_d // self.trail_step_amt)
                    keep_pct = min(self.trail_pct + steps * self.trail_step_pct, 95.0)
                    new_stop = avg - self._peak_profit_pts * (keep_pct / 100.0)
                    if new_stop < self._current_stop:
                        self._current_stop = new_stop
                        self.exit(
                            "XS",
                            from_entry="Short",
                            stop=self._current_stop,
                            limit=avg - self.tp_pts,
                        )

        self._prev_pos = self.position_size
