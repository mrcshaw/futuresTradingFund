"""The engine checked against TradingView's Strategy Tester.

A trade list exported from the Strategy Tester and saved in the project root
is the check for the script with that name. The engine runs that script on the
same contract and chart, over the same range, and every trade is compared on
entry time, side, prices, and exit. A TradingView trade the engine misses is
reported with the bars this computer is missing around it, if any.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
# checkScripts holds the exact source TradingView ran for each export, so it is read first.
_FOLDERS = ("checkScripts", "learningStrategies", "learnedStrategies", "createdStrategies")
_FRAMES = ((1, "1m"), (2, "2m"), (3, "3m"), (5, "5m"), (15, "15m"), (30, "30m"), (60, "1h"))
_WIDTH = {"1m": 60, "2m": 120, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
_ROOTS = (("ES1!", "ES"), ("NQ1!", "NQ"), ("GC1!", "GC"), ("MGC1!", "GC"), ("QO1!", "QO"))


def _trade_csvs(folder: Path) -> list[Path]:
    found = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.csv")):
        try:
            with path.open(encoding="utf-8-sig") as handle:
                head = handle.readline()
        except OSError:
            continue
        if head.startswith("Trade number,"):
            found.append(path)
    return found


def exports() -> list[Path]:
    """Strategy Tester trade lists in the project root."""
    return _trade_csvs(ROOT)


def trade_lists() -> list[Path]:
    """Strategy Tester trade lists saved for an engine check."""
    return _trade_csvs(ROOT / "trades")


def check_all() -> list[dict]:
    return [check_export(path) for path in exports()]


def check_export(path: Path) -> dict:
    from futuresfund.backtrader_engine import _bars_from, compare, load_tv_trades, script_trades
    from futuresfund.research_bars import research_series
    from futuresfund.script_backtest import Unsupported
    from futuresfund.tv_broker import tv_metrics

    name = path.name
    report: dict = {"file": name}
    tv = load_tv_trades(path)
    if not tv:
        report["error"] = "The file has no closed trades."
        return report
    script = _script_for(_title_part(path.stem))
    if script is None:
        report["error"] = f"No Pine script in the strategy folders is named like {name}."
        return report
    source_path, title = script
    root = _root_for(path.stem)
    timeframe = _timeframe(path)
    report.update({"title": title, "script": source_path.name, "instrument": root, "timeframe": timeframe})
    if not timeframe:
        report["error"] = "The chart timeframe could not be read from the trade durations."
        return report
    source = source_path.read_text(encoding="utf-8", errors="replace")
    series = research_series(root, timeframe, include_live=False)
    first_epoch, start = _range_start(path, tv)
    bars = [bar for bar in _bars_from(series) if bar.epoch >= first_epoch]
    end = max(trade["exit_time"] for trade in tv)
    best = None
    for label, inputs in _candidates(title, root):
        try:
            trades = script_trades(source, bars, inputs, root, timeframe, as_written=True)
        except Unsupported:
            report["error"] = f"{title} has no entry rule on the engine yet."
            return report
        opened = tv[0]["entry_time"]
        window = [trade for trade in trades if opened <= trade["entry_time"] <= end]
        result = compare(window, tv)
        if best is None or result["matched"] > best[1]["matched"]:
            best = (label, result, window)
    label, result, window = best
    tv_keys = {_key(trade) for trade in tv}
    engine_keys = {_key(trade) for trade in window}
    covered_from = _et(bars[0].epoch) if bars else ""
    covered_to = _et(bars[-1].epoch) if bars else ""
    # A chart file that starts in the middle of the export has no profile history.
    # The first ten days of that file are not compared.
    comparable = covered_from
    if bars and tv and covered_from > tv[0]["entry_time"]:
        comparable = (bars[0].when + timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    bar_times = {_et(bar.epoch) for bar in bars}
    outside = [
        trade for trade in tv
        if not comparable
        or trade["entry_time"] < comparable
        or trade["entry_time"] > covered_to
        or trade["entry_time"] not in bar_times
    ]
    on_chart = [trade for trade in window if comparable and comparable <= trade["entry_time"] <= covered_to]
    missed = [trade for trade in tv if _key(trade) not in engine_keys and trade not in outside]
    extras = [trade for trade in on_chart if _key(trade) not in tv_keys]
    gaps = _missing_spans(series, _WIDTH[timeframe], start, end, _traded(root))
    agree_from = _agree_from(tv, window, missed, extras)
    report.update({
        "range": [start, end],
        "inputs": label,
        "expected": len(tv),
        "covered": len(tv) - len(outside),
        "covered_profit": round(sum(float(trade["pnl"]) for trade in tv if trade not in outside), 2),
        "engine_covered_profit": round(sum(float(trade["pnl"]) for trade in on_chart), 2),
        "matched": result["matched"],
        "extra": len(extras),
        "uncovered": [_brief(trade, gaps) for trade in outside],
        "missed": [_brief(trade, gaps) for trade in missed],
        "extras": [_brief(trade, gaps) for trade in extras],
        "tradingview": tv_metrics(tv),
        "engine": tv_metrics(window),
        "missing_bars": [f"{a} to {b}" for a, b in gaps],
        "longest_missing": [f"{a} to {b}" for a, b in sorted(gaps, key=_span_minutes, reverse=True)[:3]],
        "filled_from_yahoo": [f"{a} to {b}" for a, b in sorted(_borrowed(series, _WIDTH[timeframe], start, end), key=_span_minutes, reverse=True)[:3]],
    })
    if agree_from:
        later_tv = [trade for trade in tv if trade["entry_time"] >= agree_from]
        later_engine = [trade for trade in window if trade["entry_time"] >= agree_from]
        report["identical_from"] = {
            "from": agree_from,
            "trades": len(later_tv),
            "tradingview": tv_metrics(later_tv),
            "engine": tv_metrics(later_engine),
        }
    return report


def _range_start(path: Path, tv: list[dict]) -> tuple[int, str]:
    """Where TradingView's "Last 30 days" began for this export: 00:00 UTC, 30 days before the export date."""
    from futuresfund.backtrader_engine import tester_range

    match = re.search(r"_(\d{4})-(\d{2})-(\d{2})_", path.name)
    if match:
        from datetime import date

        epoch, start = tester_range(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        if tv[0]["entry_time"] >= start:
            return epoch, start
    # Ten days, so a 15-minute profile of 120 bars is already full at the first trade.
    first = datetime.strptime(tv[0]["entry_time"][:10], "%Y-%m-%d").replace(tzinfo=ET) - timedelta(days=10)
    return int(first.timestamp()), first.strftime("%Y-%m-%d %H:%M")


def _key(trade: dict) -> tuple:
    return (
        trade["entry_time"],
        trade["side"],
        round(float(trade["entry_price"]), 2),
        trade.get("exit_time"),
        round(float(trade.get("exit_price") or 0), 2),
        trade.get("exit_signal"),
    )


def _agree_from(tv: list[dict], window: list[dict], missed: list[dict], extras: list[dict]) -> str | None:
    """The first trade from which every later TradingView trade matches and the engine adds none."""
    if not missed and not extras:
        return tv[0]["entry_time"]
    last_bad = max([trade.get("exit_time") or trade["entry_time"] for trade in missed + extras])
    later = [trade for trade in tv if trade["entry_time"] > last_bad]
    return later[0]["entry_time"] if later else None


def _brief(trade: dict, gaps: list[tuple[str, str]]) -> dict:
    entry = trade["entry_time"]
    near = [f"{a} to {b}" for a, b in gaps if a <= (trade.get("exit_time") or entry) and _back(entry, 2) <= b]
    return {
        "entry_time": entry,
        "side": trade["side"],
        "entry_price": trade["entry_price"],
        "exit_time": trade.get("exit_time"),
        "exit_signal": trade.get("exit_signal"),
        "exit_price": trade.get("exit_price"),
        "pnl": trade.get("pnl"),
        "missing_bars_nearby": near,
    }


def _back(stamp: str, days: int) -> str:
    when = datetime.strptime(stamp, "%Y-%m-%d %H:%M") - timedelta(days=days)
    return when.strftime("%Y-%m-%d %H:%M")


def _borrowed(series: list[dict], width: int, start: str, end: str) -> list[tuple[str, str]]:
    """Stretches inside the range whose bars came from Yahoo instead of TradingView."""
    spans = []
    run = None
    for row in series:
        stamp = _et(row["t"])
        if stamp < start or stamp > end:
            continue
        if row.get("source") == "yahoo":
            if run and row["t"] - run[1] <= width * 3:
                run[1] = row["t"]
            else:
                if run:
                    spans.append(run)
                run = [row["t"], row["t"]]
    if run:
        spans.append(run)
    return [(_et(a), _et(b)) for a, b in spans]


def _span_minutes(span: tuple[str, str]) -> float:
    a = datetime.strptime(span[0], "%Y-%m-%d %H:%M")
    b = datetime.strptime(span[1], "%Y-%m-%d %H:%M")
    return (b - a).total_seconds() / 60


def _traded(root: str) -> list[int]:
    """Bar times from every TradingView export of this contract, so a holiday close is not called missing."""
    from futuresfund.charts import chart_exports
    from futuresfund.research_bars import _WIDTH as widths, _export

    found = set()
    for timeframe in chart_exports(root):
        if timeframe in widths:
            found.update(_export(root, timeframe, widths[timeframe]))
    return sorted(found)


def _missing_spans(series: list[dict], width: int, start: str, end: str, traded: list[int] | None = None) -> list[tuple[str, str]]:
    """Stretches inside the range where TradingView has bars but this computer does not."""
    import bisect

    stamps = [row["t"] for row in series]
    spans = []
    for left, right in zip(stamps, stamps[1:]):
        if right - left <= width:
            continue
        first = left + width
        if not any(_market_open(epoch) for epoch in range(first, right, width)):
            continue
        if traded is not None:
            k = bisect.bisect_left(traded, first)
            if k >= len(traded) or traded[k] >= right:
                continue
        a = _et(first)
        b = _et(right - width)
        if b < start or a > end:
            continue
        spans.append((a, b))
    return spans


def _market_open(epoch: int) -> bool:
    """CME Globex hours: Sunday 18:00 to Friday 17:00 ET, closed 17:00 to 18:00 each day."""
    when = datetime.fromtimestamp(epoch, timezone.utc).astimezone(ET)
    day = when.weekday()
    minute = when.hour * 60 + when.minute
    if day == 5:
        return False
    if day == 6:
        return minute >= 18 * 60
    if day == 4:
        return minute < 17 * 60
    return not (17 * 60 <= minute < 18 * 60)


def _et(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M")


def _candidates(title: str, root: str) -> list[tuple[str, dict]]:
    """The script's own defaults, then every library row of that script on that contract."""
    from futuresfund.contracts import POINT_VALUE
    from futuresfund.learn import _with_contract_point
    from futuresfund.library import _load_index_rows

    point = float(POINT_VALUE.get(root, 50))
    settings = {"pv": point, "point_value": point, "pointvalue": point}
    found = [("the script's settings", settings)]
    for row in _load_index_rows():
        if str(row.get("script") or "") != title or not isinstance(row.get("params"), dict):
            continue
        found.append((str(row.get("title") or title), _with_contract_point(dict(row["params"]), root)))
    return found


def _title_part(stem: str) -> str:
    return re.split(r"_(?:CME|COMEX|NYMEX|CBOT)_", stem, maxsplit=1)[0]


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _script_for(part: str) -> tuple[Path, str] | None:
    """The strategy whose name is the export name, or the longest name the export starts with.

    TradingView puts the strategy name in the file, then sometimes the chart
    nickname, then the exchange. The nickname is not part of strategy().
    """
    wanted = _fold(part)
    base = ROOT / "futuresfund"
    prefix: tuple[Path, str] | None = None
    prefix_len = 0
    for folder in _FOLDERS:
        for path in sorted((base / folder).glob("*.pine")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r'strategy\s*\(\s*"([^"]+)"', text)
            if not match:
                continue
            title = match.group(1).strip()
            folded = _fold(title)
            if folded == wanted:
                return path, title
            if folded and wanted.startswith(folded) and len(folded) > prefix_len:
                prefix = (path, title)
                prefix_len = len(folded)
    return prefix


def _root_for(stem: str) -> str:
    upper = stem.upper()
    for token, root in _ROOTS:
        if f"_{token}_" in upper or upper.endswith(f"_{token}"):
            return root
    return "ES"


def _timeframe(path: Path) -> str:
    """The chart the trades were taken on, from the shortest minutes per bar any trade shows."""
    ratios = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    entries = {row["Trade number"]: row for row in rows if row["Type"].startswith("Entry")}
    for row in rows:
        if row["Type"].startswith("Entry") or row.get("Signal") == "Open":
            continue
        entry = entries.get(row["Trade number"])
        try:
            bars = int(row.get("Duration (bars)") or 0)
        except ValueError:
            continue
        if entry is None or bars <= 0:
            continue
        opened = datetime.strptime(entry["Date and time"], "%Y-%m-%d %H:%M")
        closed = datetime.strptime(row["Date and time"], "%Y-%m-%d %H:%M")
        ratios.append((closed - opened).total_seconds() / 60 / bars)
    if not ratios:
        return ""
    minutes = min(ratios)
    return min(_FRAMES, key=lambda item: abs(item[0] - minutes))[1]
