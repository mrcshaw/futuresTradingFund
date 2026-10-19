"""A bull/bear read of the session being traded, plus political, energy, and market headlines.

The last full session is 18:00 ET to 16:00 ET the next day, the one that has
already closed. When a new session is open, that session's bars lead the gauge,
so a selloff after 18:00 is not hidden by the prior close. The bull researcher
and the bear researcher each make a case from that chart and from the headlines.
The risk researcher weighs the two and leaves one score for the gauge. Nothing
here places an order.

Political headlines come from the Federal Reserve press feed and a Google News
search of Congress, the White House, and sanctions. Energy headlines come from
the EIA Today in Energy feed and a Google News search of crude, OPEC, natural
gas, and inventories. Market headlines come from a Google News search of equity
selloffs and IPO valuation cuts, including the Anthropic share sale. No key is
required.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as XML
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_CACHE_SECONDS = 300
_cache: dict = {"at": 0.0, "payload": None}

FEEDS = (
    {
        "name": "Federal Reserve press",
        "kind": "political",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
    },
    {
        "name": "Google News politics",
        "kind": "political",
        "url": "https://news.google.com/rss/search?q=White+House+OR+Congress+OR+sanctions+OR+geopolitics+when:1d&hl=en-US&gl=US&ceid=US:en",
    },
    {
        "name": "EIA Today in Energy",
        "kind": "energy",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
    },
    {
        "name": "Google News energy",
        "kind": "energy",
        "url": "https://news.google.com/rss/search?q=crude+oil+OR+OPEC+OR+%22natural+gas%22+OR+%22oil+inventory%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    },
    {
        "name": "Google News markets",
        "kind": "market",
        "url": "https://news.google.com/rss/search?q=Anthropic+IPO+valuation+OR+%22tech+selloff%22+OR+%22stock+market+selloff%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    },
)

_BULL_POLITICAL = ("rate cut", "cuts rates", "stimulus", "ceasefire", "trade deal", "soft landing")
_BEAR_POLITICAL = ("shutdown", "sanction", "tariff", "invasion", "missile", "war", "default")
_BULL_ENERGY = ("inventory build", "inventories rose", "inventories rise", "oil falls", "oil drops", "crude falls", "crude drops", "supply increase")
_BEAR_ENERGY = ("oil surge", "oil jumps", "oil rises", "crude rises", "crude jumps", "opec cut", "production cut", "supply disruption", "pipeline", "inventory draw")
_BULL_MARKET = ("stocks rally", "buy the dip", "market rally", "shares rise", "record high")
_BEAR_MARKET = (
    "selloff", "sell-off", "sell off",
    "lower valuation", "ipo valuation", "valuation expectations",
    "revaluation", "revaluing", "tech rout", "stocks tumble", "stocks slide", "market slide",
)


def last_full_session(now: datetime) -> tuple[datetime, datetime]:
    """The latest 18:00–16:00 ET session that has already closed."""
    now = now.astimezone(ET)
    day = now.date()
    close = datetime.combine(day, clock(16, 0), tzinfo=ET)
    if now < close or day.weekday() >= 5:
        day = day - timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        close = datetime.combine(day, clock(16, 0), tzinfo=ET)
    start = datetime.combine(close.date() - timedelta(days=1), clock(18, 0), tzinfo=ET)
    return start, close


def session_in_progress(now: datetime) -> tuple[datetime, datetime] | None:
    """The 18:00 session still trading. None during the 16:00–18:00 halt and on a closed weekend."""
    now = now.astimezone(ET)
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 18):
        return None
    start = datetime.combine(now.date(), clock(18, 0), tzinfo=ET)
    if now < start:
        start -= timedelta(days=1)
    close = datetime.combine(start.date() + timedelta(days=1), clock(16, 0), tzinfo=ET)
    if now >= close:
        return None
    return start, now


def analyze(bars: list[dict], headlines: list[dict], now: datetime | None = None) -> dict:
    """Bull, bear, and risk, from one session's bars and the headlines already fetched."""
    now = now.astimezone(ET) if now is not None else datetime.now(ET)
    start, end = last_full_session(now)
    live = session_in_progress(now)
    live_window = _window(bars, *live) if live is not None else []
    if len(live_window) >= 2:
        window = live_window
        session_start, session_end = live
        live_read = True
    else:
        window = _window(bars, start, end)
        session_start, session_end = start, end
        live_read = False
    bull_points, bear_points, chart = _chart_case(window)
    chart["live"] = live_read
    news_bull, news_bear, kept = _news_case(headlines)
    bull = round(bull_points + news_bull, 2)
    bear = round(bear_points + news_bear, 2)
    score = round(min(10.0, max(0.0, 5 + bull - bear)), 2)
    band = _band(score, bull, bear)
    return {
        "score": score,
        "band": band,
        "confidence": _confidence(window, kept),
        "session": {
            "start": session_start.strftime("%Y-%m-%d %H:%M"),
            "end": session_end.strftime("%Y-%m-%d %H:%M"),
            "bars": len(window),
            **chart,
        },
        "bull": _voice("Bull researcher", _bull_text(chart, kept, bull_points, news_bull)),
        "bear": _voice("Bear researcher", _bear_text(chart, kept, bear_points, news_bear)),
        "risk": _voice("Risk", _risk_text(band, score, bull, bear, chart, kept)),
        "headlines": kept[:8],
        "sources": [{"name": row["name"], "kind": row["kind"], "url": row["url"]} for row in FEEDS],
    }


