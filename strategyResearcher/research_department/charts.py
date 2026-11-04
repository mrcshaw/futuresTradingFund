"""Chart sessions for the research desk. The live trading queue is not used."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


CHARTS = Path(__file__).resolve().parents[1] / "chartData"
ET = ZoneInfo("America/New_York")
_SHIFT = timedelta(hours=6)
_CACHE: dict[str, list] = {}
_LOCK = threading.Lock()

_FOLDERS = (
    ("MNQ", "mnq"),
    ("MES", "mes"),
    ("NQ", "nq"),
    ("ES", "es"),
    ("GOLD", "gold"),
    ("GC", "gc"),
)
_SYMBOLS = {"GOLD": "GC"}


@dataclass(frozen=True)
class Bar:
    epoch: int
    o: float
    h: float
    l: float
    c: float
    v: float


def symbol_of(instrument: str) -> str:
    key = (instrument or "").upper().strip()
    if key in _SYMBOLS:
        return _SYMBOLS[key]
    for name, _folder in _FOLDERS:
        if key.startswith(name) and name != "GOLD":
            return name
    raise ValueError(f"{instrument} is not a research chart.")


def folder_of(instrument: str) -> Path:
    key = (instrument or "").upper().strip()
    for name, folder in _FOLDERS:
        if key == name or (name != "GOLD" and key.startswith(name)):
            return CHARTS / folder
    raise ValueError(f"{instrument} is not a research chart.")


def session_day(epoch: int):
    """The futures date. A session opens at 6pm New York and belongs to the next date."""
    when = datetime.fromtimestamp(int(epoch), timezone.utc).astimezone(ET)
    return (when + _SHIFT).date()


def cut_sessions(instrument: str, count: int, timeframe: str) -> tuple[list[Bar], list]:
    """The last complete sessions, read from the end of the chart file."""
    path = _cache_file(instrument)
    raw = _tail(path, max(count * 4000, 8000))
    return _window(raw, count, timeframe)


def load_sessions(instrument: str, count: int, timeframe: str) -> tuple[list[Bar], list]:
    """The last `count` sessions of the cached one-minute series."""
    path = _cache_file(instrument)
    with _LOCK:
        cached = _CACHE.get(str(path))
    if cached is None:
        cached = _read_all(path)
        with _LOCK:
            _CACHE[str(path)] = cached
    return _window(cached, count, timeframe)


def _cache_file(instrument: str) -> Path:
    path = folder_of(instrument) / "databento-1m.csv"
    if not path.is_file():
        raise FileNotFoundError(f"No one-minute chart for {instrument}.")
    return path


def _window(bars: list[Bar], count: int, timeframe: str) -> tuple[list[Bar], list]:
    grouped: dict = {}
    order = []
    for bar in bars:
        key = session_day(bar.epoch)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(bar)
    picked = [grouped[key] for key in order if len(grouped[key]) >= 30][-count:]
    if len(picked) < count:
        raise ValueError(f"The chart has {len(picked)} sessions and the stage needs {count}.")
    dates = [session_day(day[0].epoch) for day in picked]
    flat = [bar for day in picked for bar in day]
    return _resample(flat, _minutes(timeframe)), dates


def _minutes(timeframe: str) -> int:
    text = str(timeframe or "").strip().lower()
    if text.endswith("m") and text[:-1].isdigit():
        return int(text[:-1])
    raise ValueError(f"{timeframe} is not a research engine.")


def _resample(bars: list[Bar], minutes: int) -> list[Bar]:
    width = minutes * 60
    buckets: dict[int, list[Bar]] = {}
    order: list[int] = []
    for bar in bars:
        stamp = bar.epoch - (bar.epoch % width)
        if stamp not in buckets:
            buckets[stamp] = []
            order.append(stamp)
        buckets[stamp].append(bar)
    built = []
    for stamp in order:
        group = buckets[stamp]
        built.append(Bar(
            stamp,
            group[0].o,
            max(item.h for item in group),
            min(item.l for item in group),
            group[-1].c,
            sum(item.v for item in group),
        ))
    return built


def _tail(path: Path, count: int) -> list[Bar]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        window = min(size, count * 64)
        while True:
            start = max(0, size - window)
            handle.seek(start)
            data = handle.read(window)
            if data.count(b"\n") > count or start == 0:
                break
            window = min(size, window * 2)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0:
        lines = lines[1:]
    return _rows(lines)


def _read_all(path: Path) -> list[Bar]:
    rows = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for line in handle:
            parsed = _rows([line])
            if parsed:
                rows.append(parsed[0])
    return rows


def _rows(lines) -> list[Bar]:
    rows = []
    for line in lines:
        parts = line.strip().split(",")
        if len(parts) < 6 or not parts[0][:1].isdigit():
            continue
        try:
            rows.append(Bar(
                int(float(parts[0])),
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
                float(parts[4]),
                float(parts[5]),
            ))
        except ValueError:
            continue
    return rows
