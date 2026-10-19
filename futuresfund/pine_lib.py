"""The library compiled Pine scripts call: na rules, history, ta.*, math.*, arrays, strings, drawings.

na is NaN for numbers and None for objects. Any comparison with na is false,
and na in a condition is false, the way Pine treats it. Each ta.* call site
keeps its own state, updated once per bar, like TradingView's built-ins.
"""

from __future__ import annotations

import copy as _copy
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

nan = math.nan
UNSET = object()


class PineError(RuntimeError):
    """A runtime error TradingView would stop the script on too."""


# ---------- na rules ----------

def t(x) -> bool:
    if x is True:
        return True
    if x is False or x is None:
        return False
    return x == x and bool(x)


def land(a, b) -> bool:
    """Pine's and. Both sides are already evaluated; Pine does not skip the right side."""
    return t(a) and t(b)


def lor(a, b) -> bool:
    """Pine's or. Both sides are already evaluated."""
    return t(a) or t(b)


def isna(x) -> bool:
    return x is None or (isinstance(x, float) and x != x)


def nz(x, y=0):
    return y if isna(x) else x


def ne(a, b) -> bool:
    if isna(a) or isna(b):
        return False
    return a != b


def div(a, b):
    if isna(a) or isna(b) or b == 0:
        return nan
    return a / b


def mod(a, b):
    if isna(a) or isna(b) or b == 0:
        return nan
    value = math.fmod(a, b)
    return int(value) if isinstance(a, int) and isinstance(b, int) else value


def to_int(x):
    if isna(x):
        return nan
    return int(x)


def to_float(x):
    if isna(x):
        return nan
    return float(x)


def hx(series, i: int, k):
    """series[k] for a built-in series: the value k bars back."""
    if isna(k):
        return nan
    j = i - int(k)
    if 0 <= j <= i:
        return series[j]
    return nan


def hv(history: list, k, current):
    """x[k] for a script variable. history holds the closing value of each earlier bar."""
    if isna(k):
        return nan
    k = int(k)
    if k == 0:
        return current
    if 0 < k <= len(history):
        return history[-k]
    return nan


def rng(start, end, step=None):
    """A Pine for loop: both ends included, counting down when the end is below the start."""
    if isna(start) or isna(end):
        return ()
    a = int(start)
    b = int(end)
    if step is None or isna(step):
        step = 1 if a <= b else -1
    else:
        step = int(abs(step)) if a <= b else -int(abs(step))
        if step == 0:
            raise PineError("A for loop step of 0 never ends.")
    return range(a, b + (1 if step > 0 else -1), step)


def items(collection):
    if collection is None:
        return ()
    return collection


# ---------- call-site state ----------

class Site:
    __slots__ = ("i", "cur", "hist", "vals", "prev", "seed", "a", "b", "c", "last", "count", "extra")

    def __init__(self) -> None:
        self.i = None
        self.cur = nan
        self.hist = []
        self.vals = []
        self.prev = None
        self.seed = []
        self.a = None
        self.b = None
        self.c = None
        self.last = None
        self.count = 0
        self.extra = None


def _begin(st: Site, i: int) -> bool:
    """True the first time this call site runs on bar i."""
    if st.i == i:
        return False
    if st.i is not None:
        st.hist.append(st.cur)
    st.i = i
    return True


def hs(st: Site, k):
    """f(...)[k] for a ta.* call: that call's value k bars back."""
    if isna(k):
        return nan
    k = int(k)
    if k == 0:
        return st.cur
    if 0 < k <= len(st.hist):
        return st.hist[-k]
    return nan


def _length(value) -> int | None:
    if isna(value):
        return None
    n = int(value)
    return n if n > 0 else None


