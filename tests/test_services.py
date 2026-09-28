"""Ingestion, NinjaTrader confirmation, and the 5:00pm prop-account record."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from futuresfund.alerts import parse_alert


class ConfirmationTests(unittest.TestCase):
    def test_a_fill_confirms_and_a_reject_does_not(self):
        from futuresfund.trade_confirmation import judge

        signal = {"account": "PA-APEX-1", "instrument": "ES1!"}
        sent = {"sent": True, "body": ""}
        fill = {
            "account": "PA-APEX-1",
            "instrument": "ES 12-26",
            "isExit": False,
            "price": 5800.25,
            "quantity": 1,
            "epoch": 10,
        }
        confirmed = judge(signal, sent, [fill], closing=False)
        self.assertTrue(confirmed["confirmed"])
        self.assertIn("5800.25", confirmed["reason"])
        refused = judge(signal, sent, [], "Rejected", closing=False)
        self.assertFalse(refused["confirmed"])
        self.assertEqual(refused["status"], "rejected")
        dry = judge(signal, {"sent": False, "dry_run": True}, [fill], closing=False)
        self.assertEqual(dry["status"], "not_sent")

    def test_a_streamed_position_is_what_the_floor_manages(self):
        from futuresfund import ninjatrader
        from futuresfund.trade_manager import liquidation_reason, reconcile_position

        ninjatrader.apply_frame({
            "type": "positionUpdate",
            "account": "PA-APEX-1",
            "instrument": "ES 12-26",
            "marketPosition": "Long",
            "quantity": 1,
            "averagePrice": 5000,
            "unrealizedProfitLoss": -2000,
        })
        held, entry = reconcile_position({"account": "PA-APEX-1", "instrument": "ES1!"}, 0, None)
        self.assertEqual(held, 1)
        self.assertEqual(entry, 5000)
        reason = liquidation_reason(50000, held, entry, 5000, 50, -2000)
        self.assertIn("liquidate", reason)
        self.assertIsNone(liquidation_reason(50000, 1, 5000, 4998, 50, None))
        ninjatrader.reset_cache()


class DeskFlowTests(unittest.TestCase):
    def setUp(self):
        os.environ["DRY_RUN"] = "true"
        os.environ["PROP_ACCOUNTS"] = "PA-APEX-1"
        os.environ["FUTURES_MAX_QTY"] = "5"
        from futuresfund.ninjatrader import reset_cache

        reset_cache()

    def test_ingestion_adds_the_bar_and_the_floor_does_not(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "PA-APEX-1",
                "account_size": 50000,
                "strategies": [],
            })
            board = Board()
            refused = handle_interval(board, parse_alert(
                "account=PA-OTHER;instrument=ES1!;qty=1;timeframe=5;price=5000;time=2024-01-02T14:00:00Z;"
            ))
            self.assertFalse(refused["sent"])
            self.assertTrue(any(message["author"] == "Ingestion" for message in board.messages))
            self.assertFalse(any(message["author"] == "Floor Trader" for message in board.messages))

            added = handle_interval(board, parse_alert(
                "account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=5;price=5001;"
                "open=5000;high=5002;low=4999;time=2024-01-02T14:05:00Z;"
            ))
            self.assertEqual(added["action"], "bar")
            self.assertEqual(len(research.load_bars()), 1)
            added_notes = [message for message in board.messages if "Added the closed" in message["text"]]
            self.assertEqual(added_notes[0]["author"], "Ingestion")

    def test_a_desk_bar_is_stored_with_volume_and_not_sent(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "PA-APEX-1",
                "account_size": 50000,
                "strategies": [{"id": "armed", "active": True, "proven": True, "rule": {"name": "ema_cross", "fast": 8, "slow": 21}}],
            })
            board = Board()
            result = handle_interval(board, parse_alert(
                '{"id":"desk-bar","account":"PA-APEX-1","instrument":"ES1!","timeframe":"5m",'
                '"time":"2024-01-02T14:05:00Z","open":5000,"high":5002,"low":4999,"close":5001,'
                '"price":5001,"volume":1200,"qty":0,"action":"BUY"}'
            ))
            self.assertFalse(result["sent"])
            self.assertEqual(result["action"], "bar")
            self.assertIn("desk-bar", result["reason"])
            stored = research.load_bars()
            self.assertEqual(stored[-1]["c"], 5001)
            self.assertEqual(stored[-1]["v"], 1200)
            self.assertEqual(stored[-1]["h"], 5002)
            self.assertFalse(any(message["author"] == "Floor Trader" for message in board.messages))

    def test_the_price_indicator_body_is_a_desk_bar(self):
        signal = parse_alert(
            '{"id":"desk-bar","account":"desk-one","instrument":"ES1!","timeframe":"5",'
            '"time":"2026-09-27T22:15:00Z","open":7800.25,"high":7801.5,"low":7799.75,'
            '"close":7800.5,"price":7800.5,"volume":1234,"qty":0}'
        )
        self.assertEqual(signal["id"], "desk-bar")
        self.assertEqual(signal["timeframe"], "5m")
        self.assertEqual(signal["price"], 7800.5)
        self.assertEqual(signal["volume"], 1234)
        self.assertEqual(signal["open"], 7800.25)
        self.assertIsNone(signal["hinted_action"])

    def test_a_desk_bar_is_recorded_for_any_account(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "desk-one",
                "account_size": 50000,
                "strategies": [],
            })
            board = Board()
            result = handle_interval(board, parse_alert(
                '{"id":"desk-bar","account":"00000000","instrument":"ES1!","timeframe":"5",'
                '"time":"2026-09-27T22:20:00Z","open":7800,"high":7801,"low":7799,"close":7800.5,'
                '"price":7800.5,"volume":10,"qty":0}'
            ))
            self.assertFalse(result["sent"])
            self.assertEqual(result["action"], "bar")
            self.assertEqual(research.load_bars()[-1]["c"], 7800.5)
            self.assertFalse(any("APEX4415870000042" in message["text"] for message in board.messages))

    def test_two_running_strategies_use_their_own_accounts(self):
        from futuresfund import book as bookmod
        from futuresfund.book import set_running
        from futuresfund.strategy import consistent_leaders, series_match

        from futuresfund.strategy import book_account

        os.environ["PROP_ACCOUNTS"] = ""
        self.assertEqual(book_account({}), "paper")
        os.environ["PROP_ACCOUNTS"] = "desk-a,desk-b"
        self.assertEqual(book_account({}), "desk-a")
        self.assertEqual(book_account({"account": "kept"}), "kept")
        os.environ["PROP_ACCOUNTS"] = "PA-APEX-1"
        leaders = consistent_leaders(3)
        self.assertEqual(len(leaders), 3)
        for row in leaders:
            self.assertGreater(row["net_profit"], row["max_drawdown"])
            self.assertGreaterEqual(row["green"], 2)
        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            first = set_running(leaders[0]["id"], "paper", True)
            self.assertEqual(first[0]["account"], "paper")
            second = set_running(leaders[1]["id"], "paper", True)
            self.assertEqual(len(second), 2)
            self.assertEqual(second[0]["account"], second[1]["account"])
            with self.assertRaises(ValueError):
                set_running(leaders[2]["id"], "paper", True)
            strategy = {"contract": "ES1!", "timeframe": "5m", "account": "desk-one", "running": second, "strategies": []}
            left = parse_alert(
                '{"account":"paper","instrument":"ES1!","strategy":"%s","action":"BUY","qty":1,"timeframe":"5","price":5000}'
                % leaders[0]["title"]
            )
            right = parse_alert(
                '{"account":"paper","instrument":"ES1!","strategy":"%s","action":"SELL","qty":1,"timeframe":"5","price":5000}'
                % leaders[1]["title"]
            )
            missing = parse_alert('{"account":"paper","instrument":"ES1!","qty":1,"timeframe":"5","price":5000}')
            other = parse_alert('{"account":"paper","instrument":"ES1!","strategy":"Not a running strategy","qty":1,"timeframe":"5","price":5000}')
            self.assertEqual(left["strategy"], leaders[0]["title"])
            self.assertTrue(series_match(strategy, left)[0])
            self.assertTrue(series_match(strategy, right)[0])
            self.assertFalse(series_match(strategy, missing)[0])
            self.assertFalse(series_match(strategy, other)[0])

    def test_the_floor_executes_the_named_strategy_on_the_alert_account(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "desk-one",
                "account_size": 50000,
                "strategies": [],
            })
            saved = bookmod.load()
            saved["running"] = [{
                "id": "overnight",
                "title": "Overnight Drift Capture [Optimized]",
                "account": "paper",
            }]
            bookmod.save(saved)
            board = Board()
            placed = handle_interval(board, parse_alert(
                '{"account":"paper","instrument":"ES1!","strategy":"Overnight Drift Capture [Optimized]",'
                '"action":"SELL","qty":1,"order_type":"MARKET","tif":"DAY","destination":"tradovate",'
                '"price":7769.75,"timeframe":"2","time":"2026-09-28T16:00:00Z"}'
            ))
            self.assertEqual(placed["action"], "place")
            floor = [message["text"] for message in board.messages if message["author"] == "Floor Trader"]
            self.assertTrue(any("Overnight Drift Capture [Optimized]" in text and "paper" in text for text in floor))
            self.assertTrue(any("command=PLACE" in text and "account=paper" in text for text in floor))

    def test_the_floor_shows_the_webhook_and_the_analyst_follows_the_trade(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "2m",
                "account": "PA-APEX-1",
                "account_size": 50000,
                "strategies": [],
            })
            board = Board()
            placed = handle_interval(board, parse_alert(
                '{"account":"PA-APEX-1","instrument":"ES1!","action":"BUY","qty":1,'
                '"order_type":"MARKET","tif":"DAY","destination":"tradovate","price":7788.5,"timeframe":"1",'
                '"time":"2026-09-28T00:18:00Z"}'
            ))
            self.assertEqual(placed["action"], "place")
            floor = [message["text"] for message in board.messages if message["author"] == "Floor Trader"]
            self.assertTrue(any("command=PLACE" in text and "key=***" in text for text in floor))
            handle_interval(board, parse_alert(
                '{"id":"desk-bar","account":"PA-APEX-1","instrument":"ES1!","timeframe":"1",'
                '"time":"2026-09-28T00:19:00Z","open":7788.5,"high":7790,"low":7788,"close":7789.25,'
                '"price":7789.25,"volume":40,"qty":0}'
            ))
            analyst = [message["text"] for message in board.messages if message["author"] == "Trading Analyst"]
            self.assertTrue(any("Open P&L" in text and "7789.25" in text for text in analyst))

    def test_the_drawdown_tells_the_floor_to_close(self):
        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "PA-APEX-1",
                "account_size": 50000,
                "qty": 1,
                "working": {"contracts": 1, "entry": 5000},
                "strategies": [],
            })
            board = Board()
            closed = handle_interval(board, parse_alert(
                "account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=1;price=4960;"
                "open=5000;high=5000;low=4960;time=2024-01-02T14:03:00Z;"
            ))
            self.assertEqual(closed["action"], "close")
            self.assertIn("liquidate", closed["reason"])
            self.assertTrue(any(
                message["author"] == "Trading Analyst" and "Floor trader, close this trade" in message["text"]
                for message in board.messages
            ))

    def test_prop_mail_is_recorded_at_the_five_pm_meeting(self):
        from futuresfund import book as bookmod
        from futuresfund.account_mail import accept_mail, brief_prop_accounts, parse_account_update
        from futuresfund.board import Board

        parsed = parse_account_update("Daily loss limit is $1,500. Trailing drawdown is $2,000. Max contracts is 4.")
        self.assertEqual(parsed["daily_loss_limit"], 1500)
        self.assertEqual(parsed["max_drawdown"], 2000)
        self.assertEqual(parsed["max_contracts"], 4)
        self.assertEqual(parse_account_update("The balance is 25000."), {})

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            board = Board()
            accepted = accept_mail(board, {
                "from": "notices@apex.com",
                "subject": "Account update",
                "text": "Daily loss limit is $1,500. Trailing drawdown is $2,000. Max contracts is 4.",
            })
            self.assertTrue(accepted["ok"])
            self.assertTrue(any(message["author"] == "Systems Administrator" for message in board.messages))
            mention = brief_prop_accounts(board)
            saved = bookmod.load()["headquarters_rules"]["text"]
            self.assertIn("5:00pm", saved)
            self.assertIn("1,500", saved)
            self.assertEqual(saved, mention)
            self.assertTrue(any(
                message["author"] == "Portfolio Manager" and message["channel"] == "headquarters"
                for message in board.messages
            ))

    def test_an_alert_trade_is_the_order_the_floor_sends(self):
        from futuresfund.trade_manager import plan_from_alert

        buy = plan_from_alert({"hinted_action": "BUY", "qty": 1}, 0, 1)
        self.assertEqual(buy["action"], "place")
        self.assertEqual(buy["side"], "BUY")
        self.assertTrue(buy["from_alert"])
        hold = plan_from_alert({"hinted_action": "BUY", "qty": 1}, 1, 1)
        self.assertEqual(hold["action"], "hold")
        self.assertIsNone(plan_from_alert({"qty": 1}, 0, 1))


class AccountRuleTests(unittest.TestCase):
    def test_an_entered_trailing_drawdown_replaces_the_published_tier(self):
        import tempfile
        from pathlib import Path

        from futuresfund import book as bookmod
        from futuresfund.book import save_account_rules
        from futuresfund.prop_rules import active_limits, rules_sentence
        from futuresfund.trade_manager import liquidation_reason

        original = bookmod.BOOK_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bookmod.BOOK_PATH = Path(tmp) / "book.json"
                save_account_rules({
                    "account": "APEX-441587-44",
                    "size": 50000,
                    "profit_target": 3000,
                    "max_drawdown": 2500,
                    "trailing": True,
                    "max_contracts": 10,
                })
                rules = active_limits(50000)
                self.assertEqual(rules["max_drawdown"], 2500.0)
                self.assertEqual(rules["profit_target"], 3000.0)
                self.assertTrue(rules["trailing"])
                self.assertEqual(rules["max_contracts"], 10)
                self.assertIn("$2,500", rules_sentence(50000))
                self.assertIn("trailing", rules_sentence(50000))
                self.assertIsNone(liquidation_reason(50000, 1, 5000, 5000, 50, -2000, None, 2500))
                self.assertIn("liquidate", liquidation_reason(50000, 1, 5000, 5000, 50, -2500, None, 2500))
        finally:
            bookmod.BOOK_PATH = original


class AnalystAndLibraryTests(unittest.TestCase):
    def test_a_price_update_refreshes_the_account_when_flat(self):
        import tempfile
        from pathlib import Path

        from futuresfund import book as bookmod
        from futuresfund import research
        from futuresfund.board import Board
        from futuresfund.session import handle_interval
        from futuresfund.strategy import store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            research.BARS_PATH = Path(tmp) / "bars.json"
            store_strategy({
                "contract": "ES1!",
                "timeframe": "5m",
                "account": "desk-one",
                "account_size": 50000,
                "strategies": [],
            })
            board = Board()
            handle_interval(board, parse_alert(
                '{"id":"desk-bar","account":"desk-one","instrument":"ES1!","timeframe":"5",'
                '"time":"2026-09-28T16:00:00Z","open":7760,"high":7762,"low":7758,"close":7761,'
                '"price":7761,"volume":100,"qty":0}'
            ))
            analyst = [message["text"] for message in board.messages if message["author"] == "Trading Analyst"]
            self.assertTrue(any("Equity" in text and "No trade is open" in text for text in analyst))

    def test_a_profitable_finished_study_is_in_the_library(self):
        import tempfile
        from pathlib import Path

        from futuresfund import library

        original_notes = library._NOTES
        original_cache = dict(library._CACHE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                library._NOTES = Path(tmp)
                library._CACHE = {"stamp": None, "rows": []}
                payload = {
                    "file": "sample_drift.pine",
                    "title": "Sample Drift Study",
                    "best": {
                        "engine": "pineforge",
                        "runner": "pineforge",
                        "net_profit": 4200,
                        "max_drawdown": 800,
                        "trades": 9,
                        "timeframe": "5m",
                        "win_rate": 60,
                        "frames": [{"timeframe": "5m", "net_profit": 4200, "max_drawdown": 800, "trades": 9}],
                    },
                }
                (library._NOTES / "sample_drift.json").write_text(__import__("json").dumps(payload), encoding="utf-8")
                titles = {row["title"] for row in library.search_library("ES1!")["strategies"]}
                self.assertIn("Sample Drift Study", titles)
                loser = dict(payload)
                loser["title"] = "Sample Loser"
                loser["best"] = dict(payload["best"])
                loser["best"]["net_profit"] = -100
                loser["best"]["frames"] = [{"timeframe": "5m", "net_profit": -100, "max_drawdown": 800, "trades": 9}]
                (library._NOTES / "sample_loser.json").write_text(__import__("json").dumps(loser), encoding="utf-8")
                library._CACHE = {"stamp": None, "rows": []}
                titles = {row["title"] for row in library.search_library("ES1!")["strategies"]}
                self.assertIn("Sample Drift Study", titles)
                self.assertNotIn("Sample Loser", titles)
        finally:
            library._NOTES = original_notes
            library._CACHE = original_cache


class ChartTests(unittest.TestCase):
    def test_the_last_candle_matches_the_exported_market_file(self):
        import csv
        from pathlib import Path

        from futuresfund.chart_view import chart_payload

        path = Path(__file__).resolve().parents[1] / "CME_MINI_ES1!, 5_52959.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            last = list(csv.DictReader(handle))[-1]
        candle = chart_payload("ES1!", "5m", 5)["candles"][-1]
        self.assertEqual(candle["o"], float(last["open"]))
        self.assertEqual(candle["h"], float(last["high"]))
        self.assertEqual(candle["l"], float(last["low"]))
        self.assertEqual(candle["c"], float(last["close"]))
        self.assertEqual(candle["v"], float(last["Volume"]))
        self.assertIn("ET", candle["et"] + " ET")
        empty = chart_payload("GC1!", "5m", 20)
        self.assertEqual(empty["candles"], [])


class FirmRecordTests(unittest.TestCase):
    def test_the_portfolio_manager_can_read_the_firm(self):
        from futuresfund.book import firm_record

        text = firm_record()
        self.assertIn("Strategy library", text)
        self.assertIn("Library Gold:", text)
        self.assertIn("Library ES:", text)
        self.assertIn("desk-bar", text)
        self.assertIn("Dry run is on", text)
        self.assertNotIn("CROSSTRADE_KEY", text)
        self.assertNotIn("SMTP_PASSWORD", text)


class LibraryTests(unittest.TestCase):
    def test_profitable_studies_are_separated_by_contract(self):
        from futuresfund.library import contract_of, library_script, search_library

        es_symbol, es_label = contract_of("Overnight Drift Capture [Optimized]", "overnight.pine")
        self.assertEqual((es_symbol, es_label), ("ES1!", "ES"))
        gold_symbol, gold_label = contract_of("Gold opening range", "gc_orb.pine")
        self.assertEqual((gold_symbol, gold_label), ("GC1!", "Gold"))
        es = search_library("ES1!")
        gold = search_library("GC1!")
        titles = {row["title"] for row in es["strategies"]}
        self.assertIn("Overnight Drift Capture [Optimized]", titles)
        self.assertNotIn("delta_vol_night_strategy", titles)
        drift = next(row for row in es["strategies"] if row["title"] == "Overnight Drift Capture [Optimized]")
        self.assertEqual(drift["timeframe"], "2m")
        self.assertEqual(drift["net_profit"], 7569.5)
        self.assertGreater(drift["trades"], 0)
        self.assertEqual(gold["strategies"], [])
        script = library_script(drift["id"])
        self.assertTrue(script["pine"].startswith("//@version=5"))


if __name__ == "__main__":
    unittest.main()
