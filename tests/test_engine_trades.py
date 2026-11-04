"""The trading engine against TradingView's Strategy Tester lists.

Each file in the trades folder is one chart: a symbol and a timeframe, for the
script named in the file. The engine runs that script on the saved bars. NQ
uses a point value of 20. A listed trade is checked when that minute is in
the saved bars. Every checked entry, exit, and price has to be the one
TradingView recorded.

A year of 2-minute bars takes PineTS longer than its ten-minute limit, so each
list is checked on its last trades. The saved bars start WARM_DAYS before the
last CHECKED_TRADES trades they reach, and check_export leaves the first ten
days out while the indicators warm up. A list whose script is no longer in the
strategy folders is skipped, with the reason.
The bars end at the last TradingView-exported bar: after it, the Databento
front month is a different chart.
"""

import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from futuresfund import research_bars
from futuresfund.backtrader_engine import load_tv_trades
from futuresfund.tv_checks import check_export, trade_lists

ET = ZoneInfo("America/New_York")
CHECKED_TRADES = 5
WARM_DAYS = 11  # check_export does not compare the first ten days of bars that start inside the list


class EngineTradeListTests(unittest.TestCase):
    def test_every_saved_list_matches_the_engine(self):
        paths = trade_lists()
        self.assertGreaterEqual(len(paths), 1)
        seen = set()
        for path in paths:
            with self.subTest(file=path.name), mock.patch.object(research_bars, "research_series", _recent_bars(path)):
                report = check_export(path)
                if str(report.get("error") or "").startswith("No Pine script in the strategy folders"):
                    self.skipTest(f"The script {path.name} was exported from is no longer in the strategy folders.")
                self.assertNotIn("error", report, report.get("error"))
                seen.add((report["instrument"], report["timeframe"]))
                self.assertGreater(report["covered"], 0, _gap(report))
                self.assertEqual(report["missed"], [], _gap(report))
                self.assertEqual(report["extras"], [], _gap(report))
                self.assertEqual(report["engine_covered_profit"], report["covered_profit"], _gap(report))
        self.assertEqual(seen, {("ES", "2m"), ("ES", "5m"), ("ES", "15m"), ("NQ", "2m"), ("NQ", "5m"), ("NQ", "15m"), ("QO", "2m"), ("QO", "5m"), ("QO", "15m")})


def _recent_bars(path):
    """The research bars, from WARM_DAYS before the last CHECKED_TRADES trades of this list that they reach."""
    real = research_bars.research_series
    trades = sorted(load_tv_trades(path), key=lambda trade: trade["entry_time"])

    def series(root, timeframe, **kwargs):
        rows = real(root, timeframe, **kwargs)
        # The Strategy Tester ran on TradingView's chart. After the last exported bar, the front-month
        # Databento bars differ in price and volume, so a trade there checks the data, not the engine.
        # The October update continues the chart past the exports these lists were checked on.
        exported = [row["t"] for row in rows if row.get("source") == "tradingview" and row["t"] < _epoch("2026-10-02 00:00")]
        if exported:
            rows = [row for row in rows if row["t"] <= exported[-1]]
        if not rows:
            return rows
        reached = [trade for trade in trades if _epoch(trade["exit_time"]) <= rows[-1]["t"]]
        if not reached:
            return rows
        start = _epoch(reached[-CHECKED_TRADES:][0]["entry_time"]) - WARM_DAYS * 86400
        return [row for row in rows if row["t"] >= start]

    return series


def _epoch(stamp: str) -> int:
    return int(datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp())


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