class _Average:
    """An exponential average seeded with the simple average of its first values, like ta.ema and ta.rma."""

    __slots__ = ("alpha", "prev", "seed", "length")

    def __init__(self) -> None:
        self.prev = None
        self.seed = []
        self.length = None
        self.alpha = 0.0

    def update(self, value, length, wilder: bool):
        n = _length(length)
        if n is None:
            return nan
        if n != self.length:
            self.length = n
            self.alpha = 1.0 / n if wilder else 2.0 / (n + 1.0)
        if isna(value):
            return self.prev if self.prev is not None else nan
        if self.prev is None:
            self.seed.append(value)
            if len(self.seed) >= n:
                self.prev = sum(self.seed[-n:]) / n
                return self.prev
            return nan
        self.prev = self.alpha * value + (1.0 - self.alpha) * self.prev
        return self.prev


def _window(st: Site, value, length):
    st.vals.append(value)
    n = _length(length)
    if n is None or len(st.vals) < n:
        return None
    return st.vals[-n:]


# ---------- ta.* ----------

def ta_sma(st, i, rt, source, length):
    if _begin(st, i):
        window = _window(st, source, length)
        st.cur = nan if window is None or any(isna(v) for v in window) else sum(window) / len(window)
    return st.cur


def ta_ema(st, i, rt, source, length):
    if _begin(st, i):
        if st.extra is None:
            st.extra = _Average()
        st.cur = st.extra.update(source, length, False)
    return st.cur


def ta_rma(st, i, rt, source, length):
    if _begin(st, i):
        if st.extra is None:
            st.extra = _Average()
        st.cur = st.extra.update(source, length, True)
    return st.cur


def ta_wma(st, i, rt, source, length):
    if _begin(st, i):
        window = _window(st, source, length)
        if window is None or any(isna(v) for v in window):
            st.cur = nan
        else:
            n = len(window)
            st.cur = sum(value * (k + 1) for k, value in enumerate(window)) / (n * (n + 1) / 2)
    return st.cur


def ta_vwma(st, i, rt, source, length):
    if _begin(st, i):
        volume = rt.V[i]
        window = _window(st, (source * volume if not isna(source) else nan, volume), length)
        if window is None or any(isna(pair[0]) for pair in window):
            st.cur = nan
        else:
            total = sum(pair[1] for pair in window)
            st.cur = sum(pair[0] for pair in window) / total if total else nan
    return st.cur


def true_range(rt, i, handle_na=True):
    if i == 0:
        return rt.H[0] - rt.L[0] if handle_na else nan
    prior = rt.C[i - 1]
    return max(rt.H[i] - rt.L[i], abs(rt.H[i] - prior), abs(rt.L[i] - prior))


def ta_tr(st, i, rt, handle_na=False):
    if _begin(st, i):
        st.cur = true_range(rt, i, t(handle_na))
    return st.cur


def ta_atr(st, i, rt, length):
    if _begin(st, i):
        if st.extra is None:
            st.extra = _Average()
        st.cur = st.extra.update(true_range(rt, i, True), length, True)
    return st.cur


def ta_rsi(st, i, rt, source, length):
    if _begin(st, i):
        if st.extra is None:
            st.extra = (_Average(), _Average())
        previous = st.prev
        st.prev = source
        if previous is None or isna(previous) or isna(source):
            st.cur = nan
            return st.cur
        change = source - previous
        up = st.extra[0].update(max(change, 0.0), length, True)
        down = st.extra[1].update(max(-change, 0.0), length, True)
        if isna(up) or isna(down):
            st.cur = nan
        elif down == 0:
            st.cur = 100.0
        elif up == 0:
            st.cur = 0.0
        else:
            st.cur = 100.0 - 100.0 / (1.0 + up / down)
    return st.cur


def ta_stdev(st, i, rt, source, length, biased=True):
    if _begin(st, i):
        window = _window(st, source, length)
        if window is None or any(isna(v) for v in window):
            st.cur = nan
        else:
            n = len(window)
            mean = sum(window) / n
            spread = sum((value - mean) ** 2 for value in window)
            st.cur = math.sqrt(spread / n if t(biased) or n < 2 else spread / (n - 1))
    return st.cur


