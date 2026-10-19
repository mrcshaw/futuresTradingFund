"""Research backtests run through PineTS.

PineTS is the Pine runtime from https://github.com/LuxAlgo/PineTS. The desk
sends the script and the candles to a Node process and reads the closed
trades back. Profit uses the contract point value. Commission is $5.50 a
side and slippage is one tick when the script does not set them.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from futuresfund.contracts import POINT_VALUE

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / "pinets_run.mjs"
_MINTICK = {
    "ES": 0.25, "MES": 0.25, "NQ": 0.25, "MNQ": 0.25, "YM": 1.0,
    "RTY": 0.1, "QO": 0.25, "GC": 0.1, "MGC": 0.1, "CL": 0.01,
}


class PineTSError(RuntimeError):
    """PineTS could not run the script."""


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


def _trade(raw: dict) -> dict | None:
    if not isinstance(raw, dict) or raw.get("exit_time") is None or raw.get("profit") is None:
        return None
    adverse = raw.get("max_drawdown")
    try:
        adverse = -abs(float(adverse)) if adverse is not None else None
    except (TypeError, ValueError):
        adverse = None
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
    }


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
        "mintick": float(_MINTICK.get(symbol, 0.25)),
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
    text = (completed.stdout or "").strip()
    try:
        result = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        detail = (completed.stderr or text or "PineTS returned nothing.").strip()
        raise PineTSError(detail[:500]) from exc
    if completed.returncode != 0 or result.get("error"):
        raise PineTSError(str(result.get("error") or (completed.stderr or "PineTS stopped.").strip())[:500])
    trades = []
    for raw in result.get("trades") or []:
        trade = _trade(raw)
        if trade is not None:
            trades.append(trade)
    return trades
