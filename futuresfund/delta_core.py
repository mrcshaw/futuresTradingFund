"""Delta volume breakout: the golden strategy's core, and how far the desk has carried it.

A script has the core when it builds a volume profile over a lookback with up and down
volume, finds the point of control (POC), measures the net delta at the POC, and trades
the breakout of the POC zone with that delta on the same side. The chart engines study
these scripts first, and the strategy developers write only these.

The goal is a delta volume breakout result that clears the acceptance on every
instrument, on at least one of the three charts. coverage() is that scoreboard, and
next_target() points each strategy developer at an instrument that does not have one.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

_POC = re.compile(r"\bpoc", re.IGNORECASE)
_DELTA = re.compile(
    r"poc_delta|delta_min|min_delta|delta_pct|delta imbalance|delta_imbalance|delta_thresh|poc_up|poc_dn|poc_down",
    re.IGNORECASE,
)
_PROFILE = re.compile(
    r"profile_lookback|profile_rows|profile_len|vp_len|vp_rows|price levels|vol_up|vol_dn|up_vol|down_vol",
    re.IGNORECASE,
)
_CORE_CACHE: dict[str, tuple[int, int, bool]] = {}
_COVERAGE: dict = {"at": 0.0, "found": {}}
_COVERAGE_LOCK = threading.Lock()
COVERAGE_SECONDS = 300
_rounds: dict[str, int] = {}
_rounds_lock = threading.Lock()
_CHART_OFFSET = {"2m": 0, "5m": 1, "15m": 2}

CORE_TEXT = (
    "the delta volume breakout core of the golden strategy: a volume profile over a lookback with up volume and down volume "
    "on every price row, the point of control (POC) as the row with the most volume, the net delta at the POC as a percent "
    "of that row, and a breakout of the POC zone with the delta on the same side"
)


def has_core(source: str) -> bool:
    """True when the script builds the volume profile, its POC, and the POC delta."""
    text = source or ""
    return bool(_POC.search(text) and _DELTA.search(text) and _PROFILE.search(text))


def is_core_file(path: Path) -> bool:
    """has_core for a file on disk, read again only when the file changes."""
    try:
        info = path.stat()
    except OSError:
        return False
    key = str(path)
    kept = _CORE_CACHE.get(key)
    if kept is not None and kept[0] == info.st_mtime_ns and kept[1] == info.st_size:
        return kept[2]
    try:
        found = has_core(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        found = False
    _CORE_CACHE[key] = (info.st_mtime_ns, info.st_size, found)
    return found


def _folders() -> list[Path]:
    from futuresfund import learn

    return [learn.LEARNING, learn.LEARNED, learn.CREATED]


def _profit(row: dict) -> float:
    try:
        return float(row.get("net_profit"))
    except (TypeError, ValueError):
        return float("-inf")


def scan() -> dict[str, dict]:
    """The best delta volume breakout run on each instrument that clears the acceptance, on any of the three charts."""
    from futuresfund import learn
    from futuresfund.book import strategy_acceptance
    from futuresfund.objective import acceptance_misses

    limits = strategy_acceptance()
    found: dict[str, dict] = {}
    for folder in _folders():
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file() or not learn._strategy_text(path) or not is_core_file(path):
                continue
            notes = learn._read_notes(path)
            for root, record in (notes.get("instruments") or {}).items():
                charts = record.get("charts") if isinstance(record, dict) else None
                if not isinstance(charts, dict):
                    continue
                for timeframe, chart in charts.items():
                    if timeframe not in learn.RESEARCH_CHARTS or not isinstance(chart, dict):
                        continue
                    for row in chart.get("trials") or []:
                        if not isinstance(row, dict) or acceptance_misses(row, limits=limits):
                            continue
                        key = str(root).upper()
                        if key not in found or _profit(row) > _profit(found[key]):
                            found[key] = {
                                "script": path.name,
                                "folder": folder.name,
                                "timeframe": timeframe,
                                "net_profit": row.get("net_profit"),
                                "max_drawdown": row.get("max_drawdown"),
                                "trades": row.get("trades"),
                                "win_rate": row.get("win_rate"),
                                "profit_factor": row.get("profit_factor"),
                            }
    return found


def coverage(*, fresh: bool = False) -> dict[str, dict]:
    """scan(), kept for a few minutes so every chart pick does not read every note."""
    with _COVERAGE_LOCK:
        if not fresh and time.monotonic() - _COVERAGE["at"] < COVERAGE_SECONDS and _COVERAGE["at"]:
            return dict(_COVERAGE["found"])
    found = scan()
    with _COVERAGE_LOCK:
        _COVERAGE["at"] = time.monotonic()
        _COVERAGE["found"] = found
    return dict(found)


def uncovered(instruments: list[str] | None = None) -> list[str]:
    """Instruments with no delta volume breakout result that clears the acceptance, in research order."""
    from futuresfund.learn import research_instruments

    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    found = coverage()
    return [root for root in roots if root not in found]


def uncovered_first(roots: list[str]) -> list[str]:
    """The same instruments, the ones still without a result first. The order inside each group is kept."""
    try:
        missing = set(uncovered(list(roots)))
    except Exception:
        return list(roots)
    return [root for root in roots if root in missing] + [root for root in roots if root not in missing]


def next_target(timeframe: str) -> str:
    """The instrument this chart's strategy developer writes for next.

    The developers take turns over the instruments that still need a result, and the three
    charts start on different ones. When every instrument has one, they go round all of them.
    """
    from futuresfund.learn import research_instruments

    pool = uncovered() or [str(name).upper() for name in research_instruments()]
    if not pool:
        return "ES"
    with _rounds_lock:
        turn = _rounds.get(timeframe, 0)
        _rounds[timeframe] = turn + 1
    return pool[(turn + _CHART_OFFSET.get(timeframe, 0)) % len(pool)]


def golden_numbers() -> str:
    """The golden strategy's results in one sentence. Blank when no golden strategy is set."""
    from futuresfund.golden import results_line

    return results_line()


def coverage_lines(instruments: list[str] | None = None) -> list[str]:
    """One line per instrument: the best delta volume breakout result that clears the acceptance, or none yet."""
    from futuresfund.learn import research_instruments

    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    found = coverage()
    lines = []
    for root in roots:
        row = found.get(root)
        if row is None:
            lines.append(f"{root}: none yet.")
            continue
        lines.append(
            f"{root}: {row['script']} on {row['timeframe']}, profit {row['net_profit']}, drawdown {row['max_drawdown']}, "
            f"trades {row['trades']}, win rate {row['win_rate']}, profit factor {row['profit_factor']}."
        )
    return lines
