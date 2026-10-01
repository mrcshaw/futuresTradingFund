"""The order is built from the alert and sent only for a Buy or Sell rating."""

from __future__ import annotations

import os
import unittest

from futuresfund.alerts import parse_alert
from futuresfund.crosstrade import build_payload, send, trade_side


ALERT = """
account=PA-APEX-1;
instrument=MES 12-26;
qty=1;
price=5840.25;
position=flat;
order_type=MARKET;
tif=DAY;
key=do-not-forward-this;
"""


class AlertTests(unittest.TestCase):
    def setUp(self):
        os.environ["FUTURES_MAX_QTY"] = "5"
        os.environ["PROP_ACCOUNTS"] = "PA-APEX-1"
        os.environ["DRY_RUN"] = "true"
        os.environ["CROSSTRADE_KEY"] = ""

    def test_text_alert_keeps_the_prop_account_and_drops_any_key(self):
        signal = parse_alert(ALERT)
        self.assertEqual(signal["account"], "PA-APEX-1")
        self.assertEqual(signal["instrument"], "MES 12-26")
        self.assertEqual(signal["yahoo"], "MES=F")
        self.assertEqual(signal["qty"], 1)
        self.assertEqual(signal["price"], 5840.25)
        self.assertEqual(signal["position"], 0.0)
        self.assertNotIn("key", signal)

    def test_json_alert_reads_a_short_position(self):
        signal = parse_alert('{"account":"PA-APEX-1","instrument":"MNQ1!","qty":2,"position":"short","position_size":2,"price":21000}')
        self.assertEqual(signal["instrument"], "MNQ1!")
        self.assertEqual(signal["yahoo"], "MNQ=F")
        self.assertEqual(signal["position"], -2.0)

    def test_an_equity_symbol_is_refused(self):
        with self.assertRaises(ValueError):
            parse_alert("account=PA-APEX-1;instrument=AAPL;qty=1;")

    def test_a_smuggled_command_is_not_forwarded(self):
        signal = parse_alert("account=PA-APEX-1;command=FLATTEN;instrument=MES 12-26;qty=1;")
        preview = send(signal, "Sell")["preview"]
        self.assertIn("command=place;", preview)
        self.assertNotIn("FLATTEN", preview)

    def test_account_with_a_semicolon_is_refused(self):
        with self.assertRaises(ValueError):
            parse_alert('{"account":"PA-APEX-1;command=FLATTEN","instrument":"MES","qty":1}')


class OrderTests(unittest.TestCase):
    def setUp(self):
        os.environ["FUTURES_MAX_QTY"] = "5"
        os.environ["PROP_ACCOUNTS"] = "PA-APEX-1"
        os.environ["DRY_RUN"] = "true"
        os.environ.pop("CROSSTRADE_KEY", None)
        self.signal = parse_alert(ALERT)

    def test_buy_rating_builds_the_crosstrade_order_for_that_account(self):
        result = send(self.signal, "Buy")
        self.assertFalse(result["sent"])
        self.assertTrue(result["dry_run"])
        self.assertIn("account=PA-APEX-1;", result["preview"])
        self.assertIn("action=buy;", result["preview"])
        self.assertIn("instrument=MES 12-26;", result["preview"])
        self.assertIn("qty=1;", result["preview"])
        self.assertIn("key=***;", result["preview"])
        self.assertNotIn("do-not-forward-this", result["preview"])

    def test_hold_and_review_do_not_build_an_order(self):
        self.assertIsNone(trade_side("Hold"))
        self.assertIsNone(trade_side("REVIEW"))
        self.assertEqual(trade_side("Overweight"), "BUY")
        self.assertEqual(trade_side("Underweight"), "SELL")
        result = send(self.signal, "Hold")
        self.assertFalse(result["sent"])
        self.assertNotIn("preview", result)

    def test_a_different_account_does_not_block_the_order(self):
        os.environ["PROP_ACCOUNTS"] = "PA-OTHER"
        payload = build_payload(self.signal, "BUY")
        self.assertIn("account=PA-APEX-1;", payload)
        self.assertIn("command=place;", payload)
        self.assertIn("action=buy;", payload)
        self.assertNotIn("flatten_first", payload)

    def test_a_buy_while_short_does_not_open_a_new_long(self):
        signal = parse_alert('{"account":"PA-APEX-1","instrument":"MES1!","qty":1,"position":-1}')
        payload = build_payload(signal, "BUY")
        self.assertNotIn("flatten_first", payload)
        self.assertIn("action=buy;", payload)
        self.assertIn("order_type=market;", payload)
        self.assertIn("tif=day;", payload)

    def test_tradingview_crosstrade_alert_is_rebuilt_without_its_key(self):
        os.environ["CROSSTRADE_KEY"] = "server-side-key"
        raw = """
key=do-not-forward-this;
command=place;
account=PA-APEX-1;
instrument=ES1!;
action=sell;
qty=1;
order_type=market;
tif=day;
price=5800;
sync_strategy=true;
market_position=short;
prev_market_position=flat;
out_of_sync=flatten;
"""
        signal = parse_alert(raw)
        self.assertEqual(signal["instrument"], "ES1!")
        self.assertEqual(signal["position"], -1.0)
        self.assertNotIn("key", signal)
        self.assertTrue(signal["sync_strategy"])
        preview = send(signal, "Sell")["preview"]
        self.assertIn("command=place;", preview)
        self.assertIn("action=sell;", preview)
        self.assertIn("sync_strategy=true;", preview)
        self.assertIn("market_position=short;", preview)
        self.assertIn("prev_market_position=flat;", preview)
        self.assertIn("out_of_sync=flatten;", preview)
        self.assertIn("key=***;", preview)
        self.assertNotIn("do-not-forward-this", preview)
        self.assertNotIn("server-side-key", preview)


