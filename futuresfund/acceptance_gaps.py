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
    "kronos_patterns.json",
    "untested_library.json",
}
_ORDER = ("profit", "factor", "drawdown", "trades", "pace", "win_rate", "error")
_REFRESH_SECONDS = 600
_LOCK = threading.Lock()
_CACHE = {"payload": None, "loaded": False, "running": False, "started": 0.0}


def _cache_path() -> Path:
    return Path.home() / ".futuresfund" / "acceptance_gaps.json"


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


def _survives(row: dict, cap: float) -> bool:
    """The same drawdown rule as account_survives, using a cap already read from the book."""
    from futuresfund.objective import PROFIT_CUSHION, RATIO_FLOOR

    if row.get("max_drawdown") is None:
        return True
    try:
        drawdown = float(row["max_drawdown"])
    except (TypeError, ValueError):
        return True
    if drawdown < cap:
        return True
    try:
        profit = float(row.get("net_profit"))
    except (TypeError, ValueError):
        return False
    if profit <= PROFIT_CUSHION or drawdown <= 0:
        return False
    return profit / drawdown + 1e-9 >= RATIO_FLOOR


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


def _misses(row: dict, limits: dict, cap: float) -> dict[str, float | None]:
    from futuresfund.objective import TRADES_YEAR_MAX, profit_factor_of, trades_per_year, win_rate_percent

    if row.get("error") or row.get("fatal"):
        return {"error": None}
    missed: dict[str, float | None] = {}
    if limits.get("use_profit"):
        try:
            profit = float(row.get("net_profit"))
        except (TypeError, ValueError):
            missed["profit"] = None
        else:
            if profit <= float(limits["min_profit"]):
                missed["profit"] = profit
    if limits.get("use_factor"):
        factor = profit_factor_of(row)
        if factor is None or factor <= float(limits["profit_factor"]):
            missed["factor"] = factor
    if limits.get("use_drawdown") and not _survives(row, cap):
        try:
            missed["drawdown"] = float(row.get("max_drawdown"))
        except (TypeError, ValueError):
            missed["drawdown"] = None
    if limits.get("use_trades"):
        try:
            trades = int(float(row.get("trades") or 0))
        except (TypeError, ValueError):
            missed["trades"] = None
        else:
            if trades < int(limits["min_trades"]):
                missed["trades"] = float(trades)
            yearly = trades_per_year(row)
            if yearly is not None and yearly > TRADES_YEAR_MAX:
                missed["pace"] = float(yearly)
    if limits.get("use_win_rate"):
        rate = win_rate_percent(row)
        if rate is None or rate <= float(limits["win_rate"]):
            missed["win_rate"] = rate
    return missed


def _row(name: str, values: list[float], limits: dict, cap: float, misses: int, trials: int) -> dict:
    """One acceptance miss, with the typical result beside the bar."""
    from futuresfund.objective import TRADES_YEAR_MAX

    share = f"{_count(misses)} of {_count(trials)}"
    if name == "error":
        return {"reason": "Did not finish", "typical": "—", "acceptance": "A finished result", "compared": "Stopped before a result", "misses": share}
    if not values:
        return {"reason": name, "typical": "Not reported", "acceptance": "—", "compared": "No number was reported", "misses": share}
    typical = float(median(values))
    if name == "profit":
        bar = float(limits["min_profit"])
        return {
            "reason": "Profit",
            "typical": _money(typical),
            "acceptance": f"Above {_money(bar)}",
            "compared": f"{_money(bar - typical)} under the line",
            "misses": share,
        }
    if name == "factor":
        bar = float(limits["profit_factor"])
        compared = f"{typical / bar:.2f} times the bar" if bar and typical > 0 else "At or below the bar"
        return {"reason": "Profit factor", "typical": _plain(typical), "acceptance": f"Above {_plain(bar)}", "compared": compared, "misses": share}
    if name == "drawdown":
        pct = float(limits.get("drawdown_pct") or 0)
        compared = f"{typical / cap:.2f} times the cap" if cap else "Over the cap"
        return {
            "reason": "Drawdown",
            "typical": _money(typical),
            "acceptance": f"Under {_money(cap)} ({_plain(pct)} percent of the account)",
            "compared": compared,
            "misses": share,
        }
    if name == "trades":
        bar = float(limits["min_trades"])
        compared = f"{typical / bar:.2f} times that count" if bar else "Under the count"
        return {
            "reason": "Trade count",
            "typical": f"{_plain(typical)} trades",
            "acceptance": f"At least {_plain(bar)}",
            "compared": compared,
            "misses": share,
        }
    if name == "pace":
        compared = f"{typical / TRADES_YEAR_MAX:.2f} times that cap" if TRADES_YEAR_MAX else "Over the cap"
        return {
            "reason": "Trades a year",
            "typical": f"{_plain(typical)} a year",
            "acceptance": f"At most {_plain(float(TRADES_YEAR_MAX))}",
            "compared": compared,
            "misses": share,
        }
    bar = float(limits["win_rate"])
    compared = f"{typical / bar:.2f} times that rate" if bar and typical > 0 else "At or below the rate"
    return {
        "reason": "Win rate",
        "typical": f"{_plain(typical)} percent",
        "acceptance": f"Above {_plain(bar)} percent",
        "compared": compared,
        "misses": share,
    }


