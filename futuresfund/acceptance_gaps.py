"""Why finished backtests miss the acceptance boxes on the strategy library page."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from statistics import median

_SKIP = {
    "library_index.json",
    "strategy_cards.json",
    "creation_sources.json",
    "risk_suggestions.json",
    "untested_library.json",
}
_ORDER = ("profit", "factor", "drawdown", "under_drawdown", "growth", "trade_loss", "trades", "pace", "win_rate", "error")
_REFRESH_SECONDS = 600
_LOCK = threading.Lock()
_CACHE = {"payload": None, "loaded": False, "running": False, "started": 0.0}


def _cache_path() -> Path:
    return Path.home() / ".futuresfund" / "acceptance_gaps.json"


def drawdown_share_text() -> str:
    from futuresfund.objective import drawdown_share_text as text

    return text()


def _money(value: float) -> str:
    rounded = round(float(value), 2)
    sign = "-" if rounded < 0 else ""
    whole = abs(rounded)
    text = f"{whole:,.0f}" if abs(rounded - round(rounded)) < 0.001 else f"{whole:,.2f}"
    return f"{sign}${text}"


def _plain(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _count(value: int) -> str:
    return f"{int(value):,}"


def _trials_of(data: dict) -> list:
    instruments = data.get("instruments")
    found: list = []
    if isinstance(instruments, dict) and instruments:
        for record in instruments.values():
            if not isinstance(record, dict):
                continue
            charts = record.get("charts")
            if isinstance(charts, dict) and charts:
                for chart in charts.values():
                    trials = chart.get("trials") if isinstance(chart, dict) else None
                    if isinstance(trials, list):
                        found.extend(trials)
                continue
            trials = record.get("trials")
            if isinstance(trials, list):
                found.extend(trials)
        if found:
            return found
    trials = data.get("trials")
    return trials if isinstance(trials, list) else []


def _stage_of(row: dict) -> int | None:
    """The proving stage a trial ran on, in days, or None for a one-year run."""
    try:
        days = int(row.get("stage_days") or 0)
    except (TypeError, ValueError):
        return None
    return days if 0 < days < 365 else None


def _misses(row: dict, limits: dict, start: float, loss_cap: float) -> dict[str, float | None]:
    """A stage trial against its own stage's bars, and a one-year trial against the year."""
    from futuresfund.objective import acceptance_misses

    return acceptance_misses(row, days=_stage_of(row), limits=limits, start=start, loss_cap=loss_cap)


def _bars(days: int | None, limits: dict) -> dict:
    from futuresfund.objective import stage_bars

    return stage_bars(days, limits)


def _where(days: int | None) -> str:
    return f" on the {int(days)}-day test" if days else ""


def _fewest(bars: dict) -> int:
    """The fewest trades the check asked for: the stage count, or on the year the library count (never under four)."""
    if bars["year"]:
        return max(bars["library_trades"], bars["least_trades"])
    return bars["min_trades"]