if __name__ == "__main__":
    unittest.main()


class StrategyTests(unittest.TestCase):
    def test_timeframe_accepts_a_tradingview_interval(self):
        from futuresfund.strategy import normalize_timeframe
        self.assertEqual(normalize_timeframe("15"), "15m")
        self.assertEqual(normalize_timeframe("60"), "1h")
        self.assertEqual(normalize_timeframe("D"), "1D")
        signal = parse_alert("account=PA-APEX-1;instrument=MES 12-26;qty=1;timeframe=15;")
        self.assertEqual(signal["timeframe"], "15m")

    def test_meeting_keeps_a_better_or_same_rule(self):
        from futuresfund.research import choose_update
        current = {"rule": {"name": "ema_cross", "fast": 12, "slow": 50}, "backtest": {"net_profit": 100}}
        same = {"rule": {"name": "ema_cross", "fast": 12, "slow": 50}, "proven": True, "backtest": {"net_profit": 90}}
        better = {"rule": {"name": "donchian", "length": 20}, "proven": True, "backtest": {"net_profit": 200}}
        loser = {"rule": {"name": "donchian", "length": 20}, "proven": False, "backtest": {"net_profit": -10}}
        self.assertEqual(choose_update(current, same, 0), "keep")
        self.assertEqual(choose_update(current, better, 0), "replace")
        self.assertEqual(choose_update(current, loser, 0), "pause")

    def test_interval_alert_must_match_the_assigned_account(self):
        from futuresfund.strategy import accept_alert
        strategy = {
            "contract": "MES1!",
            "timeframe": "15m",
            "account": "PA-APEX-1",
            "stance": "keep",
            "proven": True,
            "rule": {"name": "ema_cross", "fast": 12, "slow": 50},
        }
        signal = parse_alert("account=PA-APEX-1;instrument=MES 12-26;qty=1;timeframe=15m;action=SELL;price=5800;")
        ok, _detail = accept_alert(strategy, signal)
        self.assertTrue(ok)
        other = parse_alert("account=PA-OTHER;instrument=MES 12-26;qty=1;timeframe=15m;price=5800;")
        from futuresfund.strategy import series_match
        self.assertTrue(series_match(strategy, other)[0])
        paused = {**strategy, "stance": "paused"}
        self.assertFalse(accept_alert(paused, signal)[0])

    def test_plan_places_holds_or_closes(self):
        from futuresfund.research import trade_plan
        self.assertEqual(trade_plan(1, 0, 2)["action"], "place")
        self.assertEqual(trade_plan(1, 2, 2)["action"], "hold")
        self.assertEqual(trade_plan(0, 2, 2)["action"], "close")
        self.assertTrue(trade_plan(-1, 2, 2)["flatten_first"])

    def test_bar_file_develops_a_profitable_rule_on_a_trend(self):
        from futuresfund.csv_bars import parse_tradingview_csv
        from futuresfund.research import develop
        lines = ["time,open,high,low,close,Volume"]
        for i in range(160):
            price = 5000 + i * 2
            lines.append(f"2024-01-02T{i // 60:02d}:{i % 60:02d}:00Z,{price-1},{price+1},{price-2},{price},10")
        bars = parse_tradingview_csv("\n".join(lines))
        strategy = develop(bars, "MES", "15m", "PA-APEX-1", 50000, 500)
        self.assertTrue(strategy["proven"])
        self.assertGreater(strategy["backtest"]["net_profit"], 0)
        self.assertIn("Long", strategy["formula"])
        names = {item["name"] for item in strategy["strategies"]}
        self.assertTrue({"ema_cross", "donchian", "rsi_revert", "poc_pullback", "vwap_side"} <= names)
        self.assertEqual(sum(1 for item in strategy["strategies"] if item["active"]), 0)
        pine = next(item["pine"] for item in strategy["strategies"] if item["name"] == "poc_pullback")
        self.assertTrue(pine.startswith("//@version=5"))
        self.assertIn("strategy(", pine)
        self.assertNotIn("POC Color + Pullback", pine)
        self.assertNotIn("min_slope_deg", pine)

    def test_agents_can_propose_a_rule_the_engine_backtests(self):
        from futuresfund.discuss import parse_proposal
        from futuresfund.research import assemble_book, evaluate_rule
        name, params = parse_proposal('The rule is {"name":"ema_cross","params":{"fast":8,"slow":34}}')
        self.assertEqual((name, params), ("ema_cross", {"fast": 8, "slow": 34}))
        with self.assertRaises(ValueError):
            parse_proposal('{"name":"ema_cross","params":{"fast":20,"slow":20}}')
        lines = ["time,open,high,low,close,Volume"]
        for i in range(160):
            price = 5000 + i * 2
            lines.append(f"2024-01-02T{i // 60:02d}:{i % 60:02d}:00Z,{price-1},{price+1},{price-2},{price},10")
        from futuresfund.csv_bars import parse_tradingview_csv
        bars = parse_tradingview_csv("\n".join(lines))
        row = evaluate_rule(bars, "ES", 50000, 3000, name, params)
        self.assertIsNotNone(row)
        self.assertIn("net_profit", row["backtest"])
        book = assemble_book([row], "ES1!", "15m", "APEX4415870000042", 50000, 3000)
        self.assertEqual(book["strategies"][0]["id"], row["id"])

    def test_an_alert_bar_extends_the_series_for_the_next_meeting(self):
        from futuresfund.research import append_bar
        from futuresfund.strategy import series_match
        bars = [{"t": "2024-01-02T14:00:00", "o": 1, "h": 2, "l": 1, "c": 1, "v": 0}]
        extended = append_bar(bars, {"t": "2024-01-02T14:15:00", "o": 2, "h": 3, "l": 2, "c": 2, "v": 0})
        self.assertEqual([item["t"] for item in extended], ["2024-01-02T14:00:00", "2024-01-02T14:15:00"])
        replaced = append_bar(extended, {"t": "2024-01-02T14:15:00", "o": 2, "h": 4, "l": 2, "c": 3, "v": 0})
        self.assertEqual(len(replaced), 2)
        self.assertEqual(replaced[-1]["c"], 3)
        paused = {
            "contract": "ES1!",
            "timeframe": "15m",
            "account": "APEX4415870000042",
            "stance": "paused",
            "proven": False,
            "strategies": [{"id": "rsi", "active": False, "proven": False, "rule": {"name": "rsi_revert"}}],
        }
        signal = parse_alert("account=APEX4415870000042;instrument=ES1!;qty=1;timeframe=15;price=5800;time=2024-01-02T14:15:00Z;")
        ok, _detail = series_match(paused, signal)
        self.assertTrue(ok)
        from futuresfund.strategy import accept_alert
        self.assertFalse(accept_alert(paused, signal)[0])

    def test_active_strategies_must_agree(self):
        from futuresfund.research import combine_signs, merge_active
        self.assertEqual(combine_signs([1, 0]), 1)
        self.assertEqual(combine_signs([0, 0]), 0)
        self.assertIsNone(combine_signs([1, -1]))
        previous = [
            {"id": "ema_cross-fast8-slow34", "proven": True, "active": True},
            {"id": "donchian-length20", "proven": True, "active": False},
        ]
        fresh = [
            {"id": "donchian-length20", "proven": True, "active": True},
            {"id": "ema_cross-fast8-slow34", "proven": True, "active": False},
        ]
        kept = merge_active(previous, fresh, 0)
        flags = {item["id"]: item["active"] for item in kept}
        self.assertTrue(flags["ema_cross-fast8-slow34"])
        self.assertFalse(flags["donchian-length20"])
        dropped = merge_active(previous, [dict(item) for item in fresh], -50)
        flags = {item["id"]: item["active"] for item in dropped}
        self.assertFalse(flags["ema_cross-fast8-slow34"])
        self.assertTrue(flags["donchian-length20"])

    def test_backtest_holds_the_direction_across_the_bars(self):
        from futuresfund.bar_backtest import score_closes
        result = score_closes([100, 110, 105], "Buy", 1, 5)
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["points"], 5)
        self.assertEqual(result["pnl"], 25)
        flat = score_closes([100, 110], "Hold", 1, 5)
        self.assertEqual(flat["pnl"], 0)
        self.assertIsNone(flat["side"])