def _extreme(st, i, rt, args, pick, default_series):
    if len(args) == 1:
        source, length = default_series[i], args[0]
    else:
        source, length = args[0], args[1]
    window = _window(st, source, length)
    if window is None:
        return nan
    values = [value for value in window if not isna(value)]
    return pick(values) if values else nan


def ta_highest(st, i, rt, *args):
    if _begin(st, i):
        st.cur = _extreme(st, i, rt, args, max, rt.H)
    return st.cur


def ta_lowest(st, i, rt, *args):
    if _begin(st, i):
        st.cur = _extreme(st, i, rt, args, min, rt.L)
    return st.cur


def _extreme_bars(st, i, rt, args, highest: bool):
    series = rt.H if highest else rt.L
    if len(args) == 1:
        source, length = series[i], args[0]
    else:
        source, length = args[0], args[1]
    window = _window(st, source, length)
    if window is None:
        return nan
    best = None
    offset = 0
    for back, value in enumerate(reversed(window)):
        if isna(value):
            continue
        if best is None or (value > best if highest else value < best):
            best = value
            offset = back
    return -offset if best is not None else nan


def ta_highestbars(st, i, rt, *args):
    if _begin(st, i):
        st.cur = _extreme_bars(st, i, rt, args, True)
    return st.cur


def ta_lowestbars(st, i, rt, *args):
    if _begin(st, i):
        st.cur = _extreme_bars(st, i, rt, args, False)
    return st.cur


def ta_crossover(st, i, rt, a, b):
    if _begin(st, i):
        previous = st.prev or (nan, nan)
        st.prev = (a, b)
        st.cur = bool(a > b and previous[0] <= previous[1])
    return st.cur


def ta_crossunder(st, i, rt, a, b):
    if _begin(st, i):
        previous = st.prev or (nan, nan)
        st.prev = (a, b)
        st.cur = bool(a < b and previous[0] >= previous[1])
    return st.cur


def ta_cross(st, i, rt, a, b):
    if _begin(st, i):
        previous = st.prev or (nan, nan)
        st.prev = (a, b)
        st.cur = bool((a > b and previous[0] <= previous[1]) or (a < b and previous[0] >= previous[1]))
    return st.cur


def ta_change(st, i, rt, source, length=1):
    if _begin(st, i):
        st.vals.append(source)
        n = int(length) if not isna(length) else 1
        if len(st.vals) <= n:
            st.cur = False if isinstance(source, bool) else nan
        else:
            before = st.vals[-1 - n]
            if isinstance(source, bool):
                st.cur = source != before
            else:
                st.cur = nan if isna(source) or isna(before) else source - before
    return st.cur


def ta_mom(st, i, rt, source, length):
    return ta_change(st, i, rt, source, length)


def ta_roc(st, i, rt, source, length):
    if _begin(st, i):
        st.vals.append(source)
        n = int(length)
        before = st.vals[-1 - n] if len(st.vals) > n else nan
        st.cur = div(100.0 * (source - before), before) if not isna(before) else nan
    return st.cur


def _pivot(st, i, rt, args, highest: bool):
    if len(args) == 2:
        source = (rt.H if highest else rt.L)[i]
        left, right = args
    else:
        source, left, right = args[0], args[1], args[2]
    st.vals.append(source)
    left = int(left)
    right = int(right)
    if len(st.vals) < left + right + 1:
        return nan
    centre = st.vals[-1 - right]
    if isna(centre):
        return nan
    before = st.vals[-1 - right - left:-1 - right] if left else []
    after = st.vals[-right:] if right else []
    # An older bar may tie. A newer bar that ties takes the pivot instead.
    for value in before:
        if isna(value):
            continue
        if (value > centre) if highest else (value < centre):
            return nan
    for value in after:
        if isna(value):
            continue
        if (value >= centre) if highest else (value <= centre):
            return nan
    return centre


