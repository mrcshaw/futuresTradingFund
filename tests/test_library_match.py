import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import book as bookmod
from futuresfund import library
from futuresfund.learn import _left_off, _offer_library


def _clear(saved: dict) -> None:
    library._CACHE.clear()
    library._CACHE.update({"stamp": None, "rows": [], "index_mtime": None, "index_path": None})


def _passing(**extra) -> dict:
    row = {
        "engine": "pinets",
        "runner": "pinets",
        "net_profit": 20000.0,
        "max_drawdown": 2000.0,
        "trades": 170,
        "days": 260,
        "win_rate": 60.0,
        "profit_factor": 1.4,
        "profit_ratio": 1.2,
        "biggest_loser": -400.0,
        "instrument": "GC",
        "contract": "GC1!",
        "timeframe": "5m",
        "params": {"ema_len": 50},
    }
    row.update(extra)
    return row


class LibraryMatchTests(unittest.TestCase):
    def setUp(self):
        self.saved_notes = library._NOTES
        self.saved_cache = dict(library._CACHE)
        self.tmp = tempfile.TemporaryDirectory()
        library._NOTES = Path(self.tmp.name)
        _clear(self.saved_cache)
        self.pine = mock.patch("futuresfund.strategy.pine_text", return_value="// @version=5\nstrategy(\"Held\")\n")
        self.pine.start()
        # A book of the test's own, so the $20,000 run clears the profit line whatever the owner's boxes say.
        self.book = mock.patch.object(bookmod, "BOOK_PATH", Path(self.tmp.name) / "book.json")
        self.book.start()
        bookmod.save_strategy_acceptance({"min_profit": 14000, "profit_factor": 1.1, "win_rate": 48, "min_trades": 100})

    def tearDown(self):
        self.book.stop()
        self.pine.stop()
        library._NOTES = self.saved_notes
        library._CACHE.clear()
        library._CACHE.update(self.saved_cache)
        self.tmp.cleanup()

    def test_an_empty_library_does_not_claim_a_stronger_version(self):
        reason = _left_off(_passing(), False, "EMA Trend RSI Pullback Vol", "GC1!")
        self.assertNotIn("stronger version", reason)
        self.assertIn("No version of this name is on this contract and timeframe.", reason)

    def test_the_reason_quotes_the_profit_actually_on_file(self):
        library._write_untested([
            {
                "id": "held",
                "script": "EMA Trend RSI Pullback Vol",
                "title": "EMA Trend RSI Pullback Vol",
                "file": "held.pine",
                "contract": "GC1!",
                "timeframe": "5m",
                "net_profit": 40000.0,
                "trades": 160,
                "days": 260,
                "kind": "Not tested on the full dataset",
                "full_dataset": False,
                "window": "one year",
            }
        ])
        reason = _left_off(_passing(), False, "EMA Trend RSI Pullback Vol", "GC1!")
        self.assertIn("A stronger version of this name is already on this contract and timeframe.", reason)
        self.assertIn("$40,000.00", reason)
        self.assertIn("$20,000.00", reason)

        library._write_untested([
            {
                "id": "weaker",
                "script": "EMA Trend RSI Pullback Vol",
                "title": "EMA Trend RSI Pullback Vol",
                "file": "weaker.pine",
                "contract": "GC1!",
                "timeframe": "5m",
                "net_profit": 10000.0,
                "trades": 140,
                "days": 260,
                "kind": "Not tested on the full dataset",
                "full_dataset": False,
                "window": "one year",
            }
        ])
        _clear(self.saved_cache)
        weaker = _left_off(_passing(), False, "EMA Trend RSI Pullback Vol", "GC1!")
        self.assertNotIn("A stronger version", weaker)
        self.assertIn("$10,000.00", weaker)
        self.assertIn("No stronger version is on this contract and timeframe.", weaker)

    def test_a_pinets_run_that_clears_acceptance_is_filed_on_the_waiting_list(self):
        _offer_library(None, {"title": "EMA Trend RSI Pullback Vol"}, Path("ema_trend.pine"), _passing())
        waiting = library._load_untested()
        self.assertEqual([row["script"] for row in waiting], ["EMA Trend RSI Pullback Vol"])
        self.assertEqual(waiting[0]["contract"], "GC1!")
        self.assertEqual(waiting[0]["net_profit"], 20000.0)
        self.assertEqual(library._load_index_rows(), [])

    def test_recover_adds_the_passing_run_and_leaves_the_failing_run_off(self):
        payload = {
            "title": "EMA Trend RSI Pullback Vol",
            "file": "created_ema_trend.pine",
            "trials": [
                _passing(net_profit=33735.0, max_drawdown=5725.5, trades=155, profit_factor=1.488, profit_ratio=1.163, win_rate=56.13, biggest_loser=-700.0),
                _passing(net_profit=100.0, profit_factor=0.4, win_rate=20.0, trades=10),
            ],
        }
        (library._NOTES / "created_ema_trend.json").write_text(json.dumps(payload), encoding="utf-8")
        report = library.recover_passed()
        waiting = library._load_untested()
        self.assertEqual([row["net_profit"] for row in waiting], [33735.0])
        self.assertEqual(report["filed"][0]["contract"], "GC1!")
        self.assertEqual(report["filed"][0]["timeframe"], "5m")
        self.assertEqual(library._load_index_rows(), [])

    def test_the_waiting_list_records_when_a_script_was_added_and_counts_each_instrument(self):
        library._write_untested([
            {"id": "gold-a", "script": "Gold A", "title": "Gold A", "file": "gold_a.pine", "contract": "GC1!", "contract_label": "Gold", "timeframe": "5m", "net_profit": 9000, "trades": 90, "days": 260, "kind": "Not tested on the full dataset", "full_dataset": False, "window": "one year"},
            {"id": "gold-b", "script": "Gold B", "title": "Gold B", "file": "gold_b.pine", "contract": "GC1!", "contract_label": "Gold", "timeframe": "15m", "net_profit": 10000, "trades": 100, "days": 260, "kind": "Not tested on the full dataset", "full_dataset": False, "window": "one year"},
            {"id": "nq-a", "script": "Nasdaq A", "title": "Nasdaq A", "file": "nq_a.pine", "contract": "NQ1!", "contract_label": "Nasdaq", "timeframe": "5m", "net_profit": 11000, "trades": 170, "days": 260, "kind": "Not tested on the full dataset", "full_dataset": False, "window": "one year"},
        ])
        shelves = {item["contract"]: item for item in library._shelves()}
        self.assertEqual(shelves["GC1!"]["untested"], 2)
        self.assertEqual(shelves["NQ1!"]["untested"], 1)
        self.assertEqual(shelves["ES1!"]["untested"], 0)
        self.assertEqual(shelves["GC1!"]["count"], 0)
        waiting = library._load_untested()
        self.assertTrue(all(row.get("added_at") for row in waiting))
        kept = waiting[0]["added_at"]
        library._untested_rows()
        again = library._load_untested()
        self.assertEqual(again[0]["added_at"], kept)