def _row(name: str, values: list[float], limits: dict, start: float, misses: int, trials: int, days: int | None = None) -> dict:
    """One acceptance miss, with the typical result beside the bar that window was held to."""
    from futuresfund.objective import TRADES_YEAR_MAX

    bars = _bars(days, limits)
    where = _where(days)
    share = f"{_count(misses)} of {_count(trials)}"
    if name == "error":
        return {"reason": f"Did not finish{where}", "typical": "—", "acceptance": "A finished result", "compared": "Stopped before a result", "misses": share}
    if name == "growth":
        return {
            "reason": "Profit growing over 7, 30, 90, and 365 days",
            "typical": "—",
            "acceptance": "More profit on each longer window",
            "compared": "A longer window made less",
            "misses": share,
        }
    if not values:
        return {"reason": f"{name}{where}", "typical": "Not reported", "acceptance": "—", "compared": "No number was reported", "misses": share}
    typical = float(median(values))
    if name == "profit":
        bar = float(round(bars["profit"]))
        steady = f" in {int(days)} days (a {_money(bars['year_profit'])} year)" if days and bars["use_profit"] else ""
        return {
            "reason": f"Profit{where}",
            "typical": _money(typical),
            "acceptance": f"Above {_money(bar)}{steady}",
            "compared": f"{_money(bar - typical)} under the line",
            "misses": share,
        }
    if name == "factor":
        bar = float(bars["profit_factor"])
        compared = f"{typical / bar:.2f} times the bar" if bar and typical > 0 else "At or below the bar"
        return {"reason": f"Profit factor{where}", "typical": _plain(typical), "acceptance": f"Above {_plain(bar)}", "compared": compared, "misses": share}
    if name == "drawdown":
        same = ", the same as the year" if days else ""
        return {
            "reason": f"Drawdown{where}",
            "typical": _money(typical),
            "acceptance": f"Inside {drawdown_share_text()}{same}",
            "compared": "Over the limit at its profit high",
            "misses": share,
        }
    if name == "under_drawdown":
        return {"reason": "Profit under the drawdown", "typical": f"{_money(typical)} drawdown", "acceptance": "Profit above the drawdown", "compared": "Drawdown at or above the profit", "misses": share}
    if name == "trade_loss":
        return {"reason": f"The stop on one trade{where}", "typical": _money(typical), "acceptance": "Stop set at no more than 1.5 percent of the account", "compared": "Over the limit", "misses": share}
    if name == "trades":
        bar = float(_fewest(bars))
        compared = f"{typical / bar:.2f} times that count" if bar else "Under the count"
        steady = f" in {int(days)} days ({bars['year_trades']} a year)" if days and bars["use_trades"] else ""
        return {
            "reason": f"Trade count{where}",
            "typical": f"{_plain(typical)} trades",
            "acceptance": f"At least {_plain(bar)}{steady}",
            "compared": compared,
            "misses": share,
        }
    if name == "pace":
        compared = f"{typical / TRADES_YEAR_MAX:.2f} times that cap" if TRADES_YEAR_MAX else "Over the cap"
        most = f" ({bars['max_trades']} in {int(days)} days)" if days else ""
        return {
            "reason": f"Trades a year{where}",
            "typical": f"{_plain(typical)} a year",
            "acceptance": f"At most {_plain(float(TRADES_YEAR_MAX))}{most}",
            "compared": compared,
            "misses": share,
        }
    bar = float(bars["win_rate"])
    compared = f"{typical / bar:.2f} times that rate" if bar and typical > 0 else "At or below the rate"
    return {
        "reason": f"Win rate{where}",
        "typical": f"{_plain(typical)} percent",
        "acceptance": f"Above {_plain(bar)} percent",
        "compared": compared,
        "misses": share,
    }


