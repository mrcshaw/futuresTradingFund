"""Run a Pine script in the PineForge engine.

The desk calls the official release image, which transpiles the Pine file and
backtests it. Python strategy files are not used.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGE = "ghcr.io/pineforge-4pass/pineforge-release:latest"
_ENGINE_LOCK = threading.Lock()
_EASTERN = ZoneInfo("America/New_York")
_TIMEFRAMES = {"1m": "1", "2m": "2", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1D": "D"}


class PineForgeUnavailable(RuntimeError):
    """Docker or the PineForge image cannot run a backtest."""


def bars_to_csv(bars: list[dict]) -> str:
    """PineForge wants timestamp, open, high, low, close, volume. Chart times are US Eastern."""
    lines = ["timestamp,open,high,low,close,volume"]
    for bar in bars:
        stamp = _epoch_ms(bar.get("t"))
        if stamp is None:
            continue
        volume = bar.get("v")
        volume = 0 if volume is None else volume
        lines.append(
            f"{stamp},{_num(bar.get('o'))},{_num(bar.get('h'))},{_num(bar.get('l'))},{_num(bar.get('c'))},{_num(volume)}"
        )
    return "\n".join(lines) + "\n"


def has_volume(bars: list[dict]) -> bool:
    return any(float(bar.get("v") or 0) > 0 for bar in bars)


def parse_report(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("PineForge returned an empty report.")
    try:
        found = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("PineForge did not return a report.") from None
        found = json.loads(raw[start:end + 1])
    if not isinstance(found, dict):
        raise ValueError("PineForge did not return a report.")
    return found


def metrics_from(report: dict) -> dict:
    """Trade count, profit, and drawdown from a PineForge report."""
    if report.get("error") and not report.get("summary"):
        return {
            "engine": "pineforge",
            "runner": "pineforge",
            "error": str(report.get("error")),
            "fatal": True,
            "net_profit": None,
            "max_drawdown": None,
            "trades": 0,
        }
    summary = report.get("summary") or {}
    drawdown = summary.get("max_drawdown")
    return {
        "engine": "pineforge",
        "runner": "pineforge",
        "net_profit": float(summary.get("net_pnl") or 0),
        "max_drawdown": abs(float(drawdown or 0)),
        "trades": int(summary.get("total_trades") or 0),
        "win_rate": summary.get("win_rate_pct"),
        "wins": summary.get("wins"),
        "losses": summary.get("losses"),
    }


def run_script(source: str, bars: list[dict], timeframe: str, inputs: dict | None, cancel) -> dict:
    """Backtest one Pine script. A stop kills the container before the next chart."""
    from futuresfund.pine_compat import prepare_for_pineforge

    if cancel is not None and cancel.is_set():
        return _stopped()
    text, fixes = prepare_for_pineforge(source or "")
    if "volume" in (source or "").lower() and not has_volume(bars):
        return {
            "engine": "pineforge",
            "runner": "pineforge",
            "blocked": True,
            "net_profit": 0.0,
            "max_drawdown": 0.0,
            "trades": 0,
            "note": "The chart file has no volume column, so this script cannot enter. The extra column on the 5-minute and 15-minute files is an EMA, not volume.",
        }
    note = " ".join(fixes) if fixes else ""
    es_note = (
        "ES is $50 a point and the tick is 0.25. "
        "The account is the amount written in the script. "
        "A 5 percent margin is applied so that account can hold one ES contract, the same way TradingView does."
    )
    text = _with_futures_margin(text)
    note = f"{note} {es_note}".strip()
    with _ENGINE_LOCK:
        result = _run_once(text, bars, timeframe, inputs, cancel)
    if note and not result.get("note"):
        result["note"] = note
    elif note and result.get("note"):
        result["note"] = f"{result['note']} {note}"
    return result


def _run_once(source: str, bars: list[dict], timeframe: str, inputs: dict | None, cancel) -> dict:
    if cancel is not None and cancel.is_set():
        return _stopped()
    chart = _TIMEFRAMES.get(timeframe, timeframe)
    payload = {str(key): _input_text(value) for key, value in (inputs or {}).items() if _input_text(value) is not None}
    name = f"pineforge-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="pineforge-") as folder:
        root = Path(folder)
        pine = root / "strategy.pine"
        csv_path = root / "ohlcv.csv"
        pine.write_text(source, encoding="utf-8")
        csv_path.write_text(bars_to_csv(bars), encoding="utf-8")
        syminfo = root / "syminfo.json"
        syminfo.write_text(
            '{"mintick": 0.25, "pointvalue": 50, "timezone": "America/New_York"}',
            encoding="utf-8",
        )
        command = [
            "docker", "run", "--name", name, "--rm",
            "-v", f"{pine}:/in/strategy.pine:ro",
            "-v", f"{csv_path}:/in/ohlcv.csv:ro",
            "-v", f"{syminfo}:/in/syminfo.json:ro",
            "-e", f"PINEFORGE_INPUT_TF={chart}",
            "-e", f"PINEFORGE_SCRIPT_TF={chart}",
            "-e", f"PINEFORGE_INPUTS={json.dumps(payload)}",
            "-e", "PINEFORGE_SYMINFO=/in/syminfo.json",
            "-e", "PINEFORGE_CHART_TZ=America/New_York",
            IMAGE,
        ]
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise PineForgeUnavailable("Docker is not installed, so PineForge cannot run the Pine script.") from exc
        try:
            stdout, stderr = _wait(proc, name, cancel)
        except FileNotFoundError as exc:
            raise PineForgeUnavailable("Docker is not installed, so PineForge cannot run the Pine script.") from exc
    code = proc.returncode
    if cancel is not None and cancel.is_set():
        return _stopped()
    if code == 0:
        try:
            found = metrics_from(parse_report(stdout))
        except ValueError as exc:
            return {"engine": "pineforge", "runner": "pineforge", "error": str(exc), "fatal": True, "net_profit": None, "max_drawdown": None, "trades": 0}
        return found
    detail = _failure_text(stdout, stderr)
    if code is None or "docker" in detail.lower() and "cannot" in detail.lower():
        raise PineForgeUnavailable(detail or "PineForge did not start.")
    if "Unable to find image" in detail or "Cannot connect to the Docker daemon" in detail or "error during connect" in detail.lower():
        raise PineForgeUnavailable(detail)
    return {
        "engine": "pineforge",
        "runner": "pineforge",
        "error": detail or f"PineForge stopped with exit code {code}.",
        "fatal": True,
        "net_profit": None,
        "max_drawdown": None,
        "trades": 0,
    }


def _wait(proc: subprocess.Popen, name: str, cancel):
    while True:
        try:
            return proc.communicate(timeout=0.4)
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                _kill(name, proc)
                return "", "stopped"
            if proc.poll() is not None:
                return proc.communicate()


def _kill(name: str, proc: subprocess.Popen) -> None:
    subprocess.run(
        ["docker", "kill", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _stopped() -> dict:
    return {
        "engine": "pineforge",
        "runner": "pineforge",
        "stopped": True,
        "net_profit": None,
        "max_drawdown": None,
        "trades": 0,
        "note": "Researchers stopped.",
    }


def _with_futures_margin(source: str) -> str:
    """Let the script's own account hold one ES contract.

    PineForge's default margin is 100 percent of the contract notional.
    One ES contract is about $390,000, so a $25,000 account would be refused.
    Five percent is about $19,600, which fits in that account, and the order size stays one contract.
    """
    if re.search(r"\bmargin_long\s*=", source):
        return source
    match = re.search(r"\bstrategy\s*\(", source)
    if not match:
        return source
    depth = 0
    for index in range(match.end() - 1, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[:index] + ", margin_long=5, margin_short=5" + source[index:]
    return source


def _failure_text(stdout: str, stderr: str) -> str:
    for text in (stdout, stderr):
        raw = (text or "").strip()
        if not raw:
            continue
        try:
            found = parse_report(raw)
        except ValueError:
            found = None
        if isinstance(found, dict) and found.get("error"):
            return str(found["error"])
    raw = (stderr or stdout or "").strip()
    line = raw.splitlines()[-1] if raw else ""
    return line[:500]


def _epoch_ms(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if number > 10_000_000_000 else number * 1000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_EASTERN)
    return int(parsed.timestamp() * 1000)


def _num(value) -> str:
    number = float(value or 0)
    return format(number, ".10g")


def _input_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format(value, ".10g")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
