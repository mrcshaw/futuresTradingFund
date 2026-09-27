"""
Market Structure Break & Order Block Strategy — Evening Session ES 2min.

Detects Market Structure Breaks (MSBs) via zigzag swing analysis,
identifies Order Blocks (last opposing candle before the impulse move),
then enters when price pulls back into the OB zone.

Two entry modes:
  - "ob_pullback": wait for price to re-enter the OB zone after MSB
  - "msb_immediate": enter right on the MSB signal bar

No repainting: all signals use confirmed (prior bar) data.
"""

import pandas as pd
import numpy as np

from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


class MsbObStrategy(Strategy):
    title = "MSB Order Block"
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

    zigzag_len = 9
    fib_factor = 0.33
    entry_mode = "ob_pullback"  # "ob_pullback" or "msb_immediate"
    cooldown_bars = 3
    stop_dollars = 300.0
    tp_dollars = 600.0
    ob_expire_bars = 50

    use_session = True
    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    use_breakeven = False
    be_trigger_dollars = 150.0

    def configure(self):
        self.stop_pts = self.stop_dollars / self.point_value
        self.tp_pts = self.tp_dollars / self.point_value
        self.be_trigger_pts = self.be_trigger_dollars / self.point_value

    def init(self):
        df = self._data
        n = len(df)
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        o = df["open"].values

        zl = self.zigzag_len

        highest = pd.Series(h).rolling(zl, min_periods=zl).max().values
        lowest = pd.Series(l).rolling(zl, min_periods=zl).min().values

        to_up = np.zeros(n, dtype=bool)
        to_down = np.zeros(n, dtype=bool)
        for i in range(zl - 1, n):
            to_up[i] = h[i] >= highest[i]
            to_down[i] = l[i] <= lowest[i]

        trend = np.zeros(n, dtype=int)
        trend[0] = 1
        for i in range(1, n):
            prev = trend[i - 1]
            if prev == 1 and to_down[i]:
                trend[i] = -1
            elif prev == -1 and to_up[i]:
                trend[i] = 1
            else:
                trend[i] = prev

        high_points = []
        high_indices = []
        low_points = []
        low_indices = []

        def _swing_low_at(i):
            """Find the lowest low between the last to_up and bar i."""
            best_val = l[i]
            best_idx = i
            j = i
            while j >= 0:
                if l[j] < best_val:
                    best_val = l[j]
                    best_idx = j
                if to_up[j] and j < i:
                    break
                j -= 1
            return best_val, best_idx

        def _swing_high_at(i):
            """Find the highest high between the last to_down and bar i."""
            best_val = h[i]
            best_idx = i
            j = i
            while j >= 0:
                if h[j] > best_val:
                    best_val = h[j]
                    best_idx = j
                if to_down[j] and j < i:
                    break
                j -= 1
            return best_val, best_idx

        for i in range(1, n):
            if trend[i] != trend[i - 1]:
                if trend[i] == 1:
                    lv, li = _swing_low_at(i)
                    low_points.append(lv)
                    low_indices.append(li)
                elif trend[i] == -1:
                    hv, hi_ = _swing_high_at(i)
                    high_points.append(hv)
                    high_indices.append(hi_)

        self._msb_events = []
        self._ob_zones = []

        market = 1
        hp_idx = 0
        lp_idx = 0

        hp_list = list(zip(high_points, high_indices))
        lp_list = list(zip(low_points, low_indices))

        h0 = h1 = l0 = l1 = 0.0
        h0i = h1i = l0i = l1i = 0
        last_msb_l0 = None
        last_msb_h0 = None

        for i in range(1, n):
            if trend[i] != trend[i - 1]:
                if trend[i] == -1 and hp_idx < len(hp_list):
                    h1, h1i = h0, h0i
                    h0, h0i = hp_list[hp_idx]
                    hp_idx += 1
                elif trend[i] == 1 and lp_idx < len(lp_list):
                    l1, l1i = l0, l0i
                    l0, l0i = lp_list[lp_idx]
                    lp_idx += 1

            if h0 == 0 or l0 == 0 or h1 == 0 or l1 == 0:
                continue

            skip = False
            if last_msb_l0 is not None and l0 == last_msb_l0:
                skip = True
            if last_msb_h0 is not None and h0 == last_msb_h0:
                skip = True

            prev_market = market
            if not skip:
                if market == 1 and l0 < l1 and l0 < l1 - abs(h0 - l1) * self.fib_factor:
                    market = -1
                    last_msb_l0 = l0
                    last_msb_h0 = h0
                elif market == -1 and h0 > h1 and h0 > h1 + abs(h1 - l0) * self.fib_factor:
                    market = 1
                    last_msb_l0 = l0
                    last_msb_h0 = h0

            if market != prev_market:
                self._msb_events.append((i, market))

                if market == 1:
                    ob_top, ob_bot, ob_bar = self._find_bu_ob(o, c, h, l, h1i, l0i, zl)
                    if ob_top is not None:
                        self._ob_zones.append({
                            "bar": i, "dir": 1,
                            "top": ob_top, "bot": ob_bot, "ob_bar": ob_bar
                        })
                elif market == -1:
                    ob_top, ob_bot, ob_bar = self._find_be_ob(o, c, h, l, l1i, h0i, zl)
                    if ob_top is not None:
                        self._ob_zones.append({
                            "bar": i, "dir": -1,
                            "top": ob_top, "bot": ob_bot, "ob_bar": ob_bar
                        })

        self._last_trade_bar = -9999
        self._entry_price = 0.0
        self._be_active = False

    def _find_bu_ob(self, o, c, h, l, from_idx, to_idx, zl):
        """Last bearish candle between swing high and swing low → bullish OB."""
        start = max(from_idx, 0)
        end = min(to_idx, len(o) - 1)
        best_bar = None
        for j in range(start, end + 1):
            if o[j] > c[j]:
                best_bar = j
        if best_bar is not None:
            return h[best_bar], l[best_bar], best_bar
        return None, None, None

    def _find_be_ob(self, o, c, h, l, from_idx, to_idx, zl):
        """Last bullish candle between swing low and swing high → bearish OB."""
        start = max(from_idx, 0)
        end = min(to_idx, len(o) - 1)
        best_bar = None
        for j in range(start, end + 1):
            if o[j] < c[j]:
                best_bar = j
        if best_bar is not None:
            return h[best_bar], l[best_bar], best_bar
        return None, None, None

    def _in_session(self, ts):
        if not self.use_session:
            return True
        try:
            ts_et = ts.tz_convert("America/New_York") if hasattr(ts, "tz_convert") else ts
            hr, mn = ts_et.hour, ts_et.minute
        except Exception:
            hr = getattr(ts, "hour", 0)
            mn = getattr(ts, "minute", 0)
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

    def next(self):
        i = self.bar_index
        df = self._data
        if i < self.zigzag_len + 5:
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1]

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        close_now = df["close"].iloc[i]
        low_now = df["low"].iloc[i]
        high_now = df["high"].iloc[i]

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars

        if self.entry_mode == "msb_immediate":
            for bar_idx, direction in self._msb_events:
                if bar_idx == i - 1 and in_session and cooled and self.position_size == 0:
                    if direction == 1:
                        self._entry_price = close_now
                        self._be_active = False
                        self._last_trade_bar = i
                        self.entry("Long", Direction.LONG)
                        self.exit("XL", from_entry="Long",
                                  stop=close_now - self.stop_pts,
                                  limit=close_now + self.tp_pts)
                    elif direction == -1:
                        self._entry_price = close_now
                        self._be_active = False
                        self._last_trade_bar = i
                        self.entry("Short", Direction.SHORT)
                        self.exit("XS", from_entry="Short",
                                  stop=close_now + self.stop_pts,
                                  limit=close_now - self.tp_pts)
                    break

        elif self.entry_mode == "ob_pullback":
            if in_session and cooled and self.position_size == 0:
                for oz in reversed(self._ob_zones):
                    if oz["bar"] >= i:
                        continue
                    if i - oz["bar"] > self.ob_expire_bars:
                        continue

                    broken = False
                    for check_bar in range(oz["bar"] + 1, i):
                        cb = df["close"].iloc[check_bar]
                        if oz["dir"] == 1 and cb < oz["bot"]:
                            broken = True
                            break
                        if oz["dir"] == -1 and cb > oz["top"]:
                            broken = True
                            break
                    if broken:
                        continue

                    if oz["dir"] == 1 and low_now <= oz["top"] and close_now >= oz["bot"]:
                        self._entry_price = close_now
                        self._be_active = False
                        self._last_trade_bar = i
                        self.entry("Long", Direction.LONG)
                        self.exit("XL", from_entry="Long",
                                  stop=close_now - self.stop_pts,
                                  limit=close_now + self.tp_pts)
                        break
                    elif oz["dir"] == -1 and high_now >= oz["bot"] and close_now <= oz["top"]:
                        self._entry_price = close_now
                        self._be_active = False
                        self._last_trade_bar = i
                        self.entry("Short", Direction.SHORT)
                        self.exit("XS", from_entry="Short",
                                  stop=close_now + self.stop_pts,
                                  limit=close_now - self.tp_pts)
                        break

        if self.position_size == 0:
            self._be_active = False

        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if close_now - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self.exit("XL", from_entry="Long",
                              stop=self._entry_price + self.tick_size,
                              limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                if self._entry_price - close_now >= self.be_trigger_pts:
                    self._be_active = True
                    self.exit("XS", from_entry="Short",
                              stop=self._entry_price - self.tick_size,
                              limit=self._entry_price - self.tp_pts)
