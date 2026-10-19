"""Research bars.

The Databento one-minute download is the chart when that root has one. It is
the front-month series for the whole file, 2021-10-04 through 2026-10-03.
A higher timeframe is folded from those minutes. A minute with no trade is
absent, and the bar is built from the minutes that did trade. A TradingView
export of that same timeframe replaces those bars, because that is the chart
the Strategy Tester ran on.

Without that download, the TradingView export for the timeframe is used.
Outside it, bars are folded from 1-minute candles, and a bar is built only
when every minute of it is there. Yahoo is not used.
"""

from __future__ import annotations

_WIDTH = {"1m": 60, "2m": 120, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def research_series(root: str, timeframe: str, *, include_live: bool = True) -> list[dict]:
    """Bars for one chart, oldest first, as {t, o, h, l, c, v, source} with t in epoch seconds.

    include_live adds the alert candles after a TradingView export. A Strategy
    Tester comparison leaves them out, because those minutes are not the chart
    the backtest ran on.
    """
    width = _WIDTH.get(timeframe)
    if width is None:
        return []
    historical = _historical(root)
    if historical:
        folded = historical if width == 60 else _fold(historical, width, partial=True)
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

