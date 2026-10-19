import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund.acceptance_gaps import measure


LIMITS = {
    "min_profit": 8000.0,
    "profit_factor": 1.05,
    "win_rate": 40.0,
    "min_trades": 80,
    "drawdown_pct": 20.0,
    "use_drawdown": True,
    "use_trades": True,
    "use_profit": True,
    "use_factor": True,
    "use_win_rate": True,
}


def _row(**values):
    row = {
        "net_profit": 9000.0,
        "profit_factor": 2.0,
        "max_drawdown": 1000.0,
        "trades": 100,
        "win_rate": 50.0,
        "days": 260,
    }
    row.update(values)
    return row


class AcceptanceGapTests(unittest.TestCase):
    def test_the_three_most_common_misses_are_listed_against_the_bar(self):
        with tempfile.TemporaryDirectory() as folder:
            notes = Path(folder)
            payload = {
                "trials": [
                    *[_row(net_profit=100.0) for _ in range(6)],
                    *[_row(profit_factor=0.5) for _ in range(4)],
                    *[_row(max_drawdown=20000.0) for _ in range(3)],
                    _row(),
                ]
            }
            (notes / "sample.json").write_text(json.dumps(payload), encoding="utf-8")
            report = measure(notes, LIMITS, 10_000.0)

        self.assertEqual(report["trials"], 14)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(
            report["lines"],
            [
                "Profit. The typical miss made $100 versus acceptance above $8,000. That is $7,900 under the line. 6 of 14 backtests miss it.",
                "Profit factor. The typical miss is 0.5 versus acceptance above 1.05. That is 0.48 times the bar. 4 of 14 backtests miss it.",
                "Drawdown. The typical miss is $20,000 versus acceptance under $10,000 (20 percent of the account). That is 2.00 times the cap. 3 of 14 backtests miss it.",
            ],
        )
        self.assertEqual(
            report["rows"],
            [
                {"reason": "Profit", "typical": "$100", "acceptance": "Above $8,000", "compared": "$7,900 under the line", "misses": "6 of 14"},
                {"reason": "Profit factor", "typical": "0.5", "acceptance": "Above 1.05", "compared": "0.48 times the bar", "misses": "4 of 14"},
                {"reason": "Drawdown", "typical": "$20,000", "acceptance": "Under $10,000 (20 percent of the account)", "compared": "2.00 times the cap", "misses": "3 of 14"},
            ],
        )

    def test_the_research_page_asks_the_portfolio_manager_for_a_table(self):
        root = Path(__file__).resolve().parents[1] / "futuresfund" / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        js = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="rnd-gaps"', html)
        self.assertIn('id="rnd-report"', html)
        self.assertIn("function renderGaps(report)", js)
        self.assertIn("renderGaps(lab.acceptance_report)", js)
        self.assertIn("Compared with the bar", js)
        self.assertIn("app.js?v=148", html)

    def test_asking_the_portfolio_manager_publishes_the_table(self):
        from futuresfund.desk_chat import answer

        report = {
            "ready": True,
            "trials": 14,
            "passed": 1,
            "rows": [{"reason": "Profit", "typical": "$100", "acceptance": "Above $8,000", "compared": "$7,900 under the line", "misses": "6 of 14"}],
            "at": "2026-10-18 20:30",
        }
        with mock.patch("futuresfund.acceptance_gaps.publish_report", return_value=report):
            text = answer(None, "Portfolio Manager", "Why are the backtests not passing the acceptance criteria?")
        self.assertIn("1 of 14 backtests clear every acceptance box", text)
        self.assertIn("Profit", text)
        self.assertIn("$7,900 under the line", text)
        self.assertIn("table on the R&D tab", text)