def ta_pivothigh(st, i, rt, *args):
    if _begin(st, i):
        st.cur = _pivot(st, i, rt, args, True)
    return st.cur


def ta_pivotlow(st, i, rt, *args):
    if _begin(st, i):
        st.cur = _pivot(st, i, rt, args, False)
    return st.cur


def ta_valuewhen(st, i, rt, condition, source, occurrence=0):
    if _begin(st, i):
        if t(condition):
            st.vals.append(source)
        n = int(occurrence)
        st.cur = st.vals[-1 - n] if len(st.vals) > n else nan
    return st.cur


def ta_barssince(st, i, rt, condition):
    if _begin(st, i):
        if t(condition):
            st.last = i
        st.cur = i - st.last if st.last is not None else nan
    return st.cur


def ta_cum(st, i, rt, source):
    if _begin(st, i):
        st.count = (st.count or 0) + (0 if isna(source) else source)
        st.cur = st.count
    return st.cur


def ta_sum(st, i, rt, source, length):
    if _begin(st, i):
        window = _window(st, source, length)
        st.cur = nan if window is None or any(isna(v) for v in window) else sum(window)
    return st.cur


def ta_rising(st, i, rt, source, length):
    if _begin(st, i):
        st.vals.append(source)
        n = int(length)
        if len(st.vals) <= n:
            st.cur = False
        else:
            st.cur = all(source > value for value in st.vals[-1 - n:-1])
    return st.cur


def ta_falling(st, i, rt, source, length):
    if _begin(st, i):
        st.vals.append(source)
        n = int(length)
        if len(st.vals) <= n:
            st.cur = False
        else:
            st.cur = all(source < value for value in st.vals[-1 - n:-1])
    return st.cur


def ta_vwap(st, i, rt, source=None, anchor=None, stdev_mult=None):
    if _begin(st, i):
        price = (rt.H[i] + rt.L[i] + rt.C[i]) / 3.0 if source is None else source
        if st.extra is None or rt.new_session(i):
            st.extra = [0.0, 0.0]
        volume = rt.V[i]
        if not isna(price):
            st.extra[0] += price * volume
            st.extra[1] += volume
        st.cur = st.extra[0] / st.extra[1] if st.extra[1] else nan
    return st.cur


class _Macd:
    """The three MACD series, so a script can read .signal and .hist or unpack the result."""

    __slots__ = ("macd", "signal", "hist")

    def __init__(self, line, signal, hist):
        self.macd = line
        self.signal = signal
        self.hist = hist

    def __iter__(self):
        yield self.macd
        yield self.signal
        yield self.hist

    def __getitem__(self, index):
        return (self.macd, self.signal, self.hist)[index]


def ta_macd(st, i, rt, source, fast, slow, signal):
    if _begin(st, i):
        if st.extra is None:
            st.extra = (_Average(), _Average(), _Average())
        quick = st.extra[0].update(source, fast, False)
        long_ = st.extra[1].update(source, slow, False)
        line = nan if isna(quick) or isna(long_) else quick - long_
        smooth = st.extra[2].update(line, signal, False)
        hist = nan if isna(line) or isna(smooth) else line - smooth
        st.cur = _Macd(line, smooth, hist)
    return st.cur


def ta_bb(st, i, rt, source, length, mult):
    if _begin(st, i):
        window = _window(st, source, length)
        if window is None or any(isna(v) for v in window):
            st.cur = (nan, nan, nan)
        else:
            n = len(window)
            mean = sum(window) / n
            dev = math.sqrt(sum((value - mean) ** 2 for value in window) / n)
            st.cur = (mean, mean + mult * dev, mean - mult * dev)
    return st.cur


