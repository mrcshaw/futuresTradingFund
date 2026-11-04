"""Micro S&P is its own contract at five dollars a point."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MesTests(unittest.TestCase):
    def test_mes_is_five_dollars_a_point_and_not_es(self):
        from futuresfund.contracts import POINT_VALUE
        from futuresfund.learn import _with_contract_point
        from futuresfund.library import _RESEARCH_ROOT, contract_of, for_contract
        from futuresfund.risk_style import point_value

        self.assertEqual(POINT_VALUE["MES"], 5)
        self.assertEqual(point_value("MES"), 5.0)
        self.assertEqual(_with_contract_point({"point_value": 50, "length": 10}, "MES")["point_value"], 5.0)
        self.assertEqual(contract_of("Night breakout", "mes_breakout.pine"), ("MES1!", "Micro S&P"))
        self.assertEqual(contract_of("Night breakout", "es_breakout.pine"), ("ES1!", "ES"))
        self.assertEqual(contract_of("Desk", "desk.pine", "MES1!"), ("MES1!", "Micro S&P"))
        self.assertEqual(contract_of("Desk", "desk.pine", "ES1!"), ("ES1!", "ES"))
        # A library row filed on MES is measured again on MES, not on ES.
        self.assertEqual(_RESEARCH_ROOT["MES1!"], "MES")
        self.assertIn('pv = input.float(5,', for_contract('pv = input.float(50, "Point Value ($)")', "MES1!"))

    def test_mes_is_researched_after_micro_nasdaq(self):
        from unittest import mock

        from futuresfund.learn import research_instruments

        with mock.patch("futuresfund.charts.chart_roots", return_value=["ES", "GC", "M2K", "MES", "MNQ", "NQ", "QO", "YM"]):
            self.assertEqual(research_instruments(), ["ES", "GC", "NQ", "MNQ", "MES", "M2K", "YM"])

    def test_the_library_has_a_micro_sp_shelf(self):
        from futuresfund.library import _shelves

        contracts = [shelf["contract"] for shelf in _shelves()]
        self.assertEqual(contracts[:7], ["ES1!", "GC1!", "NQ1!", "MNQ1!", "MES1!", "M2K1!", "YM1!"])

    def test_tradingview_names_and_downloads_know_mes(self):
        from futuresfund.pine_runtime import _TICKER
        from futuresfund.tv_checks import _ROOTS

        self.assertEqual(_TICKER["MES"], "CME_MINI:MES1!")
        self.assertIn(("MES1!", "MES"), _ROOTS)
        source = ROOT.joinpath("futuresfund", "historical_bars.py").read_text(encoding="utf-8")
        self.assertIn('"MES", "M2K", "YM"}', source)

    def test_the_prompts_state_the_mes_point_value(self):
        team = ROOT.joinpath("futuresfund", "strategy_team.py").read_text(encoding="utf-8")
        self.assertIn("On MES it is 5.", team)
        self.assertIn("On MES, M2K, and YM that is at most 150 points.", team)
        reason = ROOT.joinpath("futuresfund", "attempt_reason.py").read_text(encoding="utf-8")
        self.assertEqual(reason.count("MES, M2K, and YM are $5 a point."), 2)

    @unittest.skipUnless(ROOT.joinpath("chartData", "mes").is_dir(), "the MES download is not on disk")
    def test_the_mes_download_is_one_front_month_series(self):
        from futuresfund.historical_bars import load_minutes, outright_files

        self.assertTrue(outright_files("MES"))
        minutes = load_minutes("MES")
        self.assertGreater(len(minutes), 300_000)


if __name__ == "__main__":
    unittest.main()
