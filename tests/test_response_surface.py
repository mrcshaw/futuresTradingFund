"""The quadratic solver finds a known peak, and refuses a saddle."""

import unittest


class ResponseSurfaceTests(unittest.TestCase):
    def test_a_known_peak_is_recovered_and_is_worth_a_backtest(self):
        from futuresfund.response_surface import fit_response, research_use

        rows = []
        for left in range(5):
            for middle in range(5):
                for right in range(5):
                    height = 10.0 - (left - 2) ** 2 - (middle - 1) ** 2 - (right - 3) ** 2
                    rows.append({"a": left, "b": middle, "c": right, "net_profit": height, "max_drawdown": 0, "trades": 1})
        fit = fit_response(
            rows,
            ["a", "b", "c"],
            score=lambda row: row["net_profit"],
            use_log=False,
        )
        self.assertEqual(fit.kind, "maximum")
        self.assertTrue(fit.inside_tried_range)
        self.assertAlmostEqual(fit.critical_inputs["a"], 2.0, places=2)
        self.assertAlmostEqual(fit.critical_inputs["b"], 1.0, places=2)
        self.assertAlmostEqual(fit.critical_inputs["c"], 3.0, places=2)
        self.assertGreater(fit.r2, 0.999)
        self.assertAlmostEqual(fit.predict({"a": 2, "b": 1, "c": 3}), 10.0, places=2)
        self.assertIn("worth one backtest", research_use(fit, best_measured=9.0))

    def test_a_saddle_is_not_sent_to_a_backtest(self):
        from futuresfund.response_surface import fit_response, research_use

        rows = []
        for left in (-2, -1, 0, 1, 2):
            for right in (-2, -1, 0, 1, 2):
                height = 1.0 + left * right
                rows.append({"stop_dollars": 100 + left, "tp_dollars": 100 + right, "net_profit": height})
        fit = fit_response(
            rows,
            ["stop_dollars", "tp_dollars"],
            score=lambda row: row["net_profit"],
            use_log=False,
        )
        self.assertEqual(fit.kind, "saddle")
        self.assertIn("Do not backtest", research_use(fit, best_measured=max(row["net_profit"] for row in rows)))