class StandInTests(unittest.TestCase):
    def test_the_researcher_stands_in_the_most_profitable_rule_that_passed_risk(self):
        from futuresfund.discuss import choose_standin
        weak = {"id": "a", "title": "Weak", "backtest": {"net_profit": 10}}
        strong = {"id": "b", "title": "Strong", "backtest": {"net_profit": 90}}
        self.assertEqual(choose_standin([weak, strong], False)["title"], "Strong")
        self.assertIsNone(choose_standin([strong], True))
        self.assertIsNone(choose_standin([], False))


class EngineTrialTests(unittest.TestCase):
    def test_a_developer_backtest_is_listed_on_the_engine(self):
        import tempfile
        from pathlib import Path

        from futuresfund import lab
        from futuresfund.lab import record_trial

        with tempfile.TemporaryDirectory() as tmp:
            lab.LAB_PATH = Path(tmp) / "lab.json"
            record_trial(
                {"id": "boll", "title": "Bollinger 20 x2", "rule": {"name": "bollinger"}, "proven": False,
                 "backtest": {"net_profit": 32245.0, "max_drawdown": 18969.0, "trades": 767}},
                "Engine test of Bollinger 20 x2.",
            )
            report = lab.load_report()
            self.assertEqual(report["trials"][0]["title"], "Bollinger 20 x2")
            self.assertEqual(report["attempts"], 1)
            self.assertEqual(report["messages"][0]["author"], "2min chart developer")


