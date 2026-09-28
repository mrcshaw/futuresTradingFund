"""ES chart exports. The live book stays on the 15-minute file. Research also reads 2-minute and 5-minute files."""

from __future__ import annotations

from pathlib import Path

from futuresfund.config import ROOT
from futuresfund.csv_bars import parse_tradingview_csv

# The token is the comma-space prefix in the TradingView file name, so 15 does not match 5 or 2.
_FRAMES = (("15m", ", 15_"), ("5m", ", 5_"), ("2m", ", 2_"))


def chart_paths() -> dict[str, Path]:
    grouped: dict[str, list[Path]] = {}
    seen: set[Path] = set()
    for pattern in ("CME_MINI_ES1*.csv", "CME_MINI_DL_ES1*.csv"):
        for path in ROOT.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            for frame, token in _FRAMES:
                if token in path.name:
                    grouped.setdefault(frame, []).append(path)
                    break
    chosen: dict[str, Path] = {}
    for frame, paths in grouped.items():
        with_volume = [path for path in paths if _header_has_volume(path)]
        pool = with_volume or paths
        chosen[frame] = max(pool, key=lambda path: path.stat().st_mtime)
    return chosen


def _header_has_volume(path: Path) -> bool:
    names = {"volume", "vol", "v"}
    try:
        line = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[0]
    except OSError:
        return False
    columns = [part.strip().lower().replace(" ", "") for part in line.split(",") if part.strip()]
    return any(column in names for column in columns)


def chart_columns() -> dict[str, list[str]]:
    """Header columns for each chart file."""
    found: dict[str, list[str]] = {}
    for name, path in chart_paths().items():
        line = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[0]
        found[name] = [part.strip() for part in line.split(",") if part.strip()]
    return found


_VOLUME_READY = {"stamp": None, "value": False}


def charts_have_volume() -> bool:
    """True when an engine chart actually has contract volume on its bars.

    Alerts can store volume without landing on the exported chart. Those bars do not
    make a volume script runnable.
    """
    stamp = _volume_stamp()
    if _VOLUME_READY["stamp"] == stamp:
        return bool(_VOLUME_READY["value"])
    names = {"volume", "vol", "v"}
    ready = any(column.lower().replace(" ", "") in names for columns in chart_columns().values() for column in columns)
    if not ready:
        ready = any(
            float(bar.get("v") or 0) > 0
            for timeframe in ("5m", "2m", "15m")
            for bar in load_chart(timeframe)
        )
    _VOLUME_READY["stamp"] = stamp
    _VOLUME_READY["value"] = ready
    return ready


def _volume_stamp() -> tuple:
    from futuresfund.config import BARS_PATH

    marks = [BARS_PATH.stat().st_mtime if BARS_PATH.is_file() else 0]
    for path in chart_paths().values():
        marks.append(path.stat().st_mtime if path.is_file() else 0)
    return tuple(marks)


def load_chart(timeframe: str) -> list[dict]:
    path = chart_paths().get(timeframe)
    if path is None:
        return []
    bars = parse_tradingview_csv(path.read_text(encoding="utf-8-sig", errors="replace"))
    return _with_recorded_volume(bars)


def _with_recorded_volume(bars: list[dict]) -> list[dict]:
    """Copy volume, profile, and delta from alerts onto the exported bars with the same time."""
    from futuresfund.pineforge_engine import _epoch_ms
    from futuresfund.research import load_bars

    recorded = {}
    for bar in load_bars():
        if float(bar.get("v") or 0) <= 0:
            continue
        stamp = _epoch_ms(bar.get("t"))
        if stamp is not None:
            recorded[stamp] = bar
    if not recorded:
        return bars
    for bar in bars:
        stamp = _epoch_ms(bar.get("t"))
        live = recorded.get(stamp)
        if not live:
            continue
        bar["v"] = live.get("v") or 0
        for key in ("poc", "poc_volume", "delta", "delta_pct"):
            if live.get(key) is not None:
                bar[key] = live[key]
    return bars
