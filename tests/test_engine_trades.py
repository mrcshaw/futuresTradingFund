"""The trading engine against TradingView's Strategy Tester lists.

Each file in the trades folder is one chart: a symbol and a timeframe, for the
script named in the file. The engine runs that script on the saved bars. NQ
uses a point value of 20. A listed trade is checked when that minute is in
the saved bars. Every checked entry, exit, and price has to be the one
TradingView recorded.
"""

import unittest

from futuresfund.tv_checks import check_export, trade_lists


class EngineTradeListTests(unittest.TestCase):
    def test_every_saved_list_matches_the_engine(self):
        paths = trade_lists()
        self.assertGreaterEqual(len(paths), 1)
        seen = set()
        for path in paths:
            with self.subTest(file=path.name):
                report = check_export(path)
                self.assertNotIn("error", report, report.get("error"))
                seen.add((report["instrument"], report["timeframe"]))
                self.assertEqual(report["missed"], [], _gap(report))
                self.assertEqual(report["extras"], [], _gap(report))
                self.assertEqual(report["engine_covered_profit"], report["covered_profit"], _gap(report))
        self.assertEqual(seen, {("ES", "2m"), ("ES", "5m"), ("ES", "15m"), ("NQ", "2m"), ("NQ", "5m"), ("NQ", "15m"), ("QO", "2m"), ("QO", "5m"), ("QO", "15m")})


def _gap(report: dict) -> str:
    missed = report.get("missed") or []
    extras = report.get("extras") or []
    first = missed[0] if missed else (extras[0] if extras else {})
    which = "missed" if missed else "extra"
    return (
        f"{report.get('instrument')} {report.get('timeframe')}: "
        f"{report.get('matched')} of {report.get('covered')} on the saved bars, "
        f"{len(missed)} missed, {len(extras)} extra, "
        f"{len(report.get('uncovered') or [])} before those bars. "
        f"TradingView ${report.get('covered_profit')} engine ${report.get('engine_covered_profit')}. "
        f"First {which} {first.get('entry_time')} {first.get('side')} {first.get('entry_price')} "
        f"exit {first.get('exit_time')} {first.get('exit_signal')} {first.get('exit_price')}."
    )
