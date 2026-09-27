"""
SuperTrend AI (Clustering) Strategy — port of LuxAlgo indicator.

Computes SuperTrend for multiple factors, evaluates rolling performance,
clusters factors via k-means, picks the best cluster's average factor,
and trades on trend flips. No repainting: signals use previous bar's state.
"""

import pandas as pd
import numpy as np
from engine.strategy import Strategy
from engine.indicators import ta
from engine.types import Direction, CommissionType, QtyType


def _kmeans_1d(data, k=3, max_iter=50):
    """Simple 1D k-means returning (centroids, labels)."""
    if len(data) == 0:
        return [0.0] * k, []
    arr = np.array(data, dtype=float)
    q = np.linspace(0, 100, k + 2)[1:-1]
    centroids = np.percentile(arr, q).tolist()
    labels = [0] * len(arr)
    for _ in range(max_iter):
        for i, v in enumerate(arr):
            dists = [abs(v - c) for c in centroids]
            labels[i] = int(np.argmin(dists))
        new_c = []
        for j in range(k):
            members = [arr[i] for i in range(len(arr)) if labels[i] == j]
            new_c.append(float(np.mean(members)) if members else centroids[j])
        if new_c == centroids:
            break
        centroids = new_c
    return centroids, labels


class SuperTrendAIStrategy(Strategy):
    title = "SuperTrend AI"
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

    atr_length = 10
    min_mult = 1.0
    max_mult = 5.0
    step = 0.5
    perf_alpha = 10.0
    from_cluster = 2  # 0=worst, 1=average, 2=best

    cooldown_bars = 3
    stop_dollars = 300.0
    tp_dollars = 600.0

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
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        hl2 = (high + low) / 2.0

        atr_series = ta.atr(df["high"], df["low"], df["close"], self.atr_length)
        atr = atr_series.values

        factors = []
        f = self.min_mult
        while f <= self.max_mult + 1e-9:
            factors.append(f)
            f += self.step
        nf = len(factors)

        alpha = 2.0 / (self.perf_alpha + 1.0)

        st_upper = np.full((n, nf), np.nan)
        st_lower = np.full((n, nf), np.nan)
        st_trend = np.zeros((n, nf), dtype=int)
        st_output = np.full((n, nf), np.nan)
        st_perf = np.zeros((n, nf))

        for k in range(nf):
            fac = factors[k]
            for i in range(1, n):
                if np.isnan(atr[i]):
                    st_upper[i, k] = hl2[i]
                    st_lower[i, k] = hl2[i]
                    st_output[i, k] = hl2[i]
                    st_trend[i, k] = st_trend[i - 1, k]
                    st_perf[i, k] = st_perf[i - 1, k]
                    continue

                up = hl2[i] + atr[i] * fac
                dn = hl2[i] - atr[i] * fac

                prev_upper = st_upper[i - 1, k] if not np.isnan(st_upper[i - 1, k]) else up
                prev_lower = st_lower[i - 1, k] if not np.isnan(st_lower[i - 1, k]) else dn

                st_upper[i, k] = min(up, prev_upper) if close[i - 1] < prev_upper else up
                st_lower[i, k] = max(dn, prev_lower) if close[i - 1] > prev_lower else dn

                if close[i] > st_upper[i, k]:
                    st_trend[i, k] = 1
                elif close[i] < st_lower[i, k]:
                    st_trend[i, k] = 0
                else:
                    st_trend[i, k] = st_trend[i - 1, k]

                st_output[i, k] = st_lower[i, k] if st_trend[i, k] == 1 else st_upper[i, k]

                prev_out = st_output[i - 1, k]
                diff = np.sign(close[i - 1] - prev_out) if not np.isnan(prev_out) else 0
                price_change = close[i] - close[i - 1]
                st_perf[i, k] = st_perf[i - 1, k] + alpha * (price_change * diff - st_perf[i - 1, k])

        self._os = np.zeros(n, dtype=int)
        self._ts = np.full(n, np.nan)
        self._target_factor = np.full(n, np.nan)

        final_upper = hl2[0]
        final_lower = hl2[0]
        final_os = 0

        for i in range(max(self.atr_length + 1, 2), n):
            perfs = st_perf[i, :]
            facs = np.array(factors)

            centroids, labels = _kmeans_1d(perfs.tolist(), k=3, max_iter=30)
            sorted_idx = np.argsort(centroids)
            cluster_map = {sorted_idx[j]: j for j in range(3)}

            target_cluster = self.from_cluster
            target_factors = [facs[m] for m in range(nf) if cluster_map.get(labels[m], -1) == target_cluster]
            tf = np.mean(target_factors) if target_factors else (self._target_factor[i - 1] if not np.isnan(self._target_factor[i - 1]) else 3.0)
            self._target_factor[i] = tf

            if not np.isnan(atr[i]):
                up = hl2[i] + atr[i] * tf
                dn = hl2[i] - atr[i] * tf
                final_upper = min(up, final_upper) if close[i - 1] < final_upper else up
                final_lower = max(dn, final_lower) if close[i - 1] > final_lower else dn

                if close[i] > final_upper:
                    final_os = 1
                elif close[i] < final_lower:
                    final_os = 0

            self._os[i] = final_os
            self._ts[i] = final_lower if final_os == 1 else final_upper

        self._last_trade_bar = -9999
        self._current_stop = 0.0
        self._entry_price = 0.0
        self._be_active = False

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
        if i < max(self.atr_length + 2, 3):
            return

        ts = df.index[i]
        prev_ts = df.index[i - 1]

        if self._session_ended(prev_ts, ts) and self.position_size != 0:
            self.close_all(comment="Session Close")

        close = df["close"].iloc[i]

        # No-repaint: use previous bar's confirmed signal
        os_now = self._os[i - 1]
        os_prev = self._os[i - 2]

        in_session = self._in_session(ts)
        cooled = i - self._last_trade_bar >= self.cooldown_bars

        long_sig = os_now == 1 and os_prev == 0 and in_session and cooled and self.position_size == 0
        short_sig = os_now == 0 and os_prev == 1 and in_session and cooled and self.position_size == 0

        if long_sig:
            self._current_stop = close - self.stop_pts
            self._entry_price = close
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Long", Direction.LONG)
            self.exit("XL", from_entry="Long", stop=self._current_stop, limit=close + self.tp_pts)
        elif short_sig:
            self._current_stop = close + self.stop_pts
            self._entry_price = close
            self._be_active = False
            self._last_trade_bar = i
            self.entry("Short", Direction.SHORT)
            self.exit("XS", from_entry="Short", stop=self._current_stop, limit=close - self.tp_pts)

        if self.position_size == 0:
            self._be_active = False

        if self.use_breakeven and self.position_size != 0 and not self._be_active:
            if self.position_size > 0:
                if close - self._entry_price >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price + self.tick_size
                    self.exit("XL", from_entry="Long", stop=self._current_stop,
                              limit=self._entry_price + self.tp_pts)
            elif self.position_size < 0:
                if self._entry_price - close >= self.be_trigger_pts:
                    self._be_active = True
                    self._current_stop = self._entry_price - self.tick_size
                    self.exit("XS", from_entry="Short", stop=self._current_stop,
                              limit=self._entry_price - self.tp_pts)
