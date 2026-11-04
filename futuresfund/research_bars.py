"""Research bars.

The Databento one-minute download is the chart when that root has one. It is
the front-month series for the whole file, 2021-10-04 through 2026-10-03.
A higher timeframe is folded from those minutes. A minute with no trade is
absent, and the bar is built from the minutes that did trade. A TradingView
export of that same timeframe replaces those bars, because that is the chart
the Strategy Tester ran on. After the download's last bar, the TradingView
alert candles carry the chart forward to the last closed bar.

Without that download, the TradingView export for the timeframe is used.
Outside it, bars are folded from 1-minute candles, and a bar is built only
when every minute of it is there. Yahoo is not used.
"""

from __future__ import annotations

import threading
import time

_WIDTH = {"1m": 60, "2m": 120, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
# A Databento series is folded once and kept until a download or an export changes.
# Every study used to fold five years of minutes again, several seconds of the desk's
# own thread each time, while six charts did the same.
_SERIES: dict[tuple, tuple] = {}
_BUILD = threading.Lock()
# The alert candles after the download are read again at most this often. A study loads its
# bars once at the start, so a refresh never changes the bars under a search that is running.
LIVE_REFRESH = 900
_LIVE_TAIL: dict[tuple, tuple] = {}


def _sources(root: str, timeframe: str) -> tuple:
    """The files a Databento series is built from, with their sizes and times."""
    from futuresfund.charts import chart_exports
    from futuresfund.historical_bars import cache_path, outright_files

    stamp = []
    for path in (*outright_files(root), cache_path(root), *chart_exports(root).get(timeframe, [])):
        try:
            info = path.stat()
        except OSError:
            continue
        stamp.append((str(path), info.st_mtime_ns, info.st_size))
    return tuple(stamp)


def research_series(root: str, timeframe: str, *, include_live: bool = True) -> list[dict]:
    """Bars for one chart, oldest first, as {t, o, h, l, c, v, source} with t in epoch seconds.

    include_live adds the alert candles after a TradingView export. A Strategy
    Tester comparison leaves them out, because those minutes are not the chart
    the backtest ran on. The bar dicts are shared between callers; the list is a copy.
    """
    width = _WIDTH.get(timeframe)
    if width is None:
        return []
    key = (str(root or "").upper(), timeframe)
    stamp = _sources(root, timeframe)
    kept = _SERIES.get(key)
    if kept is not None and kept[0] == stamp:
        return _with_live_tail(root, timeframe, width, kept[1]) if include_live else list(kept[1])
    with _BUILD:
        kept = _SERIES.get(key)
        if kept is not None and kept[0] == stamp:
            return _with_live_tail(root, timeframe, width, kept[1]) if include_live else list(kept[1])
        historical = _historical(root)
        if historical:
            rows = _databento_series(root, timeframe, width, historical)
            _SERIES[key] = (_sources(root, timeframe), rows)
            return _with_live_tail(root, timeframe, width, rows) if include_live else list(rows)
    return _export_series(root, timeframe, width, include_live)


def _with_live_tail(root: str, timeframe: str, width: int, rows: list[dict]) -> list[dict]:
    """The download, then the TradingView alert candles that came in after its last bar.

    The download stops on the day it was taken. The alerts keep arriving each minute, so
    without them the 7-day stage tests the same old week until someone downloads again.
    Only minutes after the download's last bar are added, so no alert minute replaces a
    download or export bar. The bar still forming is left out.
    """
    if not rows:
        return []
    after = int(rows[-1]["t"]) + width
    key = (str(root or "").upper(), timeframe, after)
    bucket = int(time.time() // LIVE_REFRESH)
    kept = _LIVE_TAIL.get(key)
    if kept is None or kept[0] != bucket:
        try:
            minutes = {epoch: row for epoch, row in _live(root).items() if epoch >= after}
        except (OSError, ValueError, TypeError, KeyError):
            minutes = {}
        now = time.time()
        tail = [
            {"t": epoch, "o": row[0], "h": row[1], "l": row[2], "c": row[3], "v": row[4], "source": "alert"}
            for epoch, row in sorted(_fold(minutes, width, partial=True).items())
            if epoch + width <= now
        ]
        kept = (bucket, tail)
        _LIVE_TAIL[key] = kept
    return list(rows) + list(kept[1])


def _databento_series(root: str, timeframe: str, width: int, historical: dict[int, tuple]) -> list[dict]:
    folded = dict(historical) if width == 60 else _fold(historical, width, partial=True)
    source = {epoch: "databento" for epoch in folded}
    # A TradingView export is the chart the Strategy Tester used. Where it
    # covers a bar, that price replaces the front-month series.
    for epoch, row in _export(root, timeframe, width).items():
        folded[epoch] = row
        source[epoch] = "tradingview"
    return [
        {"t": epoch, "o": row[0], "h": row[1], "l": row[2], "c": row[3], "v": row[4], "source": source[epoch]}
        for epoch, row in sorted(folded.items())
    ]


def _export_series(root: str, timeframe: str, width: int, include_live: bool) -> list[dict]:
    """Without a Databento download: the TradingView exports, filled from 1-minute candles."""
    minutes = _export(root, "1m", 60)
    if include_live:
        for epoch, row in _live(root).items():
            minutes.setdefault(epoch, row)
    bars = dict(_export(root, timeframe, width)) if timeframe != "1m" else {}
    source = {epoch: "tradingview" for epoch in bars}
    for epoch, row in _fold(minutes, width).items():
        if epoch in bars:
            continue
        bars[epoch] = row
        source[epoch] = "tradingview"
    return [
        {"t": epoch, "o": row[0], "h": row[1], "l": row[2], "c": row[3], "v": row[4], "source": source[epoch]}
        for epoch, row in sorted(bars.items())
    ]


def contract_series(symbol: str, timeframe: str) -> list[dict]:
    """Bars for one expiration, such as MNQZ6, oldest first, in the same shape as research_series."""
    from futuresfund.historical_bars import load_outright

    width = _WIDTH.get(timeframe)
    minutes = load_outright(symbol) if width else None
    if not minutes:
        return []
    folded = _fold(minutes, width, partial=True)
    return [
        {"t": epoch, "o": row[0], "h": row[1], "l": row[2], "c": row[3], "v": row[4], "source": "databento"}
        for epoch, row in sorted(folded.items())
    ]


def _historical(root: str) -> dict[int, tuple] | None:
    from futuresfund.historical_bars import load_minutes

    return load_minutes(root)


def _export(root: str, timeframe: str, width: int) -> dict[int, tuple]:
    """Every TradingView export for that timeframe. A newer file wins where two cover the same bar.

    A file's last bar is left out when the file was saved before that bar closed.
    """
    from futuresfund.chart_feed import _epoch_ms
    from futuresfund.charts import chart_exports
    from futuresfund.csv_bars import parse_tradingview_csv

    found: dict[int, tuple] = {}
    for path in chart_exports(root).get(timeframe, []):
        try:
            rows = parse_tradingview_csv(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        bars = {}
        for row in rows:
            stamp = _epoch_ms(row.get("t"))
            if stamp is None:
                continue
            bars[stamp // 1000] = (float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row.get("v") or 0))
        if bars:
            last = max(bars)
            if path.stat().st_mtime < last + width:
                bars.pop(last)
        found.update(bars)
    return found


def _live(root: str) -> dict[int, tuple]:
    """Ingestion's 1-minute alert candles: the archive, then the newest file over it."""
    from futuresfund.chart_feed import archived_bars, live_bars

    found = {}
    for row in archived_bars(root) + live_bars(root):
        if row.get("timeframe") != "1m" or not row.get("epoch"):
            continue
        epoch = int(row["epoch"]) // 1000
        found[epoch] = (float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row.get("v") or 0))
    return found


def _fold(minutes: dict[int, tuple], width: int, partial: bool = False) -> dict[int, tuple]:
    """Fold 1-minute bars into one timeframe.

    A TradingView bar with a missing minute is not built. The Databento file
    leaves out quiet minutes, so those bars are built from the minutes that traded.
    """
    if width == 60:
        return dict(minutes)
    groups: dict[int, list[int]] = {}
    for epoch in minutes:
        groups.setdefault(epoch - epoch % width, []).append(epoch)
    need = width // 60
    found = {}
    for bucket, stamps in groups.items():
        if not partial and len(stamps) < need:
            continue
        if not stamps:
            continue
        stamps.sort()
        rows = [minutes[stamp] for stamp in stamps]
        found[bucket] = (
            rows[0][0],
            max(row[1] for row in rows),
            min(row[2] for row in rows),
            rows[-1][3],
            sum(row[4] for row in rows),
        )
    return found

