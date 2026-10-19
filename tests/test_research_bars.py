"""Backtests use the TradingView export and the alert candles."""

import unittest
from unittest import mock


class HistoricalFileTests(unittest.TestCase):
    def test_the_continuous_series_rolls_the_session_after_volume_leads(self):
        from datetime import date

        from futuresfund.historical_bars import roll_calendar

        first = date(2025, 11, 24)
        switch = date(2025, 11, 25)
        later = date(2025, 11, 26)
        chosen = roll_calendar({first: "GCZ5", switch: "GCG6", later: "GCG6"})
        self.assertEqual(chosen[first], "GCZ5")
        self.assertEqual(chosen[switch], "GCZ5")
        self.assertEqual(chosen[later], "GCG6")

    def test_an_outright_symbol_names_its_product_and_a_spread_uses_the_first_leg(self):
        from futuresfund.historical_bars import expiry_key, product_of

        self.assertEqual(product_of("ESH2"), "ES")
        self.assertEqual(product_of("GCG6"), "GC")
        self.assertEqual(product_of("NQH2-NQM2"), "NQ")
        self.assertLess(expiry_key("ESH2"), expiry_key("ESM2"))

    def test_a_fixed_point_price_is_scaled_and_a_decimal_price_is_kept(self):
        from futuresfund.historical_bars import _price

        self.assertEqual(_price("4348.250000000"), 4348.25)
        self.assertEqual(_price("4348250000000"), 4348.25)


class ResearchBarTests(unittest.TestCase):
    def test_a_chart_uses_the_tradingview_export_and_not_another_feed(self):
        from futuresfund import research_bars

        export_bar = {1_700_000_000: (5000.0, 5002.0, 4998.0, 5001.25, 40.0)}

        def export(root, timeframe, width):
            if timeframe == "5m":
                return dict(export_bar)
            return {}

        with mock.patch.object(research_bars, "_historical", return_value=None), mock.patch.object(
            research_bars, "_export", side_effect=export
        ), mock.patch.object(research_bars, "_live", return_value={}):
            series = research_bars.research_series("ES", "5m")
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["c"], 5001.25)
        self.assertEqual(series[0]["source"], "tradingview")
        self.assertFalse(hasattr(research_bars, "yahoo_bars"))

    def test_alert_candles_fill_a_bar_the_export_does_not_have(self):
        from futuresfund import research_bars

        start = 1_700_000_100
        live = {start + index * 60: (10.0, 11.0, 9.0, 10.5, 1.0) for index in range(5)}

        def export(root, timeframe, width):
            return {}

        with mock.patch.object(research_bars, "_historical", return_value=None), mock.patch.object(
            research_bars, "_export", side_effect=export
        ), mock.patch.object(research_bars, "_live", return_value=live):
            series = research_bars.research_series("ES", "5m")
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["o"], 10.0)
        self.assertEqual(series[0]["c"], 10.5)
        self.assertEqual(series[0]["source"], "tradingview")
        self.assertEqual(series[0]["v"], 5.0)

    def test_a_tradingview_bar_replaces_the_front_month_bar(self):
        from futuresfund import research_bars

        historical = {1_700_000_000: (1.0, 2.0, 0.5, 1.5, 9.0)}
        exported = {1_700_000_000: (10.0, 11.0, 9.0, 10.5, 4.0)}

        with mock.patch.object(research_bars, "_historical", return_value=historical), mock.patch.object(
            research_bars, "_export", return_value=exported
        ):
            series = research_bars.research_series("NQ", "1m", include_live=False)
        self.assertEqual(series[0]["o"], 10.0)
        self.assertEqual(series[0]["c"], 10.5)
        self.assertEqual(series[0]["source"], "tradingview")

    def test_a_backtest_check_can_ignore_alert_candles(self):
        from futuresfund import research_bars

        start = 1_700_000_100
        live = {start + index * 60: (10.0, 11.0, 9.0, 10.5, 1.0) for index in range(5)}

        with mock.patch.object(research_bars, "_historical", return_value=None), mock.patch.object(
            research_bars, "_export", return_value={}
        ), mock.patch.object(research_bars, "_live", return_value=live):
            series = research_bars.research_series("ES", "5m", include_live=False)
        self.assertEqual(series, [])