def _line(name: str, values: list[float], limits: dict, cap: float, misses: int, trials: int) -> str:
    from futuresfund.objective import TRADES_YEAR_MAX

    share = f"{_count(misses)} of {_count(trials)} backtests miss it."
    if name == "error":
        return f"The backtest did not finish. {_count(misses)} of {_count(trials)} backtests stopped before a result."
    if not values:
        return f"{name}. The result did not report a number. {share}"
    typical = float(median(values))
    if name == "profit":
        bar = float(limits["min_profit"])
        return (
            f"Profit. The typical miss made {_money(typical)} versus acceptance above {_money(bar)}. "
            f"That is {_money(bar - typical)} under the line. {share}"
        )
    if name == "factor":
        bar = float(limits["profit_factor"])
        times = f" That is {typical / bar:.2f} times the bar." if bar and typical > 0 else ""
        return f"Profit factor. The typical miss is {_plain(typical)} versus acceptance above {_plain(bar)}.{times} {share}"
    if name == "drawdown":
        pct = float(limits.get("drawdown_pct") or 0)
        times = f" That is {typical / cap:.2f} times the cap." if cap else ""
        return (
            f"Drawdown. The typical miss is {_money(typical)} versus acceptance under {_money(cap)} "
            f"({_plain(pct)} percent of the account).{times} {share}"
        )
    if name == "trades":
        bar = float(limits["min_trades"])
        times = f" That is {typical / bar:.2f} times that count." if bar else ""
        return (
            f"Trade count. The typical miss has {_plain(typical)} trades versus acceptance of at least {_plain(bar)}."
            f"{times} {share}"
        )
    if name == "pace":
        times = f" That is {typical / TRADES_YEAR_MAX:.2f} times that cap." if TRADES_YEAR_MAX else ""
        return (
            f"Trades a year. The typical miss takes {_plain(typical)} trades a year versus acceptance of at most "
            f"{_plain(float(TRADES_YEAR_MAX))}.{times} {share}"
        )
    bar = float(limits["win_rate"])
    times = f" That is {typical / bar:.2f} times that rate." if bar and typical > 0 else ""
    return (
        f"Win rate. The typical miss wins {_plain(typical)} percent versus acceptance above {_plain(bar)} percent."
        f"{times} {share}"
    )


def measure(notes: Path, limits: dict, cap: float, limit: int = 3) -> dict:
    """The acceptance boxes the finished backtests miss most often."""
    buckets: dict[str, list[float]] = {name: [] for name in _ORDER}
    counts = {name: 0 for name in _ORDER}
    trials = 0
    passed = 0
    if notes.is_dir():
        for path in notes.glob("*.json"):
            if path.name in _SKIP or path.name.startswith("kronos"):
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
                missed = _misses(row, limits, cap)
                if not missed:
                    passed += 1
                    continue
                for name, value in missed.items():
                    counts[name] = counts.get(name, 0) + 1
                    if value is not None:
                        buckets.setdefault(name, []).append(float(value))
    ranked = sorted(
        (name for name, count in counts.items() if count),
        key=lambda name: (-counts[name], _ORDER.index(name) if name in _ORDER else len(_ORDER)),
    )
    lines = [
        _line(name, buckets.get(name) or [], limits, cap, counts[name], trials)
        for name in ranked[:limit]
    ]
    rows = [
        _row(name, buckets.get(name) or [], limits, cap, counts[name], trials)
        for name in ranked[:limit]
    ]
    return {"ready": True, "trials": trials, "passed": passed, "lines": lines, "rows": rows}


def _refresh() -> None:
    from futuresfund.book import strategy_acceptance
    from futuresfund.learn import NOTES
    from futuresfund.objective import account_drawdown_cap

    try:
        payload = measure(NOTES, strategy_acceptance(), account_drawdown_cap())
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