def _line(name: str, values: list[float], limits: dict, start: float, misses: int, trials: int, days: int | None = None) -> str:
    from futuresfund.objective import TRADES_YEAR_MAX

    bars = _bars(days, limits)
    where = _where(days)
    tests = f"{int(days)}-day tests" if days else "backtests"
    share = f"{_count(misses)} of {_count(trials)} {tests} miss it."
    if name == "error":
        return f"The backtest did not finish{where}. {_count(misses)} of {_count(trials)} {tests} stopped before a result."
    if name == "growth":
        return f"Profit growing over 7, 30, 90, and 365 days. A longer window made less than the shorter one before it. {share}"
    if not values:
        return f"{name}{where}. The result did not report a number. {share}"
    typical = float(median(values))
    if name == "profit":
        bar = float(round(bars["profit"]))
        steady = f" in {int(days)} days (a {_money(bars['year_profit'])} year at a steady pace)" if days and bars["use_profit"] else ""
        return (
            f"Profit{where}. The typical miss made {_money(typical)} versus acceptance above {_money(bar)}{steady}. "
            f"That is {_money(bar - typical)} under the line. {share}"
        )
    if name == "factor":
        bar = float(bars["profit_factor"])
        times = f" That is {typical / bar:.2f} times the bar." if bar and typical > 0 else ""
        return f"Profit factor{where}. The typical miss is {_plain(typical)} versus acceptance above {_plain(bar)}.{times} {share}"
    if name == "drawdown":
        same = " The limit is the same as the year's, because a shorter window's drawdown is never larger than the year that holds it." if days else ""
        return f"Drawdown{where}. The typical miss is {_money(typical)}, over the limit of {drawdown_share_text()}.{same} {share}"
    if name == "under_drawdown":
        return f"Profit under the drawdown. The typical miss has a drawdown of {_money(typical)}, at or above its profit. {share}"
    if name == "trade_loss":
        return f"The stop on one trade{where}. The typical miss was set to risk {_money(typical)}, more than 1.5 percent of the account. {share}"
    if name == "trades":
        bar = float(_fewest(bars))
        times = f" That is {typical / bar:.2f} times that count." if bar else ""
        steady = f" in {int(days)} days ({bars['year_trades']} a year at a steady pace)" if days and bars["use_trades"] else ""
        return (
            f"Trade count{where}. The typical miss has {_plain(typical)} trades versus acceptance of at least {_plain(bar)}{steady}."
            f"{times} {share}"
        )
    if name == "pace":
        times = f" That is {typical / TRADES_YEAR_MAX:.2f} times that cap." if TRADES_YEAR_MAX else ""
        most = f" ({bars['max_trades']} in {int(days)} days)" if days else ""
        return (
            f"Trades a year{where}. The typical miss takes {_plain(typical)} trades a year versus acceptance of at most "
            f"{_plain(float(TRADES_YEAR_MAX))}{most}.{times} {share}"
        )
    bar = float(bars["win_rate"])
    times = f" That is {typical / bar:.2f} times that rate." if bar and typical > 0 else ""
    return (
        f"Win rate{where}. The typical miss wins {_plain(typical)} percent versus acceptance above {_plain(bar)} percent."
        f"{times} {share}"
    )


def measure(notes: Path, limits: dict, start: float, limit: int = 3) -> dict:
    """The acceptance boxes the finished backtests miss most often."""
    from futuresfund.objective import account_trade_loss_cap

    loss_cap = account_trade_loss_cap()
    # Each miss is counted per window: a 7-day trial is held to the 7-day bars, a year to the year's,
    # so the typical number in a line sits beside the bar its trials were judged against.
    buckets: dict[tuple, list[float]] = {}
    counts: dict[tuple, int] = {}
    tried: dict[int | None, int] = {}
    trials = 0
    passed = 0
    if notes.is_dir():
        for path in notes.glob("*.json"):
            if path.name in _SKIP:
                continue
            try:
                if path.stat().st_size > 8_000_000:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            for row in _trials_of(data):
                if not isinstance(row, dict):
                    continue
                trials += 1
                days = _stage_of(row)
                tried[days] = tried.get(days, 0) + 1
                missed = _misses(row, limits, start, loss_cap)
                if not missed:
                    passed += 1
                    continue
                for name, value in missed.items():
                    key = (name, days)
                    counts[key] = counts.get(key, 0) + 1
                    if value is not None:
                        buckets.setdefault(key, []).append(float(value))

    def order(key: tuple) -> tuple:
        name, days = key
        return (-counts[key], _ORDER.index(name) if name in _ORDER else len(_ORDER), days or 365)

    ranked = sorted((key for key, count in counts.items() if count), key=order)
    lines = [
        _line(name, buckets.get((name, days)) or [], limits, start, counts[(name, days)], tried.get(days, trials), days)
        for name, days in ranked[:limit]
    ]
    rows = [
        _row(name, buckets.get((name, days)) or [], limits, start, counts[(name, days)], tried.get(days, trials), days)
        for name, days in ranked[:limit]
    ]
    return {"ready": True, "trials": trials, "passed": passed, "lines": lines, "rows": rows}


