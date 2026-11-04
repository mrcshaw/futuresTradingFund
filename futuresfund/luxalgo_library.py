"""The LuxAlgo library, for the strategy developers.

The hosted server at https://mcp.luxalgo.com/mcp returns concepts and indicators.
Those pages are not strategies. This module checks the reply, then writes a short
note that tells the developer to add this desk's entry and dollar exit.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://mcp.luxalgo.com/mcp"
CHOICES = ("concepts", "indicators")
CHOICE_PATH = Path.home() / ".futuresfund" / "luxalgo_library.json"
CONTEXT = (
    "Retrieving a public trading concept and indicator so a research desk can write a strategy with an entry and an exit."
)
TOPICS = (
    "rsi",
    "ema crossover",
    "vwma",
    "bollinger",
    "macd",
    "stochastic",
    "swing high",
    "liquidity sweep",
)
_LOCK = threading.Lock()
_CURSOR = 0


class LibraryError(RuntimeError):
    """The library server answered, but the page cannot be used."""


def next_topic() -> str:
    """The next search, so one idea is not written on every new script."""
    global _CURSOR
    with _LOCK:
        topic = TOPICS[_CURSOR % len(TOPICS)]
        _CURSOR += 1
        return topic


def search(query: str, kind: str = "all", limit: int = 5, timeout: float = 12) -> list[dict]:
    """Concepts and indicators whose names match the query."""
    payload = _json_page(
        call_tool(
            "library_search",
            {"query": query, "type": kind, "limit": limit, "context": CONTEXT},
            timeout=timeout,
        )
    )
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise LibraryError("The library search did not return a results list.")
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") not in {"concept", "indicator"}:
            continue
        if not str(row.get("slug") or "").strip() or not str(row.get("name") or "").strip():
            continue
        kept.append(row)
    return kept


def concept(slug: str, timeout: float = 12) -> dict:
    """One concept page. The markdown explains the idea. It does not enter or exit."""
    page = _json_page(call_tool("library_get_concept", {"slug": slug, "context": CONTEXT}, timeout=timeout))
    for key in ("slug", "name", "content_markdown"):
        if not str(page.get(key) or "").strip():
            raise LibraryError(f"The concept page is missing {key}.")
    return page


def indicator(slug: str, timeout: float = 12) -> dict:
    """One indicator page. The description is how to read it, not a strategy."""
    page = _json_page(call_tool("library_get_indicator", {"slug": slug, "context": CONTEXT}, timeout=timeout))
    if not str(page.get("slug") or "").strip() or not str(page.get("name") or "").strip():
        raise LibraryError("The indicator page is missing its name.")
    if not str(page.get("body_markdown") or page.get("description") or "").strip():
        raise LibraryError("The indicator page has no description.")
    return page


def library_kind() -> str:
    """The R&D choice: concepts or indicators. Concepts is the choice until one is saved."""
    try:
        saved = json.loads(CHOICE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return "concepts"
    kind = str(saved.get("kind") or "") if isinstance(saved, dict) else ""
    return kind if kind in CHOICES else "concepts"


def set_library_kind(kind: str) -> str:
    """Save the R&D choice. The next strategy uses that kind of library page."""
    choice = str(kind or "").strip().lower()
    if choice not in CHOICES:
        raise LibraryError("Choose concepts or indicators.")
    CHOICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CHOICE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"kind": choice}) + "\n", encoding="utf-8")
    temporary.replace(CHOICE_PATH)
    return choice


def strategy_note(query: str | None = None, timeout: float = 12, kind: str | None = None) -> str:
    """A checked library page, with the entry and the exit the page does not have.

    kind is concepts or indicators. When it is omitted, the saved R&D choice is used.
    """
    chosen = library_kind() if kind is None else _choice(kind)
    topic = (query or next_topic()).strip()
    if not topic:
        raise LibraryError("The library search has no topic.")
    rows = search(topic, kind=chosen, limit=4, timeout=timeout)
    if not rows:
        raise LibraryError(f"The library search for {topic} returned no pages.")
    lines = [
        "LuxAlgo library. Use this idea in a new strategy. "
        + ("The page is a concept. It is not a strategy." if chosen == "concepts" else "The page is an indicator. It is not a strategy."),
    ]
    if chosen == "concepts":
        row = next((item for item in rows if item["kind"] == "concept"), None)
        if row is None:
            raise LibraryError(f"The library search for {topic} returned no concept.")
        page = concept(str(row["slug"]), timeout=timeout)
        family = page.get("family") or row.get("family") or ""
        label = f"{page['name']} ({family})" if family else page["name"]
        lines.append(f"Concept: {label}. {_excerpt(page['content_markdown'])}")
        named = "concept"
    else:
        row = next((item for item in rows if item["kind"] == "indicator"), None)
        if row is None:
            raise LibraryError(f"The library search for {topic} returned no indicator.")
        page = indicator(str(row["slug"]), timeout=timeout)
        family = page.get("family") or row.get("family") or ""
        label = f"{page['name']} ({family})" if family else page["name"]
        body = page.get("body_markdown") or page.get("description") or ""
        lines.append(f"Indicator: {label}. {_excerpt(body)}")
        named = "indicator"
    lines.append(
        "Add the entry and the exit. The entry is strategy.entry, with at least two agreeing conditions, "
        "a filter and a trigger, and only while in_session. "
        "The exit is the desk exit: stop_dollars and tp_dollars, "
        "long_stop = strategy.position_size > 0 ? strategy.position_avg_price - stop_dollars / pv : close - stop_dollars / pv, long_limit = close + tp_dollars / pv, "
        "short_stop = strategy.position_size < 0 ? strategy.position_avg_price + stop_dollars / pv : close + stop_dollars / pv, short_limit = close - tp_dollars / pv, "
        "and strategy.exit with those stop and limit prices. "
        "When strategy.openprofit is at or below minus stop_dollars, strategy.close_all. "
        f"Name the library {named} in the Built from comment. "
        "Do not copy an indicator() declaration, a plot, an alert, a table, or request.security. "
        "Use a built-in the engine runs, such as ta.rsi, ta.ema, ta.sma, ta.vwma, ta.atr, ta.macd, ta.bb, or ta.stoch."
    )
    return "\n".join(lines)


def call_tool(name: str, arguments: dict, timeout: float = 12) -> dict:
    """One tools/call on the hosted library server. The body is the tool result."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise LibraryError(f"The library server returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise LibraryError("The library server did not answer.") from exc
    except TimeoutError as exc:
        raise LibraryError("The library server did not answer.") from exc
    return _message(raw)