def ta_stoch(st, i, rt, source, high, low, length):
    if _begin(st, i):
        if st.extra is None:
            st.extra = ([], [])
        st.extra[0].append(high)
        st.extra[1].append(low)
        n = _length(length)
        if n is None or len(st.extra[0]) < n:
            st.cur = nan
        else:
            top = max(st.extra[0][-n:])
            bottom = min(st.extra[1][-n:])
            st.cur = div(100.0 * (source - bottom), top - bottom)
    return st.cur


def ta_fixnan(st, i, rt, source):
    if _begin(st, i):
        if not isna(source):
            st.last = source
        st.cur = st.last if st.last is not None else nan
    return st.cur


TA = {
    "sma": ta_sma, "ema": ta_ema, "rma": ta_rma, "wma": ta_wma, "vwma": ta_vwma, "atr": ta_atr,
    "tr": ta_tr, "rsi": ta_rsi, "stdev": ta_stdev, "highest": ta_highest, "lowest": ta_lowest,
    "highestbars": ta_highestbars, "lowestbars": ta_lowestbars, "crossover": ta_crossover,
    "crossunder": ta_crossunder, "cross": ta_cross, "change": ta_change, "mom": ta_mom, "roc": ta_roc,
    "pivothigh": ta_pivothigh, "pivotlow": ta_pivotlow, "valuewhen": ta_valuewhen,
    "barssince": ta_barssince, "cum": ta_cum, "rising": ta_rising, "falling": ta_falling,
    "vwap": ta_vwap, "macd": ta_macd, "bb": ta_bb, "stoch": ta_stoch,
}
BOOL_TA = {"crossover", "crossunder", "cross", "rising", "falling"}


# ---------- math.* ----------

def m_abs(x):
    return nan if isna(x) else abs(x)


def m_max(*args):
    if any(isna(v) for v in args):
        return nan
    return max(args)


def m_min(*args):
    if any(isna(v) for v in args):
        return nan
    return min(args)


def m_avg(*args):
    if any(isna(v) for v in args):
        return nan
    return sum(args) / len(args)


def m_floor(x):
    return nan if isna(x) else math.floor(x)


def m_ceil(x):
    return nan if isna(x) else math.ceil(x)


def m_round(x, precision=None):
    if isna(x):
        return nan
    if precision is None or isna(precision):
        return int(math.floor(x + 0.5))
    scale = 10 ** int(precision)
    return math.floor(x * scale + 0.5) / scale


def m_sqrt(x):
    return nan if isna(x) or x < 0 else math.sqrt(x)


def m_pow(x, y):
    if isna(x) or isna(y):
        return nan
    try:
        return math.pow(x, y)
    except (OverflowError, ValueError):
        return nan


def m_log(x):
    return nan if isna(x) or x <= 0 else math.log(x)


def m_log10(x):
    return nan if isna(x) or x <= 0 else math.log10(x)


def m_exp(x):
    if isna(x):
        return nan
    try:
        return math.exp(x)
    except OverflowError:
        return nan


def m_sign(x):
    if isna(x):
        return nan
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


def _trig(fn):
    def call(x):
        if isna(x):
            return nan
        try:
            return fn(x)
        except ValueError:
            return nan
    return call


MATH = {
    "abs": m_abs, "max": m_max, "min": m_min, "avg": m_avg, "floor": m_floor, "ceil": m_ceil,
    "round": m_round, "sqrt": m_sqrt, "pow": m_pow, "log": m_log, "log10": m_log10, "exp": m_exp,
    "sign": m_sign, "atan": _trig(math.atan), "sin": _trig(math.sin), "cos": _trig(math.cos),
    "tan": _trig(math.tan), "asin": _trig(math.asin), "acos": _trig(math.acos),
    "todegrees": _trig(math.degrees), "toradians": _trig(math.radians),
}
MATH_CONST = {"pi": math.pi, "e": math.e, "phi": (1 + 5 ** 0.5) / 2, "rphi": 2 / (1 + 5 ** 0.5)}


# ---------- array.* ----------

class PineArray(list):
    __slots__ = ()