class RiskGateTests(unittest.TestCase):
    def test_risk_cannot_approve_a_backtest_that_failed_the_trail(self):
        from futuresfund.discuss import _risk_approves
        failed = {"proven": False, "title": "Range break 20"}
        passed = {"proven": True, "title": "Bollinger"}
        self.assertFalse(_risk_approves("APPROVE", failed))
        self.assertFalse(_risk_approves("DENY", passed))
        self.assertTrue(_risk_approves("The drawdown is inside the limit. APPROVE", passed))
        self.assertFalse(_risk_approves("APPROVE", None))


class LeadScriptTests(unittest.TestCase):
    def test_headquarters_keeps_the_most_profitable_script(self):
        import tempfile
        from pathlib import Path

        from futuresfund import book as bookmod
        from futuresfund.strategy import lead_script, store_strategy

        with tempfile.TemporaryDirectory() as tmp:
            bookmod.BOOK_PATH = Path(tmp) / "book.json"
            store_strategy({
                "strategies": [
                    {"id": "a", "title": "Weak", "proven": True, "pine": "// weak", "formula": "weak", "backtest": {"net_profit": 10, "max_drawdown": 1, "trades": 2}},
                    {"id": "b", "title": "Strong", "proven": True, "pine": "// strong", "formula": "strong", "backtest": {"net_profit": 90, "max_drawdown": 2, "trades": 8}},
                    {"id": "c", "title": "Richest", "proven": False, "pine": "// no", "formula": "no", "backtest": {"net_profit": 500, "max_drawdown": 1, "trades": 1}},
                ],
            })
            lead = lead_script()
            self.assertEqual(lead["title"], "Strong")
            self.assertEqual(lead["pine"], "// strong")
            self.assertEqual(lead["trades"], 8)