class PointValueTests(unittest.TestCase):
    SCRIPT = 'pv                = input.float(2, "Point Value ($)")\nstop = close - 700 / pv\n'

    def test_a_script_written_for_mnq_opens_with_the_contract_point_value(self):
        self.assertIn("input.float(100,", library.for_contract(self.SCRIPT, "GC1!"))
        self.assertIn("input.float(50,", library.for_contract(self.SCRIPT, "ES1!"))
        self.assertIn("input.float(20,", library.for_contract(self.SCRIPT, "NQ"))
        self.assertIn("input.float(2,", library.for_contract(self.SCRIPT, "MNQ1!"))

    def test_an_unknown_contract_uses_the_es_value(self):
        self.assertIn("input.float(50,", library.for_contract(self.SCRIPT, "???"))


class SmallContractTests(unittest.TestCase):
    def test_m2k_and_ym_have_their_own_shelves(self):
        self.assertEqual(library.contract_of("Any", "", "M2K1!"), ("M2K1!", "Micro Russell"))
        self.assertEqual(library.contract_of("Any", "", "YM1!"), ("YM1!", "Dow"))
        self.assertEqual(library.contract_of("Breakout m2k 5min"), ("M2K1!", "Micro Russell"))
        self.assertIn("input.float(5,", library.for_contract(PointValueTests.SCRIPT, "M2K1!"))
        self.assertIn("input.float(5,", library.for_contract(PointValueTests.SCRIPT, "YM1!"))

    def test_the_engines_use_the_contract_tick(self):
        from futuresfund.pine_runtime import _TICKER

        self.assertEqual(_TICKER["YM"], "CBOT_MINI:YM1!")
        self.assertEqual(_TICKER["M2K"], "CME_MINI:M2K1!")
