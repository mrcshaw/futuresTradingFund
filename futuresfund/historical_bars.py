"""The Databento one-minute download.

The batch is GLBX.MDP3 ohlcv-1m for the parent symbols ES.FUT, NQ.FUT, and GC.FUT,
from 2021-10-04 through 2026-10-03. Prices and timestamps in the CSV are already
scaled (pretty_px and pretty_ts). Each file is one symbol. A name with a hyphen
is a spread. An outright is one expiration.

The desk backtests one series per chart. For each futures session the series
keeps the outright that led volume on the previous session, which is the front
month TradingView's continuous symbol has already rolled to. The same session's
volume is not used, because that winner is not known until the session ends
and it switches one session before TradingView.
Quiet minutes are absent from the file. A higher timeframe bar is built from
the minutes that did trade.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from futuresfund.config import ROOT

HISTORY = ROOT / "chartData" / "historicalData"
CHARTS = ROOT / "chartData"
_MONTHS = {code: index for index, code in enumerate("FGHJKMNQUVXZ", start=1)}
_SYMBOL = re.compile(r"^([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d+)$")
_CACHE: dict[str, dict[int, tuple]] = {}


def product_of(symbol: str) -> str | None:
    """ES from ESH2, GC from GCG6. A spread uses the first leg."""
    head = str(symbol or "").split("-", 1)[0].strip().upper()
    match = _SYMBOL.match(head)
    return match.group(1) if match else None


def expiry_key(symbol: str) -> tuple[int, int]:
    """Earlier expiration sorts first. A one-digit year in this batch is 2021 through 2027."""
    head = str(symbol or "").split("-", 1)[0].strip().upper()
    match = _SYMBOL.match(head)
    if not match:
        return (9999, 99)
    year = int(match.group(3))
    if year < 100:
        year += 2020 if year < 10 else 2000
    return (year, _MONTHS[match.group(2)])


def organize() -> dict[str, int]:
    """Move each symbol file into chartData/es, nq, gc, or mnq. Spreads go in spreads/."""
    counts = {"ES": 0, "NQ": 0, "GC": 0, "MNQ": 0, "spreads": 0, "left": 0}
    if not HISTORY.is_dir():
        return counts
    for path in sorted(HISTORY.glob("*.csv")):
        symbol = path.stem.split(".")[-1]
        product = product_of(symbol)
        if product not in {"ES", "NQ", "GC", "MNQ"}:
            counts["left"] += 1
            continue
        folder_name = "gc" if product == "GC" else product.lower()
        folder = CHARTS / folder_name
        if "-" in symbol:
            folder = folder / "spreads"
            counts["spreads"] += 1
        else:
            counts[product] += 1
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / path.name
        if target.exists():
            continue
        path.replace(target)
    return counts


def _folder_name(root: str) -> str:
    from futuresfund.contracts import product_root

    return product_root(root).lower() or "es"


def outright_files(root: str) -> list[Path]:
    """Minute files for one root.

    A file named for one outright ends in that symbol. A download that keeps
    every outright in one file ends in ohlcv-1m and carries the symbol on each
    row. A hyphenated symbol is a spread and stays out.
    """
    folder_name = _folder_name(root)
    folder = CHARTS / folder_name
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.csv"):
        if "ohlcv-1m" not in path.name or path.parent.name == "spreads":
            continue
        symbol = path.stem.split(".")[-1]
        if "-" in symbol and symbol != "ohlcv-1m":
            continue
        found.append(path)
    return sorted(found)


def cache_path(root: str) -> Path:
    return CHARTS / _folder_name(root) / "databento-1m.csv"


def load_minutes(root: str) -> dict[int, tuple] | None:
    """The front-month one-minute bars, or None when this root has no Databento download."""
    from futuresfund.contracts import product_root

    key = product_root(root)
    if key in _CACHE:
        return _CACHE[key]
    files = outright_files(key)
    if not files:
        return None
    path = cache_path(key)
    if not path.is_file() or path.stat().st_mtime < max(item.stat().st_mtime for item in files):
        build_continuous(key)
    minutes = _read_cache(path)
    _CACHE[key] = minutes
    return minutes


def roll_calendar(leaders: dict) -> dict:
    """Map each session to the outright that led volume on the previous session.

    The first session uses its own leader. After that, a new outright is used
    on the session after it first leads, which is when TradingView's continuous
    symbol rolls.
    """
    chosen = {}
    previous = None
    for day in sorted(leaders):
        chosen[day] = previous if previous is not None else leaders[day]
        previous = leaders[day]
    return chosen


def build_continuous(root: str) -> Path:
    """One series: each session uses the previous session's volume leader."""
    from futuresfund.objective import _session_day

    files = outright_files(root)
    totals: dict[tuple, float] = {}
    for path in files:
        for epoch, _o, _h, _l, _c, volume, symbol in _read_csv(path):
            if product_of(symbol) != root:
                continue
            totals[_session_day(epoch), symbol] = totals.get((_session_day(epoch), symbol), 0.0) + volume
    by_day: dict = {}
    for (day, symbol), volume in totals.items():
        by_day.setdefault(day, []).append((volume, expiry_key(symbol), symbol))
    leaders = {}
    for day, rows in by_day.items():
        rows.sort(key=lambda item: (-item[0], item[1]))
        leaders[day] = rows[0][2]
    chosen = roll_calendar(leaders)
    series = []
    seen = set()
    for path in files:
        for epoch, opened, high, low, close, volume, symbol in _read_csv(path):
            if product_of(symbol) != root or chosen.get(_session_day(epoch)) != symbol or epoch in seen:
                continue
            seen.add(epoch)
            series.append((epoch, opened, high, low, close, volume))
    series.sort()
    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", "o", "h", "l", "c", "v"])
        writer.writerows(series)
    temporary.replace(path)
    _CACHE.pop(str(root).upper(), None)
    return path


def _read_cache(path: Path) -> dict[int, tuple]:
    found = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                epoch = int(float(row["t"]))
            except (TypeError, ValueError, KeyError):
                continue
            found[epoch] = (
                float(row["o"]),
                float(row["h"]),
                float(row["l"]),
                float(row["c"]),
                float(row["v"] or 0),
            )
    return found


def _read_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            epoch = _epoch(row.get("ts_event"))
            close = _price(row.get("close"))
            if epoch is None or close is None:
                continue
            opened = _price(row.get("open"))
            high = _price(row.get("high"))
            low = _price(row.get("low"))
            try:
                volume = float(row.get("volume") or 0)
            except ValueError:
                volume = 0.0
            symbol = str(row.get("symbol") or path.stem.split(".")[-1])
            if "-" in symbol:
                continue
            yield (
                epoch,
                opened if opened is not None else close,
                high if high is not None else close,
                low if low is not None else close,
                close,
                volume,
                symbol,
            )


def _epoch(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10**16:
            number //= 1_000_000_000
        elif number > 10**13:
            number //= 1_000_000
        elif number > 10**11:
            number //= 1_000
        return number
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _price(value) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if abs(number) >= 1_000_000:
        number /= 1_000_000_000
    return number