class LiveStopTests(unittest.TestCase):
    def test_a_finer_alert_is_accepted_and_a_coarser_alert_is_refused(self):
        from futuresfund.strategy import series_match
        strategy = {"contract": "ES1!", "timeframe": "5m", "account": "PA-APEX-1", "strategies": []}
        fine = parse_alert("account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=1;price=5000;time=2024-01-02T14:01:00Z;")
        coarse = parse_alert("account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=15;price=5000;time=2024-01-02T14:00:00Z;")
        self.assertTrue(series_match(strategy, fine)[0])
        self.assertFalse(series_match(strategy, coarse)[0])

    def test_one_minute_alerts_close_one_five_minute_candle(self):
        from futuresfund.research import absorb_alert
        forming = {}
        closed = []
        for minute, price in ((0, 10), (1, 12), (4, 9)):
            bar = {"t": f"2024-01-02T14:{minute:02d}:00", "o": price, "h": price + 1, "l": price - 1, "c": price, "v": 0}
            closed, forming = absorb_alert(forming, bar, "1m", "5m")
            self.assertEqual(closed, [])
        self.assertEqual(forming["bucket"], "2024-01-02T14:00:00")
        self.assertEqual(forming["h"], 13)
        self.assertEqual(forming["l"], 8)
        nxt = {"t": "2024-01-02T14:05:00", "o": 9, "h": 9, "l": 9, "c": 9, "v": 0}
        closed, forming = absorb_alert(forming, nxt, "1m", "5m")
        self.assertEqual(closed[0]["t"], "2024-01-02T14:00:00")
        self.assertEqual(closed[0]["c"], 9)
        self.assertEqual(forming["bucket"], "2024-01-02T14:05:00")

    def test_the_stop_uses_the_bar_extreme_not_only_the_close(self):
        from futuresfund.research import stop_hit
        self.assertTrue(stop_hit(5000, 1, {"h": 5001, "l": 4995, "c": 4998}, 250, 50))
        self.assertFalse(stop_hit(5000, 1, {"h": 5001, "l": 4996, "c": 4996}, 250, 50))
        self.assertTrue(stop_hit(5000, -1, {"h": 5005, "l": 4999, "c": 5001}, 250, 50))

    def test_a_new_candle_reuses_the_saved_signal(self):
        from futuresfund.research import desired_sign, follow_rules
        bars = []
        for i in range(80):
            price = 100 + (i % 7) - 3
            bars.append({"t": f"2024-01-02T{i // 60:02d}:{i % 60:02d}:00", "o": price, "h": price + 1, "l": price - 1, "c": price, "v": 1})
        rule = {"id": "boll", "rule": {"name": "bollinger", "length": 14, "dev": 2}}
        sign, memory = follow_rules(bars, [rule], None)
        self.assertEqual(sign, desired_sign(bars, rule["rule"]))
        extra = dict(bars[-1])
        extra["t"] = "2024-01-02T01:20:00"
        extra["c"] = 80
        extra["l"] = 79
        extra["h"] = 81
        sign, memory = follow_rules(bars + [extra], [rule], memory)
        self.assertEqual(sign, desired_sign(bars + [extra], rule["rule"]))
        self.assertEqual(memory["count"], len(bars) + 1)

    def test_a_one_minute_alert_flattens_at_the_stop_and_does_not_reenter(self):
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
                "account": "PA-APEX-1",
                "qty": 1,
                "working": {"contracts": 1, "entry": 5000},
                "strategies": [{
                    "id": "boll",
                    "title": "Bollinger",
                    "active": True,
                    "proven": True,
                    "rule": {"name": "bollinger", "length": 14, "dev": 2, "stop": 250},
                }],
            })
            board = Board()
            stopped = handle_interval(board, parse_alert(
                "account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=1;price=4998;"
                "open=5000;high=5001;low=4995;time=2024-01-02T14:03:00Z;"
            ))
            self.assertNotEqual(stopped["action"], "close")
            closed = handle_interval(board, parse_alert(
                '{"account":"PA-APEX-1","instrument":"ES1!","action":"SELL","qty":1,'
                '"stop_loss":4995,"price":4995,"timeframe":"5","time":"2024-01-02T14:05:00Z"}'
            ))
            self.assertIn(closed["action"], {"close", "place"})
            floor = [message["text"] for message in board.messages if message["author"] == "Floor Trader"]
            self.assertTrue(any("SELL" in text and "4995" in text for text in floor))
            again = handle_interval(board, parse_alert(
                "account=PA-APEX-1;instrument=ES1!;qty=1;timeframe=5;price=4990;"
                "open=5000;high=5001;low=4988;time=2024-01-02T14:00:00Z;"
            ))
            self.assertIn(again["action"], {"watch", "bar"})