def _refresh() -> None:
    from futuresfund.book import strategy_acceptance
    from futuresfund.learn import NOTES
    from futuresfund.objective import drawdown_start

    try:
        payload = measure(NOTES, strategy_acceptance(), drawdown_start())
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except Exception:
        payload = None
    with _LOCK:
        if isinstance(payload, dict):
            _CACHE["payload"] = payload
        _CACHE["running"] = False
        _CACHE["started"] = time.time()


def _load_once() -> None:
    with _LOCK:
        if _CACHE["loaded"]:
            return
        _CACHE["loaded"] = True
    payload = None
    path = _cache_path()
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
    with _LOCK:
        if isinstance(payload, dict) and payload.get("ready"):
            _CACHE["payload"] = payload
            _CACHE["started"] = time.time()


def gaps_report() -> dict:
    """The last reading. A fresh reading runs in the background when this one is old."""
    _load_once()
    now = time.time()
    with _LOCK:
        payload = _CACHE["payload"]
        missing_rows = isinstance(payload, dict) and payload.get("ready") and not payload.get("rows")
        start = not _CACHE["running"] and (
            payload is None or missing_rows or now - float(_CACHE["started"]) >= _REFRESH_SECONDS
        )
        if start:
            _CACHE["running"] = True
    if start:
        threading.Thread(target=_refresh, daemon=True, name="acceptance-gaps").start()
    if isinstance(payload, dict):
        return payload
    return {"ready": False, "trials": 0, "passed": 0, "lines": [], "rows": []}


def _report_path() -> Path:
    return Path.home() / ".futuresfund" / "acceptance_report.json"


_PUBLISHED = {"loaded": False, "report": None}


def _load_published() -> None:
    with _LOCK:
        if _PUBLISHED["loaded"]:
            return
        _PUBLISHED["loaded"] = True
    report = None
    path = _report_path()
    if path.is_file():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
    if isinstance(report, dict) and report.get("at"):
        with _LOCK:
            _PUBLISHED["report"] = report


def published_report() -> dict:
    """The report the Portfolio Manager has already given. Empty until someone asks."""
    _load_published()
    with _LOCK:
        report = _PUBLISHED["report"]
    if isinstance(report, dict):
        return report
    return {"ready": False, "trials": 0, "passed": 0, "rows": [], "at": ""}


def publish_report() -> dict:
    """File the acceptance table from the finished backtests."""
    from datetime import datetime

    gaps = gaps_report()
    rows = gaps.get("rows") if isinstance(gaps.get("rows"), list) else []
    if not gaps.get("ready") or not rows:
        report = {"ready": False, "trials": int(gaps.get("trials") or 0), "passed": int(gaps.get("passed") or 0), "rows": [], "at": ""}
    else:
        report = {
            "ready": True,
            "trials": int(gaps["trials"]),
            "passed": int(gaps["passed"]),
            "rows": rows,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report), encoding="utf-8")
    temporary.replace(path)
    with _LOCK:
        _PUBLISHED["report"] = report
        _PUBLISHED["loaded"] = True
    return report


def backtest_report_text() -> str:
    """The Portfolio Manager's reading of the acceptance table."""
    report = publish_report()
    if not report.get("ready"):
        return "I am still reading the finished backtests. Ask again in a moment and I will put the table on the R&D tab."
    lines = [
        f"{report['passed']:,} of {report['trials']:,} backtests clear every acceptance box that is on.",
        "The three misses that show up most often are in the table on the R&D tab.",
    ]
    for row in report["rows"]:
        lines.append(
            f"{row['reason']}. Typical result {row['typical']}. Acceptance {row['acceptance']}. "
            f"Compared with the bar: {row['compared']}. {row['misses']} miss it."
        )
    return "\n".join(lines)