def _message(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise LibraryError("The library server returned no message.")
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LibraryError("The library server did not return JSON.") from exc
    else:
        data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not data_lines:
            raise LibraryError("The library server returned no message.")
        try:
            body = json.loads(data_lines[-1])
        except json.JSONDecodeError as exc:
            raise LibraryError("The library server did not return JSON.") from exc
    if not isinstance(body, dict):
        raise LibraryError("The library server did not return JSON.")
    error = body.get("error")
    if isinstance(error, dict):
        raise LibraryError(str(error.get("message") or "The library server returned an error."))
    result = body.get("result")
    if not isinstance(result, dict):
        raise LibraryError("The library server returned no result.")
    if result.get("isError"):
        detail = _content_text(result)
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("error"):
            raise LibraryError(str(parsed["error"]))
        raise LibraryError(detail or "The library server rejected the request.")
    return result


def _content_text(result: dict) -> str:
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise LibraryError("The library server returned no text.")
    text = content[0].get("text")
    if not isinstance(text, str) or not text.strip():
        raise LibraryError("The library server returned no text.")
    return text


def _json_page(result: dict) -> dict:
    text = _content_text(result)
    try:
        page = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LibraryError("The library page was not JSON.") from exc
    if not isinstance(page, dict):
        raise LibraryError("The library page was not a record.")
    if page.get("error"):
        raise LibraryError(str(page["error"]))
    return page


def _choice(kind: str) -> str:
    choice = str(kind or "").strip().lower()
    if choice not in CHOICES:
        raise LibraryError("Choose concepts or indicators.")
    return choice


def _excerpt(markdown: str, limit: int = 700) -> str:
    text = markdown or ""
    while "[" in text and "](" in text:
        start = text.find("[")
        middle = text.find("](", start)
        end = text.find(")", middle + 2) if middle != -1 else -1
        if start == -1 or middle == -1 or end == -1:
            break
        text = text[:start] + text[start + 1 : middle] + text[end + 1 :]
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit].rsplit(" ", 1)[0].rstrip(".,;:")
    return cut + "…"
