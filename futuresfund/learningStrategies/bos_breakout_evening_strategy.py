"""
Break of Structure (BOS) Breakout Strategy -- Evening Session ES 2min.

Adapted from the EURUSD hourly BOS notebook:
1. EMA trend filter: all candles in a rolling window must be above/below EMA
2. Pivot detection: rolling centered window for pivot highs/lows
3. BOS detection:
   - Bullish: PH found -> PL after PH has lower low than PL before PH (sweep)
             -> first close above PH.High triggers long entry
   - Bearish: PL found -> PH after PL has higher high than PH before PL
             -> first close below PL.Low triggers short entry
4. ATR-based stop loss and take profit
5. Session filter: 18:00-8:00 ET (evening/overnight)
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


class BOSBreakoutEveningStrategy(Strategy):
    title = "BOS Breakout Evening ES 2min"
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

    # EMA trend filter
    ema_length = 50
    ema_trend_window = 10  # how many bars must all be above/below EMA
    use_ema_filter = False  # Pine default is off

    # Pivot detection
    pivot_window = 4  # bars on each side for pivot detection

    # BOS lookback
    backcandles = 25  # how far back to look for the BOS pattern

    # ATR for SL/TP
    atr_length = 14
    atr_sl_mult = 2.0  # stop = ATR * this, used only when stop_dollars is 0
    atr_tp_mult = 4.0  # tp = ATR * this, used only when tp_dollars is 0

    # Dollar-based SL/TP (override ATR if > 0). Pine defaults are $500 / $1000.
    stop_dollars = 500.0
    tp_dollars = 1000.0

    # Cooldown
    cooldown_bars = 5

    # Session
    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    # Breakeven
    use_breakeven = False
    be_trigger_dollars = 200.0

    # Reverse after a stop-out. Pine default is on.
    flip_on_stopout = True
    min_loss_to_flip = 100.0
    # Bars after the broker flattens the stop before the reversal order.
    # One bar matches TradingView: the script sees the flat position on the
    # bar after the fill and enters on that bar's close.
    # The broker flattens a stop before next() sees it. TradingView's script
    # sees that flat position on the next bar and sends the reversal on the
    # bar after that. Two bars reproduces the tester's reversal prices.
    flip_delay_bars = 2

    def configure(self):
        if self.stop_dollars > 0:
            self.stop_pts = self.stop_dollars / self.point_value
        else:
            self.stop_pts = 0
        if self.tp_dollars > 0:
            self.tp_pts = self.tp_dollars / self.point_value
        else:
            self.tp_pts = 0
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)

        self.ema_val = ta.ema(df["close"], self.ema_length)
        self.atr_val = ta.atr(df["high"], df["low"], df["close"], self.atr_length)

        # Precompute pivot highs and lows
        self._pivot_type = np.zeros(n, dtype=int)  # 0=none, 1=PH, 2=PL, 3=both
        span = 2 * self.pivot_window + 1
        roll_max = df["high"].rolling(span, center=True).max()
        roll_min = df["low"].rolling(span, center=True).min()
        ph_mask = (df["high"] >= roll_max) & roll_max.notna()
        pl_mask = (df["low"] <= roll_min) & roll_min.notna()
        self._pivot_type[ph_mask.values] += 1
        self._pivot_type[pl_mask.values] += 2

        # Precompute EMA signal (trend alignment)
        above_ema = (np.minimum(df["open"].values, df["close"].values) > self.ema_val.values).astype(int)
        below_ema = (np.maximum(df["open"].values, df["close"].values) < self.ema_val.values).astype(int)
        win = self.ema_trend_window + 1
        above_roll = pd.Series(above_ema).rolling(win, min_periods=win).sum()
        below_roll = pd.Series(below_ema).rolling(win, min_periods=win).sum()
        upt = (above_roll == win).values
        dnt = (below_roll == win).values
        self._ema_signal = np.zeros(n, dtype=int)
        self._ema_signal[upt & ~dnt] = 2  # bullish trend
        self._ema_signal[dnt & ~upt] = 1  # bearish trend

        # Precompute BOS signals
        self._bos_signal = np.zeros(n, dtype=int)
        self._bos_pivot_ref = np.full(n, -1, dtype=int)
        self._precompute_bos(df, n)

        # Combine with EMA filter (optional)
        if self.use_ema_filter:
            for i in range(n):
                if self._bos_signal[i] != 0 and self._ema_signal[i] != self._bos_signal[i]:
                    self._bos_signal[i] = 0
                    self._bos_pivot_ref[i] = -1

        # State
        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._be_active = False
        self._prev_position_size = 0.0
        self._flip_next = 0
        self._flip_wait = 0

    def _precompute_bos(self, df, n):
        pw = self.pivot_window
        bc = self.backcandles

        for candle in range(bc, n):
            if candle < 1:
                continue

            close_now = df["close"].iloc[candle]
            close_prev = df["close"].iloc[candle - 1]
            start = candle - bc

            # Confirmed pivots: indices where pivot is at least pw bars before current
            piv_end = candle - pw
            if piv_end <= start:
                continue

            # Collect pivot highs and lows in the confirmed region
            ph_indices = []
            pl_indices = []
            for j in range(start, piv_end + 1):
                pt = self._pivot_type[j]
                if pt == 1 or pt == 3:
                    ph_indices.append(j)
                if pt == 2 or pt == 3:
                    pl_indices.append(j)

            # Bullish: find latest PH, then check PLs before and after
            if ph_indices:
                ph_idx = ph_indices[-1]
                ph_val = df["high"].iloc[ph_idx]

                if close_now > ph_val and close_prev <= ph_val:
                    pl_before = [j for j in pl_indices if j < ph_idx]
                    pl_after = [j for j in pl_indices if j > ph_idx]

                    if pl_before and pl_after:
                        pl1_val = df["low"].iloc[pl_before[-1]]
                        pl2_val = min(df["low"].iloc[j] for j in pl_after)
                        if pl2_val < pl1_val:
                            self._bos_signal[candle] = 2
                            self._bos_pivot_ref[candle] = ph_idx
                            continue

            # Bearish: find latest PL, then check PHs before and after
            if pl_indices:
                pl_idx = pl_indices[-1]
                pl_val = df["low"].iloc[pl_idx]

                if close_now < pl_val and close_prev >= pl_val:
                    ph_before = [j for j in ph_indices if j < pl_idx]
                    ph_after = [j for j in ph_indices if j > pl_idx]

                    if ph_before and ph_after:
                        ph1_val = df["high"].iloc[ph_before[-1]]
                        ph2_val = max(df["high"].iloc[j] for j in ph_after)
                        if ph2_val > ph1_val:
                            self._bos_signal[candle] = 1
                            self._bos_pivot_ref[candle] = pl_idx

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

        min_bars = max(self.backcandles, self.ema_length, self.atr_length) + 5
        if i < min_bars:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1] if i > 0 else ts

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        cl = df["close"].iloc[i]
        hi = df["high"].iloc[i]
        lo = df["low"].iloc[i]
        atr = self.atr_val.iloc[i]
        if pd.isna(atr) or atr <= 0:
            self._prev_position_size = self.position_size
            return

        # Determine stop/tp distances
        if self.stop_dollars > 0:
            sl_dist = self.stop_pts
        else:
            sl_dist = atr * self.atr_sl_mult

        if self.tp_dollars > 0:
            tp_dist = self.tp_pts
        else:
            tp_dist = atr * self.atr_tp_mult

        sig = self._bos_signal[i]
        in_session = self._in_session(ts)
        cooled_down = i - self._last_trade_bar >= self.cooldown_bars
        flat = self.position_size == 0

        # The broker flattens a stop before next() runs. The reversal waits
        # flip_delay_bars so the order goes out on the same bar as the tester.
        just_closed = flat and self._prev_position_size != 0
        fire_flip = 0
        if self._flip_next:
            self._flip_wait -= 1
            if self._flip_wait <= 0:
                fire_flip = self._flip_next
                self._flip_next = 0
        if self.flip_on_stopout and just_closed and self._broker.closed_trades:
            last = self._broker.closed_trades[-1]
            if last.profit < -self.min_loss_to_flip:
                self._flip_next = 1 if last.direction == Direction.SHORT else -1
                self._flip_wait = self.flip_delay_bars

        long_sig = sig == 2 and in_session and cooled_down and flat
        short_sig = sig == 1 and in_session and cooled_down and flat
        flip_long = fire_flip == 1 and in_session and flat
        flip_short = fire_flip == -1 and in_session and flat

        if long_sig or flip_long:
            self._current_stop = cl - sl_dist
            self._entry_price = cl
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG, comment="flip" if flip_long and not long_sig else "")
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=cl + tp_dist)
        elif short_sig or flip_short:
            self._current_stop = cl + sl_dist
            self._entry_price = cl
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Short", Direction.SHORT, comment="flip" if flip_short and not short_sig else "")
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=cl - tp_dist)

        self._prev_position_size = self.position_size

        if self.position_size == 0:
            self._be_active = False

        # Breakeven management
        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if cl - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + self.tick_size
                    if self.tp_dollars > 0:
                        tp_lim = self._entry_price + self.tp_pts
                    else:
                        tp_lim = self._entry_price + tp_dist
                    self.exit("XL", from_entry="Long", stop=self._current_stop, limit=tp_lim)
            elif self.position_size < 0:
                if self._entry_price - cl >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - self.tick_size
                    if self.tp_dollars > 0:
                        tp_lim = self._entry_price - self.tp_pts
                    else:
                        tp_lim = self._entry_price - tp_dist
                    self.exit("XS", from_entry="Short", stop=self._current_stop, limit=tp_lim)
