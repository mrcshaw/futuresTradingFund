"""Desk compiler fixes found while converting the Flux Charts S&R Logistic Regression indicator."""

import math
import unittest

from futuresfund.backtrader_engine import Bar
from futuresfund.pine_runtime import run_pine, supported

HEADER = (
    '//@version=6\n'
    'strategy("SR fixes", overlay=true, initial_capital=25000, default_qty_type=strategy.fixed, default_qty_value=1,\n'
    '  process_orders_on_close=true)\n'
)


def _bars(closes, start=1_788_307_200, width=120):
    bars = []
    for k, close in enumerate(closes):
        open_ = closes[k - 1] if k else close
        bars.append(Bar(start + k * width, open_, max(open_, close) + 0.5, min(open_, close) - 0.5, close, 10))
    return bars


class SRFixTests(unittest.TestCase):
    def test_a_function_body_may_start_with_a_minus_sign(self):
        body = (
            'loss(y, p) =>\n'
            '    -y * math.log(p) - (1 - y) * math.log(1 - p)\n'
            'v = loss(1.0, 0.5)\n'
            'if bar_index == 2 and math.abs(v - math.log(2)) < 0.000001\n'
            '    strategy.entry("L", strategy.long)\n'
        )
        self.assertTrue(supported(HEADER + body))
        trades = run_pine(HEADER + body + 'if bar_index == 4\n    strategy.close("L")\n', _bars([100, 101, 102, 103, 104, 105]), {})
        self.assertEqual(len(trades), 1)

    def test_a_local_may_share_its_name_with_the_function_it_calls(self):
        body = (
            'loss(y, p) =>\n'
            '    -y * math.log(p)\n'
            'f(x) =>\n'
            '    loss = loss(x, 0.5)\n'
            '    loss * 2\n'
            'v = f(1.0)\n'
            'if bar_index == 2 and math.abs(v - 2 * math.log(2)) < 0.000001\n'
            '    strategy.entry("L", strategy.long)\n'
            'if bar_index == 4\n'
            '    strategy.close("L")\n'
        )
        self.assertTrue(supported(HEADER + body))
        self.assertEqual(len(run_pine(HEADER + body, _bars([100, 101, 102, 103, 104, 105]), {})), 1)
        self.assertTrue(math.isclose(2 * math.log(2), 1.3862943611198906))


if __name__ == "__main__":
    unittest.main()
