"""Ingestion, NinjaTrader confirmation, and the 5:00pm prop-account record."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from futuresfund.alerts import parse_alert

_LIVE = {}


def setUpModule():
    """Alert candles from these tests go to a temporary file, never the desk's own live candles."""
    from futuresfund import chart_feed

    _LIVE["folder"] = tempfile.TemporaryDirectory()
    _LIVE["path"] = chart_feed.LIVE_BARS_PATH
    chart_feed.LIVE_BARS_PATH = Path(_LIVE["folder"].name) / "live.json"


def tearDownModule():
    from futuresfund import chart_feed

    chart_feed.LIVE_BARS_PATH = _LIVE["path"]
    _LIVE["folder"].cleanup()


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
            "unrealizedProfitLoss": -2500,
        })
        held, entry = reconcile_position({"account": "PA-APEX-1", "instrument": "ES1!"}, 0, None)
        self.assertEqual(held, 1)
        self.assertEqual(entry, 5000)
        reason = liquidation_reason(50000, held, entry, 5000, 50, -2500)
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
            self.assertTrue(any("command=place" in text and "account=paper" in text for text in floor))

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
            self.assertTrue(any("command=place" in text and "key=***" in text for text in floor))
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
                "account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=1;price=4950;"
                "open=5000;high=5000;low=4950;time=2024-01-02T14:03:00Z;"
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

    def test_closing_a_short_books_one_trade_and_leaves_the_book_flat(self):
        from futuresfund.book import apply_target, paper_status

        book = {
            "positions": {
                "A|ES1!": {
                    "account": "A",
                    "instrument": "ES1!",
                    "contracts": -1,
                    "average_price": 7749.25,
                    "last_price": 7749.25,
                }
            },
            "paper": {"realized": 0.0, "peak": 50000},
            "strategy": {"account_size": 50000},
        }
        apply_target(book, {"account": "A", "instrument": "ES1!", "yahoo": "ES=F"}, 0, 7731.0)
        self.assertEqual(book["positions"], {})
        self.assertEqual(book["paper"]["realized"], 912.5)
        self.assertEqual(len(book["trades"]), 1)
        self.assertEqual(book["trades"][0]["pnl"], 912.5)
        self.assertEqual(book["trades"][0]["side"], "short")
        status = paper_status(book)
        self.assertEqual(status["active_pnl"], 0)
        self.assertEqual(status["pnl"], 912.5)
        self.assertEqual(status["equity"], 50912.5)
        book["positions"]["A|ES1!"] = {
            "account": "A",
            "instrument": "ES1!",
            "contracts": -1,
            "average_price": 7749.25,
            "last_price": 7749.25,
        }
        apply_target(book, {"account": "A", "instrument": "ES1!", "yahoo": "ES=F"}, 1, 7731.0)
        self.assertEqual(book["positions"], {})

    def test_each_account_shows_its_own_live_equity(self):
        os.environ["PROP_ACCOUNTS"] = "ONE,TWO"
        from futuresfund.book import display_accounts

        book = {
            "positions": {
                "ONE|ES1!": {
                    "account": "ONE",
                    "instrument": "ES1!",
                    "contracts": 1,
                    "average_price": 100,
                    "last_price": 102,
                }
            },
            "account_books": {
                "ONE": {"size": 50000, "realized": 0, "peak": 50000},
                "TWO": {"size": 25000, "realized": 0, "peak": 25000},
            },
            "account_rules": {"account": "ONE", "size": 50000},
            "strategy": {"account_size": 50000},
            "paper": {"realized": 0, "peak": 50000},
        }
        rows = {row["account"]: row for row in display_accounts(book)}
        self.assertEqual(set(rows), {"ONE", "TWO"})
        self.assertEqual(rows["ONE"]["pnl"], 100.0)
        self.assertEqual(rows["ONE"]["equity"], 50100.0)
        self.assertEqual(rows["TWO"]["pnl"], 0.0)
        self.assertEqual(rows["TWO"]["equity"], 25000.0)
        self.assertEqual(rows["TWO"]["drawdown"], 0.0)

    def test_an_alert_trade_is_the_order_the_floor_sends(self):
        from futuresfund.trade_manager import plan_from_alert

        buy = plan_from_alert({"hinted_action": "BUY", "qty": 1}, 0, 1)
        self.assertEqual(buy["action"], "place")
        self.assertEqual(buy["side"], "BUY")
        self.assertTrue(buy["from_alert"])
        hold = plan_from_alert({"hinted_action": "BUY", "qty": 1}, 1, 1)
        self.assertEqual(hold["action"], "hold")
        cover = plan_from_alert({"hinted_action": "BUY", "qty": 1}, -1, 1)
        self.assertEqual(cover["action"], "close")
        self.assertEqual(cover["target"], 0)
        self.assertEqual(cover["side"], "BUY")
        self.assertEqual(cover["qty"], 1)
        flat = plan_from_alert({"hinted_action": "SELL", "qty": 1}, 1, 1)
        self.assertEqual(flat["action"], "close")
        self.assertEqual(flat["target"], 0)
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
                        "engine": "backtrader",
                        "runner": "backtrader",
                        "net_profit": 25000,
                        "max_drawdown": 800,
                        "trades": 120,
                        "days": 260,
                        "timeframe": "5m",
                        "win_rate": 65,
                        "profit_factor": 1.4,
                        "profit_ratio": 1.2,
                        "frames": [{"timeframe": "5m", "net_profit": 25000, "max_drawdown": 800, "trades": 120, "days": 260, "profit_ratio": 1.2}],
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
    def setUp(self):
        from unittest import mock

        from futuresfund import kronos_track

        self.track = tempfile.TemporaryDirectory()
        self.track_patch = mock.patch.object(kronos_track, "PATH", Path(self.track.name) / "kronos_direction.json")
        self.track_patch.start()

    def tearDown(self):
        self.track_patch.stop()
        self.track.cleanup()

    def test_an_es_price_labeled_as_gold_stays_on_the_es_chart(self):
        from futuresfund.chart_feed import place_root

        anchors = {"ES": 7726.5, "QO": 4174.5}
        self.assertEqual(place_root("QO", 7746.0, anchors), "ES")
        self.assertEqual(place_root("QO", 4174.25, anchors), "QO")
        self.assertEqual(place_root("ES", 7730.0, anchors), "ES")

    def test_the_last_candle_matches_the_exported_market_file(self):
        import csv
        from pathlib import Path

        from futuresfund.chart_view import chart_payload

        path = Path(__file__).resolve().parents[1] / "chartData" / "es" / "CME_MINI_ES1!, 1_fa513.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            last = list(csv.DictReader(handle))[-1]
        from zoneinfo import ZoneInfo

        stamp = int(float(last["time"]))
        if stamp > 10_000_000_000:
            stamp //= 1000
        et = datetime.fromtimestamp(stamp, timezone.utc).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
        minute_payload = chart_payload("ES1!", "1m", 400)
        minute = minute_payload["candles"]
        candle = next(item for item in reversed(minute) if item["et"] == et)
        self.assertEqual(candle["o"], float(last["open"]))
        self.assertEqual(candle["h"], float(last["high"]))
        self.assertEqual(candle["l"], float(last["low"]))
        self.assertEqual(candle["c"], float(last["close"]))
        self.assertEqual(candle["v"], float(last["Volume"]))
        five = chart_payload("ES1!", "5m", 400)
        bucket = next(item for item in reversed(five["candles"]) if item["et"] <= et)
        self.assertEqual(bucket["c"], float(last["close"]))
        self.assertGreaterEqual(bucket["h"], float(last["high"]))
        self.assertLess(five["count"], minute_payload["count"])
        self.assertIn("ET", candle["et"] + " ET")
        empty = chart_payload("GC1!", "5m", 20)
        self.assertEqual(empty["candles"], [])
        gold_path = Path(__file__).resolve().parents[1] / "chartData" / "qo" / "COMEX_MINI_QO1!, 1_ffc58.csv"
        with gold_path.open(encoding="utf-8-sig", newline="") as handle:
            gold_last = list(csv.DictReader(handle))[-1]
        gold_stamp = int(float(gold_last["time"]))
        if gold_stamp > 10_000_000_000:
            gold_stamp //= 1000
        gold_et = datetime.fromtimestamp(gold_stamp, timezone.utc).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")
        gold = chart_payload("QO1!", "1m", 400)
        gold_candle = next(item for item in reversed(gold["candles"]) if item["et"] == gold_et)
        self.assertEqual(gold_candle["c"], float(gold_last["close"]))
        self.assertGreater(gold["count"], 100)
        indicators = chart_payload("ES1!", "5m", 30)["indicators"]
        self.assertIn("volume", indicators)
        self.assertIn("macd", indicators)
        self.assertIn("delta", indicators)

    def test_one_minute_bars_update_the_open_candle(self):
        from futuresfund.chart_feed import fold_bars, record_live_bar
        from futuresfund.chart_view import chart_payload
        from futuresfund.chart_feed import _epoch_ms
        import futuresfund.chart_feed as feed

        base = 1_700_000_000_000
        base -= base % (6 * 60 * 1000)
        bars = [
            {"t": "2026-09-28T18:00:00+00:00", "epoch": base, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 10},
            {"t": "2026-09-28T18:01:00+00:00", "epoch": base + 60_000, "o": 100.5, "h": 103, "l": 100, "c": 102, "v": 4, "macd": -1.5},
        ]
        folded = fold_bars(bars, "6m")
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["o"], 100)
        self.assertEqual(folded[0]["h"], 103)
        self.assertEqual(folded[0]["c"], 102)
        self.assertEqual(folded[0]["v"], 14)
        self.assertEqual(folded[0]["macd"], -1.5)

        original = feed.LIVE_BARS_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                feed.LIVE_BARS_PATH = Path(tmp) / "live.json"
                before = chart_payload("ES1!", "5m", 5)["candles"][-1]
                opened = _epoch_ms(before["t"])
                stamp = datetime.fromtimestamp((opened + 60_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                live = {"instrument": "ES1!", "timeframe": "1m", "price": before["c"] - 2}
                record_live_bar(live, {
                    "t": stamp,
                    "o": before["c"],
                    "h": before["h"] + 1,
                    "l": before["l"] - 1,
                    "c": before["c"] - 1,
                    "v": 25,
                })
                again = record_live_bar(live, {
                    "t": stamp,
                    "o": before["c"],
                    "h": before["h"] + 2,
                    "l": before["l"] - 1,
                    "c": before["c"] - 2,
                    "v": 30,
                })
                self.assertEqual(len(feed._load()), 1)
                self.assertEqual(again["c"], before["c"] - 2)
                updated = chart_payload("ES1!", "5m", 5)["candles"][-1]
                self.assertEqual(updated["o"], before["o"])
                self.assertEqual(updated["c"], before["c"] - 2)
                self.assertEqual(updated["h"], before["h"] + 2)
                self.assertEqual(updated["v"], before["v"] + 30)
                six = chart_payload("ES1!", "6m", 8)
                self.assertGreaterEqual(len(six["candles"]), 2)
                self.assertIn("volume", six["indicators"])
        finally:
            feed.LIVE_BARS_PATH = original


class QoPointTests(unittest.TestCase):
    def test_qo_is_tested_at_50_dollars_a_point(self):
        from futuresfund.contracts import POINT_VALUE
        from futuresfund.learn import _with_contract_point, research_instruments
        from futuresfund.library import _RESEARCH_ROOT

        self.assertEqual(POINT_VALUE["QO"], 50)
        self.assertEqual(POINT_VALUE["GC"], 100)
        self.assertEqual(_RESEARCH_ROOT["GC1!"], "QO")
        self.assertIn("QO", research_instruments())
        self.assertNotIn("GC", research_instruments())
        updated = _with_contract_point({"point_value": 100, "length": 10}, "QO")
        self.assertEqual(updated["point_value"], 50.0)
        self.assertEqual(updated["length"], 10)


class InstrumentCoverageTests(unittest.TestCase):
    def test_a_strategy_is_not_tested_twice_and_waits_for_every_instrument(self):
        from futuresfund.charts import chart_roots
        from futuresfund.learn import pending_instruments, ready_to_file, research_instruments, tested_instruments

        self.assertEqual(chart_roots(), ["ES", "GC", "NQ", "QO"])
        self.assertEqual(research_instruments(), ["ES", "QO", "NQ"])
        self.assertNotIn("GC", research_instruments())
        old = {"trials": [{"net_profit": 1}], "best": {"net_profit": 1}}
        self.assertEqual(tested_instruments(old), {"ES"})
        self.assertEqual(pending_instruments(old, ["ES", "QO"]), ["QO"])
        self.assertFalse(ready_to_file(old, ["ES", "QO"]))
        covered = {
            "instruments": {
                "ES": {"attempts": 2, "best": {"net_profit": 1}},
                "QO": {"attempts": 2, "best": {"net_profit": 4}},
            }
        }
        self.assertEqual(pending_instruments(covered, ["ES", "QO"]), [])
        self.assertTrue(ready_to_file(covered, ["ES", "QO"]))
        blocked = {"instruments": {"ES": {"blocked": True, "attempts": 1, "best": {}}}}
        self.assertEqual(pending_instruments(blocked, ["ES", "QO"]), ["ES", "QO"])
        failed = {"instruments": {"ES": {"error": "compile", "attempts": 1, "best": {"fatal": True, "error": "compile"}}}}
        self.assertEqual(tested_instruments(failed), set())
        self.assertEqual(pending_instruments(failed, ["ES", "QO", "NQ"]), ["ES", "QO", "NQ"])


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
        self.assertEqual(contract_of("Night breakout", "night.pine", "QO1!"), ("GC1!", "Gold"))
        labels = [item["label"] for item in search_library("all")["contracts"]]
        self.assertEqual(labels.count("Gold"), 1)
        es = search_library("ES1!")
        gold = search_library("GC1!")
        titles = {row["title"] for row in es["strategies"]}
        self.assertIn("Overnight Drift Capture [Optimized]", titles)
        self.assertNotIn("delta_vol_night_strategy", titles)
        drift = next(row for row in es["strategies"] if row["title"] == "Overnight Drift Capture [Optimized]")
        self.assertEqual(drift["timeframe"], "2m")
        self.assertEqual(drift["net_profit"], 7569.5)
        self.assertGreater(drift["trades"], 0)
        self.assertTrue(gold["strategies"])
        self.assertTrue(all(row["contract_label"] == "Gold" and row["net_profit"] > 0 for row in gold["strategies"]))
        script = library_script(drift["id"])
        self.assertTrue(script["pine"].startswith("//@version=5"))

    def test_every_library_strategy_still_has_its_script(self):
        from futuresfund.library import library_script, search_library

        payload = search_library("all")
        missing = []
        for bucket in ("strategies", "untested"):
            for row in payload.get(bucket) or []:
                opened = library_script(row.get("id"), row.get("contract"))
                pine = (opened or {}).get("pine") or ""
                if "strategy(" not in pine:
                    missing.append(f"{bucket}: {row.get('file') or row.get('title')}")
        self.assertEqual(missing, [])


class AddedLibraryTests(unittest.TestCase):
    def test_a_hand_added_strategy_stays_on_the_shelf_until_it_is_deleted(self):
        from unittest.mock import patch

        from futuresfund import library

        pine = "//@version=5\nstrategy(\"Desk Add\", overlay=true)\n"
        cache = dict(library._CACHE)
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes"
            added = Path(tmp) / "learned"
            original_notes = library._NOTES
            original_added = library._ADDED_DIR
            library._NOTES = notes
            library._ADDED_DIR = added
            try:
                with self.assertRaises(ValueError):
                    library.add_strategy("Nope", "ES1!", "plot(close)")
                row = library.add_strategy("Desk Add", "NQ1!", pine, "5m")
                self.assertEqual(row["contract"], "NQ1!")
                self.assertEqual(row["contract_label"], "Nasdaq")
                self.assertEqual(row["kind"], "Added")
                self.assertTrue((added / row["file"]).is_file())
                self.assertIn("strategy(", (added / row["file"]).read_text(encoding="utf-8"))
                kept = library.shelf_rows([
                    {"id": "weak", "script": "Weak", "net_profit": 100},
                    {"id": "rich", "script": "Rich One", "net_profit": 25000, "max_drawdown": 1000, "trades": 150, "days": 260, "win_rate": 65, "profit_factor": 1.4, "profit_ratio": 1.2},
                    {"id": "rich-2", "script": "Rich One v2", "net_profit": 21000, "max_drawdown": 500, "profit_ratio": 1.2},
                    row,
                ])
                self.assertEqual({item["id"] for item in kept}, {"rich", row["id"]})
                neighbor = {
                    "id": "neighbor",
                    "title": "Neighbor",
                    "script": "Neighbor",
                    "file": "neighbor.pine",
                    "contract": "ES1!",
                    "net_profit": 30000,
                    "max_drawdown": 1000,
                    "trades": 150,
                    "days": 260,
                    "win_rate": 65,
                    "profit_factor": 1.4,
                    "kind": "Measured",
                    "profit_ratio": 1.2,
                }
                library._write_index(library.shelf_rows([neighbor, row]))
                library._write_untested([
                    {"id": row["id"], "contract": row["contract"], "title": row["title"]},
                    {"id": "keep-me", "contract": "ES1!", "title": "Keep"},
                ])

                def fake_pine(filename):
                    path = added / filename
                    return path.read_text(encoding="utf-8") if path.is_file() else ""

                with patch("futuresfund.strategy.pine_text", fake_pine):
                    opened = library.library_script(row["id"], row["contract"])
                self.assertIn("strategy(", opened["pine"])
                self.assertTrue(library.remove_strategy(row["id"], row["contract"]))
                self.assertFalse((added / row["file"]).exists())
                self.assertEqual({item["id"] for item in library._load_index_rows()}, {"neighbor"})
                self.assertEqual({item["id"] for item in library._load_untested()}, {"keep-me"})
                self.assertFalse(library.remove_strategy(row["id"]))
            finally:
                library._NOTES = original_notes
                library._ADDED_DIR = original_added
                library._CACHE.clear()
                library._CACHE.update(cache)


if __name__ == "__main__":
    unittest.main()
