"""ta.supertrend, ta.linreg, ta.hma, ta.percentile_nearest_rank and array.sort_indices in the desk's Pine runtime."""

import math
import unittest
from types import SimpleNamespace

from futuresfund import pine_lib as L
from futuresfund.pine_runtime import PineUnsupported, compile_pine, supported


def _rt(highs, lows, closes):
    return SimpleNamespace(H=list(highs), L=list(lows), C=list(closes))


def _series(fn, values, *args, rt=None):
    site = L.Site()
    rt = rt or _rt(values, values, values)
    return [fn(site, i, rt, value, *args) for i, value in enumerate(values)]


class AdditionTests(unittest.TestCase):
    def test_linreg_on_a_straight_line_is_the_line(self):
        values = [10.0 + 2.0 * k for k in range(30)]
        out = _series(L.ta_linreg, values, 10, 0)
        self.assertTrue(math.isnan(out[8]))
        self.assertAlmostEqual(out[20], values[20])
        back = _series(L.ta_linreg, values, 10, 1)
        self.assertAlmostEqual(back[20], values[19])

    def test_linreg_over_one_bar_is_na(self):
        values = [10.0 + k for k in range(5)]
        out = _series(L.ta_linreg, values, 1, 0)
        self.assertTrue(all(math.isnan(v) for v in out))

    def test_hma_is_the_wma_of_the_wma_difference(self):
        values = [100 + math.sin(k / 3.0) * 5 + k * 0.1 for k in range(80)]
        hma = _series(L.ta_hma, values, 16)
        half = _series(L.ta_wma, values, 8)
        full = _series(L.ta_wma, values, 16)
        diff = [2 * a - b if not (math.isnan(a) or math.isnan(b)) else math.nan for a, b in zip(half, full)]
        expected = _series(L.ta_wma, diff, 4)
        self.assertAlmostEqual(hma[-1], expected[-1])
        self.assertTrue(math.isnan(hma[10]))

    def test_percentile_nearest_rank(self):
        values = [float(v) for v in [5, 1, 4, 2, 3]]
        out = _series(L.ta_percentile_nearest_rank, values, 5, 40)
        self.assertEqual(out[-1], 2.0)
        self.assertTrue(math.isnan(out[3]))

    def test_sort_indices(self):
        a = L.PineArray([3.0, 1.0, 2.0])
        self.assertEqual(list(L.a_sort_indices(a, "order.ascending")), [1, 2, 0])
        self.assertEqual(list(L.a_sort_indices(a, "order.descending")), [0, 2, 1])

    def test_sort_indices_ties_and_na_follow_pinets(self):
        # Ascending and stable with na last, then the whole list reversed for descending.
        a = L.PineArray([2.0, math.nan, 1.0, 2.0])
        self.assertEqual(list(L.a_sort_indices(a, "order.ascending")), [2, 0, 3, 1])
        self.assertEqual(list(L.a_sort_indices(a, "order.descending")), [1, 3, 0, 2])
        # A string array puts na first.
        s = L.PineArray(["b", math.nan, "a"])
        self.assertEqual(list(L.a_sort_indices(s, "order.ascending")), [1, 2, 0])
        self.assertEqual(list(L.a_sort_indices(s, "order.descending")), [0, 2, 1])

    def test_sort_with_na_follows_pinets(self):
        a = L.PineArray([3.0, math.nan, 1.0, 2.0])
        L.a_sort(a, "order.ascending")
        self.assertEqual(a[:3], [1.0, 2.0, 3.0])
        self.assertTrue(math.isnan(a[3]))
        b = L.PineArray([3.0, math.nan, 1.0, 2.0])
        L.a_sort(b, "order.descending")
        self.assertTrue(math.isnan(b[0]))
        self.assertEqual(b[1:], [3.0, 2.0, 1.0])
        c = L.PineArray([3, 1, 2])
        L.a_sort(c)
        self.assertEqual(c, [1, 2, 3])

    def test_supertrend_turns_down_after_a_rally_breaks(self):
        closes = [100 + k for k in range(30)] + [129 - 3 * k for k in range(1, 20)]
        rt = _rt([c + 1 for c in closes], [c - 1 for c in closes], closes)
        site = L.Site()
        out = [L.ta_supertrend(site, i, rt, 2.0, 5) for i in range(len(closes))]
        self.assertEqual(out[0][1], 1)
        self.assertEqual(out[25][1], -1)
        self.assertLess(out[25][0], closes[25])
        self.assertEqual(out[-1][1], 1)
        self.assertGreater(out[-1][0], closes[-1])

    def test_a_script_using_them_compiles(self):
        source = (
            '//@version=5\nstrategy("New calls", overlay=true)\n'
            '[st, dir] = ta.supertrend(2.0, 10)\n'
            'lr = ta.linreg(close, 20, 0)\nhm = ta.hma(close, 20)\npr = ta.percentile_nearest_rank(close, 50, 25)\n'
            'var float[] d = array.new_float(0)\narray.push(d, close)\nint[] idx = array.sort_indices(d, order.ascending)\n'
            'if ta.crossunder(dir, 0) and close > lr\n    strategy.entry("L", strategy.long)\n'
        )
        self.assertTrue(supported(source))

    def test_calling_a_value_says_it_is_not_a_function(self):
        source = '//@version=5\nstrategy("Call a value")\nx = 1\ny = x(2)\n'
        with self.assertRaises(PineUnsupported) as caught:
            compile_pine(source)
        self.assertIn("x is not a function", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
