"""Research backtests run through PineTS.

PineTS is the Pine runtime from https://github.com/LuxAlgo/PineTS. The desk
sends the script and the candles to a Node process and reads the closed
trades back. Profit uses the contract point value. Commission and slippage
are what the script's strategy() sets, and nothing when it sets neither,
which is what TradingView's Strategy Tester charts.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from futuresfund.contracts import POINT_VALUE, TICK_SIZE

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / "pinets_run.mjs"


class PineTSError(RuntimeError):
    """PineTS could not run the script."""


class PineTSScriptError(PineTSError):
    """PineTS ran and the script itself raised, such as an invalid session string. Running it again gives the same error."""


def _timeframe(timeframe: str) -> str:
    text = str(timeframe or "1").strip().lower()
    if text.endswith("m"):
        return text[:-1] or "1"
    if text.endswith("h"):
        return str(int(float(text[:-1] or "1")) * 60)
    if text.endswith("d"):
        return f"{text[:-1] or '1'}D"
    return text


def _clock(millis) -> str:
    try:
        stamp = int(millis) / 1000
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M")


_EXIT_ID = re.compile(r"strategy\.exit\s*\(\s*(?:id\s*=\s*)?[\"']([^\"']+)[\"']")
_ON_CLOSE = re.compile(r"\bprocess_orders_on_close\s*=\s*true\b")


def _bar_path(bar: dict) -> list[float]:
    """TradingView's broker emulator inside one bar: the open, the nearer extreme, the other extreme, the close."""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if abs(o - h) <= abs(o - l):
        return [o, h, l, c]
    return [o, l, h, c]


def _path_to(bar: dict, price: float) -> list[float]:
    """The prices a bar visits until a stop or limit fills at price."""
    points = _bar_path(bar)
    seen = []
    for here, there in zip(points, points[1:]):
        seen.append(here)
        if min(here, there) <= price <= max(here, there):
            seen.append(price)
            return seen
    return points


def _excursions(raw: dict, candles: list[dict], point: float, on_close: bool, exit_orders: set[str]) -> tuple[float, float] | None:
    """The trade's adverse and favorable excursion in dollars, as the Strategy Tester lists them.

    PineTS's own max_drawdown does not follow TradingView's bar path, so the
    path is walked here. A fill at the bar open counts that whole bar; a fill at
    the close starts on the next bar. A strategy.exit stop or limit counts its
    bar up to the fill. A market exit counts the open it filled at, or the whole
    bar when orders fill on the close. The entry commission is in both numbers.
    """
    try:
        first = int(raw["entry_bar_index"])
        last = int(raw["exit_bar_index"])
        entry = float(raw["entry_price"])
        exit_price = float(raw["exit_price"])
        size = float(raw.get("size") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if size == 0 or first < 0 or last >= len(candles) or last < first:
        return None
    seen: list[float] = []
    start = first + 1 if on_close else first
    for index in range(start, last):
        seen.extend((candles[index]["high"], candles[index]["low"]))
    if last >= start:
        bar = candles[last]
        if raw.get("exit_id") in exit_orders:
            seen.extend(_path_to(bar, exit_price))
        elif on_close:
            seen.extend(_bar_path(bar))
        elif last > first:
            seen.append(bar["open"])
        else:
            seen.extend(_bar_path(bar))
    seen.append(exit_price)
    side = 1.0 if size > 0 else -1.0
    worst = min(seen) if side > 0 else max(seen)
    best = max(seen) if side > 0 else min(seen)
    qty = abs(size)
    fee = float(raw.get("commission") or 0) / 2.0
    adverse = min(0.0, (worst - entry) * side * point * qty) - fee
    favorable = max(0.0, (best - entry) * side * point * qty) - fee
    return round(adverse, 2), round(favorable, 2)


def _trade(raw: dict, excursion: tuple[float, float] | None = None) -> dict | None:
    if not isinstance(raw, dict) or raw.get("exit_time") is None or raw.get("profit") is None:
        return None
    adverse = raw.get("max_drawdown")
    try:
        adverse = -abs(float(adverse)) if adverse is not None else None
    except (TypeError, ValueError):
        adverse = None
    favorable = None
    if excursion is not None:
        adverse, favorable = excursion
    try:
        size = float(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0.0
    return {
        "entry_time": _clock(raw.get("entry_time")),
        "exit_time": _clock(raw.get("exit_time")),
        "side": "long" if size > 0 else "short",
        "entry_price": raw.get("entry_price"),
        "exit_price": raw.get("exit_price"),
        "pnl": round(float(raw["profit"]), 2),
        "size": raw.get("size"),
        "entry_signal": raw.get("entry_comment") or raw.get("entry_id") or "",
        "exit_signal": raw.get("exit_comment") or raw.get("exit_id") or "",
        "adverse": adverse,
        "favorable": favorable,
    }


def _payload(text: str) -> dict | None:
    """The runner's JSON object. Log lines can sit in front of it and must not hide the trades."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(raw.splitlines()):
        piece = line.strip()
        if not piece.startswith("{"):
            continue
        try:
            value = json.loads(piece)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def run_pinets(source: str, bars, timeframe: str = "2m", root: str = "ES") -> list[dict]:
    """Closed trades for one script. Each bar needs epoch, o, h, l, c, and v."""
    symbol = str(root or "ES").upper()
    candles = []
    for bar in bars or []:
        candles.append({
            "open": float(bar.o),
            "high": float(bar.h),
            "low": float(bar.l),
            "close": float(bar.c),
            "volume": float(bar.v or 0),
            "openTime": int(bar.epoch) * 1000,
        })
    if not candles:
        return []
    if not RUNNER.is_file():
        raise PineTSError("The PineTS runner is not installed.")
    payload = {
        "source": source or "",
        "candles": candles,
        "symbol": symbol,
        "timeframe": _timeframe(timeframe),
        "pointvalue": float(POINT_VALUE.get(symbol, 50)),
        "mintick": float(TICK_SIZE.get(symbol, 0.25)),
    }
    try:
        completed = subprocess.run(
            ["node", str(RUNNER)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(ROOT),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PineTSError("PineTS did not finish within 10 minutes.") from exc
    except OSError as exc:
        raise PineTSError("Node is not available, so PineTS cannot run.") from exc
    result = _payload(completed.stdout or "")
    if result is None:
        # A crash prints its reason last, after any log lines the script wrote.
        detail = (completed.stderr or completed.stdout or "PineTS returned nothing.").strip()
        raise PineTSError(detail[-500:] or "PineTS returned nothing.")
    if result.get("error"):
        raise PineTSScriptError(str(result["error"])[:500])
    if completed.returncode != 0:
        raise PineTSError(((completed.stderr or "").strip()[-500:]) or "PineTS stopped.")
    text = source or ""
    on_close = _ON_CLOSE.search(text) is not None
    exit_orders = set(_EXIT_ID.findall(text))
    trades = []
    for raw in result.get("trades") or []:
        trade = _trade(raw, _excursions(raw, candles, payload["pointvalue"], on_close, exit_orders))
        if trade is not None:
            trades.append(trade)
    return trades