def current_gauge(now: datetime | None = None) -> dict:
    """The gauge for the contract a trader is executing, or NQ. Headlines are reused for five minutes."""
    now = now.astimezone(ET) if now is not None else datetime.now(ET)
    if _cache["payload"] is not None and time.time() - _cache["at"] < _CACHE_SECONDS:
        return _cache["payload"]
    root = _root()
    try:
        from futuresfund.live_trading import _execution_series

        bars = _execution_series(root, "15m")
    except Exception as exc:
        bars = []
        chart_error = str(exc)
    else:
        chart_error = ""
    headlines = load_headlines()
    payload = analyze(bars, headlines, now)
    payload["instrument"] = root
    if chart_error:
        payload["chart_error"] = chart_error
    _cache["at"] = time.time()
    _cache["payload"] = payload
    return payload


def load_headlines(get=None) -> list[dict]:
    """Titles from the political and energy feeds. A feed that does not answer is skipped."""
    fetch = get or _http_get
    found: list[dict] = []

    def one(feed: dict) -> list[dict]:
        try:
            text = fetch(feed["url"])
        except Exception:
            return []
        return [{"source": feed["name"], "kind": feed["kind"], "title": title} for title in _titles(text)]

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        for rows in pool.map(one, FEEDS):
            found.extend(rows)
    return found


def _http_get(url: str) -> str:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "futures-desk/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.read().decode("utf-8", errors="replace")


def _titles(xml_text: str) -> list[str]:
    try:
        root = XML.fromstring(xml_text)
    except XML.ParseError:
        return []
    titles = []
    for node in root.iter():
        if not str(node.tag).lower().endswith("title") or not node.text:
            continue
        title = " ".join(node.text.split())
        if title:
            titles.append(title)
    return titles[1:7]


def _root() -> str:
    try:
        from futuresfund.book import load
        from futuresfund.live_trading import _slots

        slots = _slots(load())
    except Exception:
        return "NQ"
    for slot in slots:
        if slot.get("root"):
            return str(slot["root"])
    return "NQ"


def _window(bars: list[dict], start: datetime, end: datetime) -> list[dict]:
    opened, closed = int(start.timestamp()), int(end.timestamp())
    return [row for row in bars if opened <= int(row.get("t") or 0) < closed]