def _index(a, index):
    if a is None:
        raise PineError("The array is na.")
    k = int(index)
    if k < 0:
        k += len(a)
    if not 0 <= k < len(a):
        raise PineError(f"Index {int(index)} is out of bounds. Array size is {len(a)}.")
    return k


def a_new(size=0, initial_value=nan):
    count = 0 if isna(size) else int(size)
    return PineArray([initial_value] * count)


def a_from(*values):
    return PineArray(values)


def a_get(a, index):
    return a[_index(a, index)]


def a_set(a, index, value):
    a[_index(a, index)] = value


def a_push(a, value):
    a.append(value)


def a_pop(a):
    if not a:
        raise PineError("Cannot pop from an empty array.")
    return a.pop()


def a_shift(a):
    if not a:
        raise PineError("Cannot shift an empty array.")
    return a.pop(0)


def a_unshift(a, value):
    a.insert(0, value)


def a_insert(a, index, value):
    a.insert(int(index), value)


def a_remove(a, index):
    return a.pop(_index(a, index))


def a_clear(a):
    a.clear()


def a_size(a):
    return len(a) if a is not None else nan


def a_first(a):
    return a_get(a, 0)


def a_last(a):
    return a_get(a, -1)


def a_sum(a):
    values = [v for v in a if not isna(v)]
    return sum(values) if values else nan


def a_avg(a):
    values = [v for v in a if not isna(v)]
    return sum(values) / len(values) if values else nan


def a_max(a, nth=0):
    values = sorted((v for v in a if not isna(v)), reverse=True)
    return values[int(nth)] if len(values) > int(nth) else nan


def a_min(a, nth=0):
    values = sorted(v for v in a if not isna(v))
    return values[int(nth)] if len(values) > int(nth) else nan


def a_indexof(a, value):
    try:
        return a.index(value)
    except ValueError:
        return -1


def a_lastindexof(a, value):
    for k in range(len(a) - 1, -1, -1):
        if a[k] == value:
            return k
    return -1


def a_includes(a, value):
    return value in a


def a_copy(a):
    return PineArray(a)


def a_slice(a, start, end):
    return PineArray(a[int(start):int(end)])


def a_reverse(a):
    a.reverse()


def a_sort(a, order="ascending"):
    a.sort(reverse=str(order).endswith("descending"))


def a_concat(a, b):
    a.extend(b)
    return a


def a_fill(a, value, index_from=0, index_to=None):
    end = len(a) if index_to is None or isna(index_to) else int(index_to)
    for k in range(int(index_from), end):
        a[k] = value


def a_join(a, separator=""):
    return str(separator).join(str(v) for v in a)


def a_stdev(a, biased=True):
    values = [v for v in a if not isna(v)]
    if not values:
        return nan
    mean = sum(values) / len(values)
    spread = sum((v - mean) ** 2 for v in values)
    n = len(values)
    return math.sqrt(spread / n if t(biased) or n < 2 else spread / (n - 1))


def a_median(a):
    values = sorted(v for v in a if not isna(v))
    if not values:
        return nan
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def a_percentile_linear_interpolation(a, percentage):
    values = sorted(v for v in a if not isna(v))
    if not values or isna(percentage):
        return nan
    rank = (len(values) - 1) * float(percentage) / 100.0
    low = int(math.floor(rank))
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (rank - low)


def a_percentile_nearest_rank(a, percentage):
    values = sorted(v for v in a if not isna(v))
    if not values or isna(percentage):
        return nan
    rank = max(int(math.ceil(float(percentage) / 100.0 * len(values))), 1)
    return values[min(rank, len(values)) - 1]


def a_percentrank(a, index):
    k = _index(a, index)
    value = a[k]
    others = [v for j, v in enumerate(a) if j != k and not isna(v)]
    return 100.0 * sum(1 for v in others if v <= value) / len(others) if others else nan


