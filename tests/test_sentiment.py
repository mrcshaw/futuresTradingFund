"""The gauge reads the last closed 18:00–16:00 session and the political and energy headlines."""

import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from futuresfund.sentiment import FEEDS, analyze, last_full_session, load_headlines

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "futuresfund" / "static"


def session_bars(now, closes):
    start, _end = last_full_session(now)
    rows = []
    price = closes[0]
    for index, close in enumerate(closes):
        rows.append({
            "t": int(start.timestamp()) + index * 900,
            "o": price,
            "h": max(price, close) + 0.5,
            "l": min(price, close) - 0.5,
            "c": close,
            "v": 1,
        })
        price = close
    return rows


class SessionTests(unittest.TestCase):
    def test_thursday_evening_uses_the_session_that_closed_at_1600(self):
        start, end = last_full_session(datetime(2026, 10, 8, 20, 15, tzinfo=ET))
        self.assertEqual((start, end), (
            datetime(2026, 10, 7, 18, 0, tzinfo=ET),
            datetime(2026, 10, 8, 16, 0, tzinfo=ET),
        ))

    def test_monday_morning_uses_the_friday_close(self):
        start, end = last_full_session(datetime(2026, 10, 5, 10, 0, tzinfo=ET))
        self.assertEqual(end, datetime(2026, 10, 2, 16, 0, tzinfo=ET))
        self.assertEqual(start, datetime(2026, 10, 1, 18, 0, tzinfo=ET))

    def test_the_weekend_stays_on_the_friday_close(self):
        start, end = last_full_session(datetime(2026, 10, 10, 12, 0, tzinfo=ET))
        self.assertEqual(end, datetime(2026, 10, 9, 16, 0, tzinfo=ET))
        self.assertEqual(start, datetime(2026, 10, 8, 18, 0, tzinfo=ET))


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 8, 20, 15, tzinfo=ET)

    def test_a_rally_and_easier_headlines_are_bullish(self):
        bars = session_bars(self.now, [100 + i * 0.1 for i in range(20)])
        headlines = [
            {"source": "Federal Reserve press", "kind": "political", "title": "The Fed delivers a rate cut"},
            {"source": "EIA Today in Energy", "kind": "energy", "title": "Crude inventories rose and oil falls"},
            {"source": "Google News politics", "kind": "political", "title": "A warning on the calendar"},
        ]
        row = analyze(bars, headlines, self.now)
        self.assertEqual(row["band"], "Bullish")
        self.assertGreater(row["score"], 6.5)
        self.assertIn("up", row["bull"]["text"])
        self.assertIn("rate cut", row["bull"]["text"])
        self.assertIn("does not support a bearish case", row["bear"]["text"])
        self.assertIn("not an order", row["risk"]["text"])
        self.assertEqual(row["session"]["change"], 1.9)
        self.assertEqual(row["confidence"], "high")

    def test_a_selloff_and_tighter_headlines_are_bearish(self):
        bars = session_bars(self.now, [100, 99, 98, 97])
        headlines = [
            {"source": "Google News politics", "kind": "political", "title": "New sanctions after the invasion"},
            {"source": "Google News energy", "kind": "energy", "title": "OPEC cut sends an oil surge"},
        ]
        row = analyze(bars, headlines, self.now)
        self.assertEqual(row["band"], "Bearish")
        self.assertLess(row["score"], 3.4)
        self.assertIn("sanctions", row["bear"]["text"])
        self.assertIn("oil surge", row["bear"]["text"])
        self.assertIn("does not support a bullish case", row["bull"]["text"])

    def test_bars_outside_the_session_are_not_part_of_the_read(self):
        bars = session_bars(self.now, [100, 110])
        bars.append({"t": int(datetime(2026, 10, 6, 12, tzinfo=ET).timestamp()), "o": 50, "h": 50, "l": 50, "c": 50, "v": 1})
        row = analyze(bars, [], self.now)
        self.assertEqual(row["session"]["bars"], 2)
        self.assertEqual(row["session"]["open"], 100)

    def test_no_bars_and_no_headlines_stay_neutral(self):
        row = analyze([], [], self.now)
        self.assertEqual((row["band"], row["score"], row["confidence"]), ("Neutral", 5, "low"))
        self.assertIn("no 15-minute bars", row["risk"]["text"])

    def test_a_feed_that_fails_is_skipped_and_the_others_are_kept(self):
        xml = "<rss><channel><title>Feed</title><item><title>Congress reaches a trade deal</title></item></channel></rss>"

        def get(url):
            if "federalreserve" in url:
                raise OSError("down")
            return xml

        rows = load_headlines(get)
        self.assertTrue(any(row["title"] == "Congress reaches a trade deal" for row in rows))
        self.assertTrue(all(row["source"] != "Federal Reserve press" for row in rows))
        self.assertEqual({row["kind"] for row in rows}, {"political", "energy", "market"})


class PageTests(unittest.TestCase):
    def test_the_gauge_sits_in_the_middle_of_the_top_panel(self):
        html = (PAGE / "index.html").read_text(encoding="utf-8")
        css = (PAGE / "styles.css").read_text(encoding="utf-8")
        js = (PAGE / "app.js").read_text(encoding="utf-8")
        top = html.split('<header class="top">', 1)[1].split("</header>", 1)[0]
        self.assertLess(top.index("sentiment-gauge"), top.index("desk-clock"))
        self.assertLess(top.index("channel-title"), top.index("sentiment-gauge"))
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)", css)
        self.assertIn("gauge-arc-bear", html)
        self.assertIn("gauge-arc-mid", html)
        self.assertIn("gauge-arc-bull", html)
        self.assertIn("gauge-value", html)
        self.assertIn("stroke: #ef6a5a", css)
        self.assertIn("stroke: #e6c15a", css)
        self.assertIn("stroke: #3dd68c", css)
        self.assertIn('fetch("/api/sentiment")', js)
        self.assertIn("gauge-delta", js)
        names = {row["name"] for row in FEEDS}
        self.assertEqual(names, {"Federal Reserve press", "Google News politics", "EIA Today in Energy", "Google News energy"})
        config = (ROOT.parent / "hedgeFund" / "tradingagents" / "default_config.py").read_text(encoding="utf-8")
        self.assertIn("US politics Congress White House sanctions election", config)
        self.assertIn("crude oil OPEC natural gas EIA inventory energy", config)


if __name__ == "__main__":
    unittest.main()