def _chart_case(window: list[dict]) -> tuple[float, float, dict]:
    if not window:
        return 0.0, 0.0, {"open": None, "high": None, "low": None, "close": None, "change": None, "location": None}
    first, last = window[0], window[-1]
    high = max(float(row["h"]) for row in window)
    low = min(float(row["l"]) for row in window)
    opened, closed = float(first["o"]), float(last["c"])
    span = high - low
    location = (closed - low) / span if span else 0.5
    change = round(closed - opened, 2)
    bull = bear = 0.0
    if opened:
        move = (closed - opened) / opened * 100
        if move > 0:
            bull += min(3.0, move)
        elif move < 0:
            bear += min(3.0, -move)
    if location >= 0.66:
        bull += 1.5
    elif location <= 0.33:
        bear += 1.5
    mid = window[len(window) // 2]
    if float(last["c"]) > float(mid["c"]):
        bull += 0.5
    elif float(last["c"]) < float(mid["c"]):
        bear += 0.5
    chart = {
        "open": opened,
        "high": high,
        "low": low,
        "close": closed,
        "change": change,
        "location": round(location, 2),
    }
    return bull, bear, chart


def _market_bias(title: str) -> tuple[bool, bool]:
    """An IPO valuation cut or a selloff is bearish. Anthropic's share-sale repricing counts."""
    text = title.lower()
    ipo_cut = "anthropic" in text and any(word in text for word in ("valuation", "ipo", "selloff", "sell-off", "sell off"))
    return _hits(title, _BULL_MARKET), _hits(title, _BEAR_MARKET) or ipo_cut


def _hits(title: str, phrases: tuple[str, ...]) -> bool:
    text = title.lower()
    for phrase in phrases:
        if " " in phrase or len(phrase) > 4:
            if phrase in text:
                return True
        elif re.search(rf"\b{re.escape(phrase)}\b", text):
            return True
    return False


def _news_case(headlines: list[dict]) -> tuple[float, float, list[dict]]:
    bull = bear = 0.0
    kept = []
    for row in headlines:
        if not isinstance(row, dict) or not str(row.get("title") or "").strip():
            continue
        title = str(row["title"]).strip()
        kind = str(row.get("kind") or "")
        item = {"source": str(row.get("source") or ""), "kind": kind, "title": title}
        kept.append(item)
        if kind == "energy":
            bull_hit, bear_hit = _hits(title, _BULL_ENERGY), _hits(title, _BEAR_ENERGY)
        elif kind == "market":
            bull_hit, bear_hit = _market_bias(title)
        else:
            bull_hit, bear_hit = _hits(title, _BULL_POLITICAL), _hits(title, _BEAR_POLITICAL)
        if bull_hit:
            bull += 0.6
        if bear_hit:
            bear += 0.6
    return min(bull, 2.5), min(bear, 2.5), kept


def _band(score: float, bull: float, bear: float) -> str:
    if bull >= 1.5 and bear >= 1.5 and abs(bull - bear) < 0.75:
        return "Mixed"
    if score >= 6.5:
        return "Bullish"
    if score >= 5.5:
        return "Mildly Bullish"
    if score <= 3.4:
        return "Bearish"
    if score <= 4.4:
        return "Mildly Bearish"
    return "Neutral"


def _confidence(window: list[dict], headlines: list[dict]) -> str:
    if len(window) >= 20 and len(headlines) >= 3:
        return "high"
    if window and headlines:
        return "medium"
    return "low"


def _voice(name: str, text: str) -> dict:
    return {"name": name, "text": text}


def _session_label(chart: dict) -> str:
    if chart.get("change") is None:
        return "The last full session has no 15-minute bars on this chart."
    direction = "up" if chart["change"] > 0 else "down" if chart["change"] < 0 else "unchanged"
    if chart.get("live"):
        return (
            f"The session in progress opened at {chart['open']} and is {direction} {abs(chart['change'])} points "
            f"at {chart['close']}, between {chart['low']} and {chart['high']}."
        )
    return (
        f"The session opened at {chart['open']} and closed {direction} {abs(chart['change'])} points "
        f"at {chart['close']}, between {chart['low']} and {chart['high']}."
    )


def _named(headlines: list[dict], kind: str, phrases: tuple[str, ...]) -> list[str]:
    return [row["title"] for row in headlines if row["kind"] == kind and _hits(row["title"], phrases)]


def _bull_text(chart: dict, headlines: list[dict], chart_weight: float, news_weight: float) -> str:
    parts = [_session_label(chart)]
    if chart_weight > 0:
        parts.append("The close held up relative to the session range, which is the bull case on the chart.")
    else:
        parts.append("The chart itself does not support a bullish case.")
    political = _named(headlines, "political", _BULL_POLITICAL)
    energy = _named(headlines, "energy", _BULL_ENERGY)
    market = [row["title"] for row in headlines if row["kind"] == "market" and _market_bias(row["title"])[0]]
    if political:
        parts.append(f"Political headlines lean easier: {political[0]}.")
    if energy:
        parts.append(f"Energy headlines lean toward ample supply: {energy[0]}.")
    if market:
        parts.append(f"Market headlines lean toward a bid: {market[0]}.")
    if news_weight == 0:
        parts.append("No political or energy headline in this pull supports the bull case.")
    return " ".join(parts)


def _bear_text(chart: dict, headlines: list[dict], chart_weight: float, news_weight: float) -> str:
    parts = [_session_label(chart)]
    if chart_weight > 0:
        parts.append("The close was weak relative to the session range, which is the bear case on the chart.")
    else:
        parts.append("The chart itself does not support a bearish case.")
    political = _named(headlines, "political", _BEAR_POLITICAL)
    energy = _named(headlines, "energy", _BEAR_ENERGY)
    market = [row["title"] for row in headlines if row["kind"] == "market" and _market_bias(row["title"])[1]]
    if political:
        parts.append(f"Political headlines lean tighter: {political[0]}.")
    if energy:
        parts.append(f"Energy headlines lean toward a supply squeeze: {energy[0]}.")
    if market:
        parts.append(f"Market headlines lean toward a selloff: {market[0]}.")
    if news_weight == 0:
        parts.append("No political or energy headline in this pull supports the bear case.")
    return " ".join(parts)


def _risk_text(band: str, score: float, bull: float, bear: float, chart: dict, headlines: list[dict]) -> str:
    political = len([row for row in headlines if row["kind"] == "political"])
    energy = len([row for row in headlines if row["kind"] == "energy"])
    market = len([row for row in headlines if row["kind"] == "market"])
    lead = "the bull case" if bull > bear else "the bear case" if bear > bull else "neither case"
    which = "the session in progress" if chart.get("live") else "the last closed session"
    return (
        f"Risk weighs {lead}. The score is {score} ({band}). "
        f"Bull weight {bull}, bear weight {bear}. "
        f"{_session_label(chart)} {political} political headlines, {energy} energy headlines, "
        f"and {market} market headlines were in the pull. "
        f"This is a read of {which} and those headlines. It is not an order."
    )