ARRAY = {
    "percentile_linear_interpolation": a_percentile_linear_interpolation,
    "percentile_nearest_rank": a_percentile_nearest_rank, "percentrank": a_percentrank,
    "new": a_new, "new_float": a_new, "new_int": a_new, "new_bool": a_new, "new_string": a_new,
    "new_color": a_new, "new_label": a_new, "new_line": a_new, "new_box": a_new, "new_table": a_new,
    "new_linefill": a_new, "from": a_from, "get": a_get, "set": a_set, "push": a_push, "pop": a_pop,
    "shift": a_shift, "unshift": a_unshift, "insert": a_insert, "remove": a_remove, "clear": a_clear,
    "size": a_size, "first": a_first, "last": a_last, "sum": a_sum, "avg": a_avg, "max": a_max,
    "min": a_min, "indexof": a_indexof, "lastindexof": a_lastindexof, "includes": a_includes,
    "copy": a_copy, "slice": a_slice, "reverse": a_reverse, "sort": a_sort, "concat": a_concat,
    "fill": a_fill, "join": a_join, "stdev": a_stdev, "median": a_median,
}


# ---------- str.* ----------

def s_tostring(value, fmt=None):
    if isna(value):
        return "NaN"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and fmt and isinstance(fmt, str) and "#" in fmt:
        places = len(fmt.split(".", 1)[1]) if "." in fmt else 0
        text = f"{value:.{places}f}"
        return text.rstrip("0").rstrip(".") if places and "0" not in fmt.split(".", 1)[1] else text
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def s_format(fmt, *args):
    text = str(fmt)
    for k, value in enumerate(args):
        text = text.replace("{" + str(k) + "}", s_tostring(value))
    return text


STR = {
    "tostring": s_tostring, "format": s_format, "length": lambda s: len(str(s)),
    "contains": lambda s, part: str(part) in str(s), "startswith": lambda s, part: str(s).startswith(str(part)),
    "endswith": lambda s, part: str(s).endswith(str(part)), "upper": lambda s: str(s).upper(),
    "lower": lambda s: str(s).lower(), "replace_all": lambda s, a, b: str(s).replace(str(a), str(b)),
    "substring": lambda s, a, b=None: str(s)[int(a):(None if b is None else int(b))],
    "split": lambda s, sep: PineArray(str(s).split(str(sep))),
    "tonumber": lambda s: _number(s), "pos": lambda s, part: str(s).find(str(part)),
}


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return nan


# ---------- drawings that a script can read back ----------

class Box:
    __slots__ = ("left", "top", "right", "bottom", "text", "deleted")

    def __init__(self, left=nan, top=nan, right=nan, bottom=nan, *args, **kwargs) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom
        self.text = kwargs.get("text", "")
        self.deleted = False


def box_new(*args, **kwargs):
    names = ("left", "top", "right", "bottom")
    values = {name: kwargs.get(name, args[k] if k < len(args) else nan) for k, name in enumerate(names)}
    return Box(values["left"], values["top"], values["right"], values["bottom"], text=kwargs.get("text", ""))


def _box_get(field):
    def get(b):
        return nan if not isinstance(b, Box) else getattr(b, field)
    return get


def _box_set(field):
    def put(b, value):
        if isinstance(b, Box):
            setattr(b, field, value)
    return put


def box_set_lefttop(b, left, top):
    if isinstance(b, Box):
        b.left, b.top = left, top


def box_set_rightbottom(b, right, bottom):
    if isinstance(b, Box):
        b.right, b.bottom = right, bottom


def box_delete(b):
    if isinstance(b, Box):
        b.deleted = True


def _ignore(*args, **kwargs):
    return None


BOX = {
    "new": box_new, "get_left": _box_get("left"), "get_top": _box_get("top"), "get_right": _box_get("right"),
    "get_bottom": _box_get("bottom"), "set_left": _box_set("left"), "set_top": _box_set("top"),
    "set_right": _box_set("right"), "set_bottom": _box_set("bottom"), "set_text": _box_set("text"),
    "set_lefttop": box_set_lefttop, "set_rightbottom": box_set_rightbottom, "delete": box_delete,
    "copy": lambda b: _copy.copy(b),
}


