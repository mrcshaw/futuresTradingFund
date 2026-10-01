"""Read a TradingView chart export. Columns are time, open, high, low, close, and volume."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone


def parse_tradingview_csv(text: str) -> list[dict]:
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        raise ValueError("The bar file was empty.")
    delimiter = ";" if raw.splitlines()[0].count(";") > raw.splitlines()[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("The bar file needs a header row.")
    fields = {_clean(name): name for name in reader.fieldnames if name}
    time_key = _pick(fields, ("time", "datetime", "date"))
    open_key = _pick(fields, ("open", "o"))
    high_key = _pick(fields, ("high", "h"))
    low_key = _pick(fields, ("low", "l"))
    close_key = _pick(fields, ("close", "c"))
    volume_key = _pick(fields, ("volume", "vol", "v"))
    macd_key = _pick(fields, ("macd",))
    histogram_key = _pick(fields, ("histogram",))
    signal_key = _pick(fields, ("signalline",))
    delta_key = _pick(fields, ("volumedelta(close)",))
    delta_high_key = _pick(fields, ("volumedelta(high", "volumedelta(high)"))
    delta_low_key = _pick(fields, ("volumedelta(low)",))
    clock_key = fields.get("time") if "date" in fields and fields.get("time") != fields.get(time_key or "") else None
    if "date" in fields and "time" in fields:
        time_key = fields["date"]
        clock_key = fields["time"]
    if not close_key or not time_key:
        raise ValueError("The bar file needs time and close columns.")
    bars = []
    for row in reader:
        stamp = _stamp(row.get(time_key), row.get(clock_key) if clock_key else None)
        close = _num(row.get(close_key))
        if stamp is None or close is None:
            continue
        high = _num(row.get(high_key)) if high_key else None
        low = _num(row.get(low_key)) if low_key else None
        opened = _num(row.get(open_key)) if open_key else None
        bar = {
            "t": stamp,
            "o": opened if opened is not None else close,
            "h": high if high is not None else close,
            "l": low if low is not None else close,
            "c": close,
            "v": _num(row.get(volume_key)) if volume_key else 0,
        }
        _keep(bar, "macd", row, macd_key)
        _keep(bar, "macd_histogram", row, histogram_key)
        _keep(bar, "macd_signal", row, signal_key)
        _keep(bar, "delta", row, delta_key)
        _keep(bar, "delta_high", row, delta_high_key)
        _keep(bar, "delta_low", row, delta_low_key)
        bars.append(bar)
    if len(bars) < 2:
        raise ValueError("The bar file did not contain enough candles.")
    bars.sort(key=lambda bar: bar["t"])
    unique = []
    for bar in bars:
        if unique and unique[-1]["t"] == bar["t"]:
            unique[-1] = bar
        else:
            unique.append(bar)
    return unique


def _clean(name: str) -> str:
    return name.strip().lower().replace(" ", "")


def _pick(fields: dict, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in fields:
            return fields[name]
    return None


def _keep(bar: dict, key: str, row: dict, source: str | None) -> None:
    if not source:
        return
    number = _num(row.get(source))
    if number is not None:
        bar[key] = number


def _num(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _stamp(date_value, clock_value) -> str | None:
    text = " ".join(part.strip() for part in (str(date_value or ""), str(clock_value or "")) if part and str(part).strip())
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number = number / 1000
        return datetime.fromtimestamp(number, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(text.replace("Z", "+0000") if fmt.endswith("%z") else text, fmt)
        except ValueError:
            continue
        return _keep_zone(parsed)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _keep_zone(parsed)


def _keep_zone(parsed: datetime) -> str:
    if parsed.tzinfo is not None:
        return parsed.isoformat(timespec="seconds")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")
