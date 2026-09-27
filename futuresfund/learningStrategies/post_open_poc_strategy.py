"""
Post-Open POC Strategy v5: Tightened ORB with adaptive filters.

New filters vs v4:
- OR size filter: skip days where opening range is too wide or too narrow
- Breakout strength: price must close X pts beyond the range boundary
- Daily loss limit: stop trading after losing $X in a day
- Consecutive loss cooldown: pause N days after M consecutive losses
- Max stop cap: even in range mode, cap the stop at max_stop_dollars
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.types import Direction, CommissionType, QtyType


def _poc_from_bars(df, start_idx, end_idx, rows=24):
    if end_idx < start_idx or end_idx >= len(df):
        return None
    prof_hi = df["high"].iloc[start_idx : end_idx + 1].max()
    prof_lo = df["low"].iloc[start_idx : end_idx + 1].min()
    span = prof_hi - prof_lo
    if span <= 0:
        return None
    step = span / rows
    vol = np.zeros(rows)
    for j in range(start_idx, end_idx + 1):
        v = df["volume"].iloc[j]
        if v <= 0:
            continue
        lo, hi = df["low"].iloc[j], df["high"].iloc[j]
        s = max(int((lo - prof_lo) / step), 0)
        e = min(int((hi - prof_lo) / step), rows - 1)
        n = max(e - s + 1, 1)
        for k in range(n):
            r = s + k
            if 0 <= r < rows:
                vol[r] += v / n
    return prof_lo + (int(np.argmax(vol)) + 0.5) * step


class PostOpenPOCStrategy(Strategy):
    title = "Post-Open POC (ORB Tightened)"
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

    # Gathering period
    gather_minutes = 15

    # Entry window
    entry_end_hr = 11
    entry_end_mn = 0

    # EOD
    close_eod_hr = 15
    close_eod_mn = 45

    # RTH
    rth_start_hr = 9
    rth_start_mn = 30
    rth_end_hr = 16
    rth_end_mn = 0

    # POC
    poc_rows = 24
    poc_filter = False

    # Entry
    confirm_bars = 1
    max_attempts = 2

    # --- NEW FILTERS ---
    or_min_pts = 2.0          # Skip if opening range < this (too narrow = noise)
    or_max_pts = 20.0         # Skip if opening range > this (too wide = choppy)
    breakout_margin_pts = 0.5 # Close must be this many pts beyond the range boundary
    daily_loss_limit = 400.0  # Stop trading for the day after losing this much ($)
    consec_loss_max = 3       # After this many consecutive losses...
    consec_loss_pause = 1     # ...pause this many days
    max_stop_dollars = 500.0  # Cap the range-based stop at this dollar amount

    # Risk
    stop_mode = "range"
    stop_dollars = 200.0
    tp_dollars = 500.0

    # Trailing stop
    use_trail = True
    trail_activate_dollars = 400.0
    trail_keep_pct = 40.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value if self.tp_dollars > 0 else 0
        self.trail_activate_pts = self.trail_activate_dollars / self.point_value
        self.max_stop_pts = self.max_stop_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)
        if n == 0:
            self.prev_day_poc = pd.Series(dtype=float)
            return

        try:
            self._tz = "America/New_York" if hasattr(df.index[0], "tz_convert") else None
        except Exception:
            self._tz = None

        rth_s = self.rth_start_hr * 100 + self.rth_start_mn
        rth_e = self.rth_end_hr * 100 + self.rth_end_mn
        day_bars = {}

        for i in range(n):
            hr, mn, dt = self._parse_ts(df.index[i])
            if rth_s <= (hr * 100 + mn) < rth_e:
                if dt not in day_bars:
                    day_bars[dt] = []
                day_bars[dt].append(i)

        day_poc = {}
        for dt, indices in day_bars.items():
            if len(indices) < 5:
                continue
            poc = _poc_from_bars(df, indices[0], indices[-1], self.poc_rows)
            if poc is not None:
                day_poc[dt] = poc

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

        self.prev_day_poc = pd.Series(prev_pocs, index=df.index)

        # State
        self._last_day = None
        self._day_attempts = 0
        self._or_high = 0.0
        self._or_low = 99999.0
        self._or_done = False
        self._or_valid = False
        self._peak_profit_pts = 0.0
        self._current_stop = 0.0
        self._day_pnl = 0.0
        self._day_locked = False
        self._consec_losses = 0
        self._pause_until = None
        self._entry_equity = 0.0

    def _parse_ts(self, ts):
        try:
            if self._tz and hasattr(ts, "tz_convert"):
                et = ts.tz_convert("America/New_York")
            else:
                et = ts
            return et.hour, et.minute, et.date() if hasattr(et, "date") else None
        except Exception:
            return 0, 0, None

    def _minutes_from_open(self, hr, mn):
        return (hr - 9) * 60 + (mn - 30)

    def next(self):
        i = self.bar_index
        df = self._data
        if i < 1:
            return

        hr, mn, today = self._parse_ts(df.index[i])
        if today is None:
            return

        bar_t = hr * 100 + mn
        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]

        rth_s = self.rth_start_hr * 100 + self.rth_start_mn

        # Day reset
        if today != self._last_day:
            # Track trade result from yesterday
            if self._last_day is not None and self._day_pnl < -1:
                self._consec_losses += 1
            elif self._last_day is not None and self._day_pnl > 1:
                self._consec_losses = 0

            self._day_attempts = 0
            self._last_day = today
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
            import datetime
            self._pause_until = today + datetime.timedelta(days=self.consec_loss_pause)
            self._consec_losses = 0
            if self.position_size != 0:
                self.close_all(comment="PAUSE")
            return

        if bar_t < rth_s:
            return

        # Daily P&L tracking
        self._day_pnl = self.equity - self._entry_equity

        # Daily loss limit
        if self.daily_loss_limit > 0 and self._day_pnl <= -self.daily_loss_limit:
            if not self._day_locked:
                self._day_locked = True
                if self.position_size != 0:
                    self.close_all(comment="DayLimit")
            return

        # EOD flatten
        if bar_t >= (self.close_eod_hr * 100 + self.close_eod_mn) and self.position_size != 0:
            self.close_all(comment="EOD")
            return

        # Gathering phase
        mins = self._minutes_from_open(hr, mn)
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

        # Trailing stop management
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
                        if self.tp_pts > 0:
                            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=avg + self.tp_pts)
                        else:
                            self.exit("XL", from_entry="Long", stop=self._current_stop)
                else:
                    ns = avg - lock
                    if ns < self._current_stop:
                        self._current_stop = ns
                        if self.tp_pts > 0:
                            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=avg - self.tp_pts)
                        else:
                            self.exit("XS", from_entry="Short", stop=self._current_stop)

        # Entry window check
        end_t = self.entry_end_hr * 100 + self.entry_end_mn
        if bar_t >= end_t:
            return

        or_range = self._or_high - self._or_low

        # Breakout detection with margin
        breakout_long = close > (self._or_high + self.breakout_margin_pts)
        breakout_short = close < (self._or_low - self.breakout_margin_pts)

        # Confirmation
        confirmed_long, confirmed_short = True, True
        for k in range(self.confirm_bars):
            idx = i - k
            if idx < 0:
                confirmed_long = confirmed_short = False
                break
            if df["close"].iloc[idx] <= self._or_high:
                confirmed_long = False
            if df["close"].iloc[idx] >= self._or_low:
                confirmed_short = False

        # POC directional filter
        prev_poc = self.prev_day_poc.iloc[i]
        if self.poc_filter and not pd.isna(prev_poc):
            if close <= prev_poc:
                confirmed_long = False
            if close >= prev_poc:
                confirmed_short = False

        long_sig = breakout_long and confirmed_long and self.position_size <= 0
        short_sig = breakout_short and confirmed_short and self.position_size >= 0

        if self._day_attempts >= self.max_attempts:
            long_sig = short_sig = False

        if self._day_locked:
            long_sig = short_sig = False

        if long_sig:
            self._day_attempts += 1
            self._peak_profit_pts = 0.0
            if self.stop_mode == "range":
                raw_stop = self._or_low
                if (close - raw_stop) > self.max_stop_pts:
                    raw_stop = close - self.max_stop_pts
                self._current_stop = raw_stop
            else:
                self._current_stop = close - self.stop_pts
            self.entry("Long", Direction.LONG)
            if self.tp_pts > 0:
                self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close + self.tp_pts)
            else:
                self.exit("XL", from_entry="Long", stop=self._current_stop)
        elif short_sig:
            self._day_attempts += 1
            self._peak_profit_pts = 0.0
            if self.stop_mode == "range":
                raw_stop = self._or_high
                if (raw_stop - close) > self.max_stop_pts:
                    raw_stop = close + self.max_stop_pts
                self._current_stop = raw_stop
            else:
                self._current_stop = close + self.stop_pts
            self.entry("Short", Direction.SHORT)
            if self.tp_pts > 0:
                self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close - self.tp_pts)
            else:
                self.exit("XS", from_entry="Short", stop=self._current_stop)
        elif self.position_size > 0 and not self.use_trail:
            avg = self.position_avg_price
            if self.tp_pts > 0:
                self.exit("XL", from_entry="Long", stop=avg - self.stop_pts, limit=avg + self.tp_pts)
            else:
                self.exit("XL", from_entry="Long", stop=avg - self.stop_pts)
        elif self.position_size < 0 and not self.use_trail:
            avg = self.position_avg_price
            if self.tp_pts > 0:
                self.exit("XS", from_entry="Short", stop=avg + self.stop_pts, limit=avg - self.tp_pts)
            else:
                self.exit("XS", from_entry="Short", stop=avg + self.stop_pts)