class Label:
    __slots__ = ("x", "y", "text")

    def __init__(self) -> None:
        self.x = nan
        self.y = nan
        self.text = ""


class Line:
    __slots__ = ("x1", "y1", "x2", "y2")

    def __init__(self, x1=nan, y1=nan, x2=nan, y2=nan, *args, **kwargs) -> None:
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2


def line_new(*args, **kwargs):
    names = ("x1", "y1", "x2", "y2")
    values = [kwargs.get(name, args[k] if k < len(args) else nan) for k, name in enumerate(names)]
    return Line(*values)


def line_get_price(ln, x):
    if ln is None or isna(ln.x1) or isna(ln.x2) or ln.x2 == ln.x1:
        return nan if ln is None else ln.y1
    return ln.y1 + (ln.y2 - ln.y1) * (x - ln.x1) / (ln.x2 - ln.x1)


def _line_set(*fields):
    def put(ln, *values):
        if ln is not None:
            for field, value in zip(fields, values):
                setattr(ln, field, value)
    return put


LINE = {
    "new": line_new, "get_price": line_get_price, "get_x1": lambda ln: ln.x1, "get_y1": lambda ln: ln.y1,
    "get_x2": lambda ln: ln.x2, "get_y2": lambda ln: ln.y2, "set_x1": _line_set("x1"),
    "set_y1": _line_set("y1"), "set_x2": _line_set("x2"), "set_y2": _line_set("y2"),
    "set_xy1": _line_set("x1", "y1"), "set_xy2": _line_set("x2", "y2"), "delete": _ignore,
}


def method(obj, name, *args):
    """obj.name(...) for the built-in types a script can call methods on."""
    if isinstance(obj, PineArray):
        fn = ARRAY.get(name)
    elif isinstance(obj, Box):
        fn = BOX.get(name)
    elif isinstance(obj, Line):
        fn = LINE.get(name)
    elif isinstance(obj, Label) or obj is None:
        return None
    elif isinstance(obj, str):
        fn = STR.get(name)
    elif name == "copy":
        return _copy.copy(obj)
    else:
        fn = None
    if fn is None:
        raise PineError(f"The method {name} is not available here.")
    return fn(obj, *args)


# ---------- time ----------

_ZONES: dict[str, ZoneInfo] = {}
_STAMPS: dict[tuple, datetime] = {}


def local_time(ms, tz: str) -> datetime:
    key = (ms, tz)
    found = _STAMPS.get(key)
    if found is None:
        zone = _ZONES.get(tz)
        if zone is None:
            zone = _ZONES[tz] = ZoneInfo(_zone_name(tz))
        found = datetime.fromtimestamp(ms / 1000.0, timezone.utc).astimezone(zone)
        if len(_STAMPS) > 400000:
            _STAMPS.clear()
        _STAMPS[key] = found
    return found


def _zone_name(tz: str) -> str:
    text = str(tz or "UTC").strip()
    if text.upper().startswith(("GMT", "UTC")) and len(text) > 3:
        sign = 1 if "+" in text else -1
        hours = text[4:].split(":")[0] or "0"
        return f"Etc/GMT{'-' if sign > 0 else '+'}{int(hours)}"
    return text


def time_part(name: str, ms, tz: str):
    if isna(ms):
        return nan
    when = local_time(ms, tz)
    if name == "hour":
        return when.hour
    if name == "minute":
        return when.minute
    if name == "second":
        return when.second
    if name == "dayofmonth":
        return when.day
    if name == "month":
        return when.month
    if name == "year":
        return when.year
    if name == "dayofweek":
        return (when.weekday() + 1) % 7 + 1
    if name == "weekofyear":
        return when.isocalendar()[1]
    raise PineError(f"Unknown time part {name}.")
