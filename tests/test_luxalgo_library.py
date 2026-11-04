"""The LuxAlgo library service the strategy developers read before they write."""

import json
import unittest
from unittest import mock

from futuresfund.luxalgo_library import LibraryError, concept, indicator, search, strategy_note


def _sse(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


SEARCH = {
    "results": [
        {
            "kind": "concept",
            "slug": "rsi",
            "name": "RSI",
            "family": "momentum",
            "url": "https://www.luxalgo.com/library/concept/rsi/",
        },
        {
            "kind": "indicator",
            "slug": "adaptive-bounds-rsi",
            "name": "Adaptive Bounds RSI",
            "family": "momentum",
            "description": "Bounds that move with RSI.",
        },
        {"kind": "concept", "slug": "", "name": "Missing"},
        {"kind": "note", "slug": "other", "name": "Other"},
    ]
}

CONCEPT = {
    "slug": "rsi",
    "name": "RSI",
    "family": "momentum",
    "content_markdown": (
        "# RSI\n\nRSI compares average gains with average losses. "
        "Readings above 70 are [overbought](https://www.luxalgo.com/library/concept/overbought-oversold/). "
        "A cross back through 50 can be the trigger."
    ),
}

INDICATOR = {
    "slug": "adaptive-bounds-rsi",
    "name": "Adaptive Bounds RSI",
    "family": "momentum",
    "body_markdown": (
        "Bullish marker: RSI crosses below the lower bound. "
        "Bearish marker: RSI crosses above the upper bound. "
        "This is an indicator. It has no strategy.entry and no strategy.exit."
    ),
}


class LibraryResponseTests(unittest.TestCase):
    def test_a_search_keeps_only_named_concepts_and_indicators(self):
        with mock.patch("futuresfund.luxalgo_library.call_tool", return_value=_sse(SEARCH)):
            rows = search("rsi")
        self.assertEqual([row["slug"] for row in rows], ["rsi", "adaptive-bounds-rsi"])
        self.assertEqual([row["kind"] for row in rows], ["concept", "indicator"])

    def test_a_rejected_page_is_not_given_to_the_writer(self):
        refused = {"content": [{"type": "text", "text": '{"error":"Unknown concept slug."}'}], "isError": True}
        with mock.patch("futuresfund.luxalgo_library.call_tool", return_value=refused):
            with self.assertRaises(LibraryError) as caught:
                concept("missing")
        self.assertIn("Unknown concept", str(caught.exception))

    def test_a_concept_without_an_explanation_is_refused(self):
        page = {"slug": "rsi", "name": "RSI", "content_markdown": ""}
        with mock.patch("futuresfund.luxalgo_library.call_tool", return_value=_sse(page)):
            with self.assertRaises(LibraryError):
                concept("rsi")

    def test_an_indicator_without_a_description_is_refused(self):
        page = {"slug": "rsi", "name": "RSI"}
        with mock.patch("futuresfund.luxalgo_library.call_tool", return_value=_sse(page)):
            with self.assertRaises(LibraryError):
                indicator("rsi")

    def test_a_library_page_becomes_a_strategy_with_an_entry_and_an_exit(self):
        def call_tool(name, arguments, timeout=12):
            if name == "library_search":
                return _sse(SEARCH)
            if name == "library_get_concept":
                return _sse(CONCEPT)
            if name == "library_get_indicator":
                return _sse(INDICATOR)
            raise AssertionError(name)

        with mock.patch("futuresfund.luxalgo_library.call_tool", call_tool):
            note = strategy_note("rsi", kind="concepts")
        self.assertIn("Concept: RSI (momentum)", note)
        self.assertNotIn("Indicator:", note)
        self.assertIn("The page is a concept.", note)
        self.assertIn("overbought", note)
        self.assertNotIn("https://", note)
        self.assertIn("It is not a strategy.", note)
        self.assertIn("strategy.entry", note)
        self.assertIn("strategy.exit", note)
        self.assertIn("stop_dollars", note)
        self.assertIn("in_session", note)
        self.assertNotIn("indicator()", note.split("Do not copy", 1)[0])

    def test_indicators_are_used_when_that_choice_is_selected(self):
        calls = []

        def call_tool(name, arguments, timeout=12):
            calls.append((name, arguments.get("type") or arguments.get("slug")))
            if name == "library_search":
                return _sse(SEARCH)
            if name == "library_get_indicator":
                return _sse(INDICATOR)
            raise AssertionError(name)

        with mock.patch("futuresfund.luxalgo_library.call_tool", call_tool):
            note = strategy_note("rsi", kind="indicators")
        self.assertEqual(calls[0], ("library_search", "indicators"))
        self.assertEqual(calls[1][0], "library_get_indicator")
        self.assertIn("Indicator: Adaptive Bounds RSI (momentum)", note)
        self.assertNotIn("Concept:", note)
        self.assertIn("strategy.entry", note)
        self.assertIn("strategy.exit", note)

    def test_the_saved_choice_is_concepts_until_indicators_are_selected(self):
        import tempfile
        from pathlib import Path

        from futuresfund import luxalgo_library

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "luxalgo_library.json"
            with mock.patch.object(luxalgo_library, "CHOICE_PATH", path):
                self.assertEqual(luxalgo_library.library_kind(), "concepts")
                self.assertEqual(luxalgo_library.set_library_kind("indicators"), "indicators")
                self.assertEqual(luxalgo_library.library_kind(), "indicators")
                self.assertEqual(luxalgo_library.set_library_kind("concepts"), "concepts")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["kind"], "concepts")
                with self.assertRaises(LibraryError):
                    luxalgo_library.set_library_kind("both")


class LiveLibraryTests(unittest.TestCase):
    def test_the_server_returns_a_concept_and_an_indicator_that_can_become_a_strategy(self):
        rows = search("rsi", limit=3, timeout=20)
        self.assertTrue(any(row["kind"] == "concept" and row["slug"] == "rsi" for row in rows))
        self.assertTrue(any(row["kind"] == "indicator" and row["slug"] and row["name"] for row in rows))
        page = concept("rsi", timeout=20)
        self.assertEqual(page["slug"], "rsi")
        self.assertIn("RSI", page["name"])
        self.assertIn("Relative Strength", page["content_markdown"])
        self.assertNotIn("strategy.entry", page["content_markdown"])
        note = strategy_note("rsi", timeout=20, kind="concepts")
        self.assertIn("Concept: RSI", note)
        self.assertNotIn("Indicator:", note)
        self.assertIn("It is not a strategy.", note)
        self.assertIn("strategy.entry", note)
        self.assertIn("strategy.exit", note)
        self.assertIn("stop_dollars", note)