class ScheduleTests(unittest.TestCase):
    def test_saturday_has_no_meeting_and_sunday_evening_is_five(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from futuresfund.session import meeting_slot

        et = ZoneInfo("America/New_York")
        self.assertIsNone(meeting_slot(datetime(2026, 9, 26, 8, 0, tzinfo=et)))
        self.assertIsNone(meeting_slot(datetime(2026, 9, 26, 17, 0, tzinfo=et)))
        self.assertIsNone(meeting_slot(datetime(2026, 9, 27, 8, 0, tzinfo=et)))
        self.assertEqual(meeting_slot(datetime(2026, 9, 27, 17, 0, tzinfo=et)), "5:00pm")
        self.assertEqual(meeting_slot(datetime(2026, 9, 28, 8, 0, tzinfo=et)), "8:00am")
        self.assertIsNone(meeting_slot(datetime(2026, 9, 28, 17, 3, tzinfo=et)))


class ReportTests(unittest.TestCase):
    def test_the_report_uses_book_numbers_and_does_not_send_without_smtp(self):
        from futuresfund.mailer import factual_report, send_report, write_report

        text = factual_report({
            "strategies": [{
                "title": "Bollinger 14",
                "active": True,
                "proven": True,
                "accepted": True,
                "backtest": {"net_profit": 100, "max_drawdown": 40, "trades": 3},
            }],
            "orders": [],
        }, [])
        self.assertIn("Bollinger 14", text)
        self.assertIn("profit 100", text)
        self.assertIn("Saturday has no meeting", text)
        self.assertIn("No account rules have been entered.", text)
        self.assertIn("No trade is open.", text)
        accounted = factual_report({
            "strategies": [],
            "orders": [{"time": "2026-09-28 16:00:00", "account": "APEX-441587-44", "side": "BUY", "instrument": "ES1!", "reason": "paper"}],
            "previous_meeting": "2026-09-28 08:00",
            "account_rules": {"account": "APEX-441587-44", "size": 50000, "profit_target": 3000, "max_drawdown": 2500, "trailing": True, "max_contracts": 10},
            "positions": [],
        }, [])
        self.assertIn("APEX-441587-44", accounted)
        self.assertIn("trailing drawdown 2500", accounted)
        self.assertIn("Orders since the last meeting", accounted)
        self.assertIn("BUY", accounted)
        written = write_report(text, writer=lambda prompt: "The fund is running Bollinger 14.")
        self.assertEqual(written, "The fund is running Bollinger 14.")
        kept = write_report(text, writer=lambda prompt: "The meeting model could not answer: down")
        self.assertEqual(kept, text)
        os.environ.pop("SMTP_HOST", None)
        os.environ.pop("REPORT_TO", None)
        result = send_report(text)
        self.assertFalse(result["sent"])
