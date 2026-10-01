"""Run a Pine script in the PineForge engine.

The desk calls the official release image, which transpiles the Pine file and
backtests it. Python strategy files are not used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGE = "ghcr.io/pineforge-4pass/pineforge-release:latest"
_ENGINE_SLOTS = 4
_CHART_FOR_SLOT = ("2m", "5m", "15m", "")
_OWNER_FOR_SLOT = (
    "2min chart developer",
    "5 min chart developer",
    "15 min chart developer",
    "Creation tester",
)
_ENGINE_LOCKS = [threading.Lock() for _ in range(_ENGINE_SLOTS)]
_ENGINE_GATE = threading.Condition()
_ENGINE_STATUS = [
    {
        "id": index + 1,
        "busy": False,
        "script": "",
        "timeframe": _CHART_FOR_SLOT[index],
        "instrument": "",
        "developer": _OWNER_FOR_SLOT[index],
    }
    for index in range(_ENGINE_SLOTS)
]
_WARM_NAMES = [f"pineforge-warm-{index + 1}" for index in range(_ENGINE_SLOTS)]
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


def engine_status() -> list[dict]:
    """What each warm PineForge container is running."""
    with _ENGINE_GATE:
        return [dict(row) for row in _ENGINE_STATUS]


def slot_for(timeframe: str) -> int:
    """Engine 1 is the 2-minute chart, engine 2 is the 5-minute chart, engine 3 is the 15-minute chart."""
    try:
        return _CHART_FOR_SLOT.index(timeframe)
    except ValueError:
        return _ENGINE_SLOTS - 1


def store_bars(folder: Path, instrument: str, timeframe: str, bars: list[dict]) -> Path:
    """Write this chart's bar file once. The same bars return the file already on disk."""
    folder.mkdir(parents=True, exist_ok=True)
    signature = _bar_signature(bars)
    path = folder / f"{_bar_stem(instrument, timeframe)}.csv"
    stamp = path.with_suffix(".stamp")
    if path.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == signature:
        return path
    path.write_text(bars_to_csv(bars), encoding="utf-8")
    stamp.write_text(signature, encoding="utf-8")
    return path


def warm_engines() -> None:
    """Start the four PineForge containers and leave them running."""
    for index in range(_ENGINE_SLOTS):
        _ensure_warm(index)


def _acquire_engine(script: str, timeframe: str, instrument: str, slot: int | None = None) -> int:
    index = slot if slot is not None else slot_for(timeframe)
    with _ENGINE_GATE:
        while not _ENGINE_LOCKS[index].acquire(blocking=False):
            _ENGINE_GATE.wait(timeout=0.4)
        _ENGINE_STATUS[index].update({
            "busy": True,
            "script": script,
            "timeframe": timeframe or _CHART_FOR_SLOT[index],
            "instrument": instrument,
            "developer": _OWNER_FOR_SLOT[index],
        })
        return index


def _release_engine(index: int) -> None:
    _ENGINE_STATUS[index].update({
        "busy": False,
        "script": "",
        "timeframe": _CHART_FOR_SLOT[index],
        "instrument": "",
        "developer": _OWNER_FOR_SLOT[index],
    })
    _ENGINE_LOCKS[index].release()
    with _ENGINE_GATE:
        _ENGINE_GATE.notify()


def run_script(source: str, bars: list[dict], timeframe: str, inputs: dict | None, cancel, instrument: str = "ES", label: str = "", slot: int | None = None) -> dict:
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
    syminfo, market_note = _market_note(instrument)
    percent = margin_percent(instrument, bars, text)
    text = _with_point_value(text, float(syminfo["pointvalue"]))
    text = _with_futures_margin(text, percent)
    market_note = (
        f"{market_note} Margin is {percent:g} percent so the account in the script "
        "can hold one contract at the highest price in this file."
    )
    note = f"{note} {market_note}".strip()
    slot = _acquire_engine(label or instrument, timeframe, instrument, slot)
    try:
        result = _run_once(text, bars, timeframe, inputs, cancel, syminfo, instrument, slot)
    finally:
        _release_engine(slot)
    result["engine_slot"] = slot + 1
    if note and not result.get("note"):
        result["note"] = note
    elif note and result.get("note"):
        result["note"] = f"{result['note']} {note}"
    return result


def _market_note(instrument: str) -> tuple[dict, str]:
    from futuresfund.contracts import POINT_VALUE, root_of, tick_size

    try:
        root = root_of(instrument)
    except ValueError:
        root = "ES"
    tick = tick_size(f"{root}1!")
    point = float(POINT_VALUE.get(root, 50))
    info = {"mintick": tick, "pointvalue": point, "timezone": "America/New_York"}
    if root == "ES":
        sentence = (
            "ES is $50 a point and the tick is 0.25. "
            "The account is the amount written in the script."
        )
    else:
        sentence = (
            f"{root} is ${point:g} a point and the tick is {tick:g}. "
            "The account is the amount written in the script."
        )
    return info, sentence


def _run_once(source: str, bars: list[dict], timeframe: str, inputs: dict | None, cancel, syminfo: dict | None = None, instrument: str = "ES", slot: int | None = None) -> dict:
    if cancel is not None and cancel.is_set():
        return _stopped()
    chart = _TIMEFRAMES.get(timeframe, timeframe)
    point = float((syminfo or {}).get("pointvalue") or 50)
    payload = {
        str(key): _input_text(_contract_point(key, value, point))
        for key, value in (inputs or {}).items()
        if _input_text(_contract_point(key, value, point)) is not None
    }
    index = slot if slot is not None else slot_for(timeframe)
    try:
        name = _ensure_warm(index)
    except FileNotFoundError as exc:
        raise PineForgeUnavailable("Docker is not installed, so PineForge cannot run the Pine script.") from exc
    slot = _slot_dir(index)
    _publish_text(slot / "strategy.pine", source)
    shared = store_bars(_bars_dir(), instrument, timeframe, bars)
    _publish_file(slot / "ohlcv.csv", shared)
    info = syminfo or {"mintick": 0.25, "pointvalue": 50, "timezone": "America/New_York"}
    _publish_text(slot / "syminfo.json", json.dumps(info))
    command = [
        "docker", "exec",
        "-e", f"PINEFORGE_INPUT_TF={chart}",
        "-e", f"PINEFORGE_SCRIPT_TF={chart}",
        "-e", f"PINEFORGE_INPUTS={json.dumps(payload)}",
        "-e", "PINEFORGE_SYMINFO=/in/syminfo.json",
        "-e", "PINEFORGE_CHART_TZ=America/New_York",
        name,
        "/opt/pineforge/bin/entrypoint.sh",
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
    """Stop the backtest and leave the warm container running."""
    subprocess.run(
        ["docker", "restart", "-t", "1", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=40,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _pineforge_root() -> Path:
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / ".futuresfund" / "pineforge"


def _bars_dir() -> Path:
    return _pineforge_root() / "bars"


def _slot_dir(index: int) -> Path:
    return _pineforge_root() / f"slot{index + 1}"


def _bar_stem(instrument: str, timeframe: str) -> str:
    root = re.sub(r"[^A-Za-z0-9]+", "", str(instrument or "ES")).upper() or "ES"
    chart = re.sub(r"[^A-Za-z0-9]+", "", str(timeframe or "5m")) or "5m"
    return f"{root}_{chart}"


def _bar_signature(bars: list[dict]) -> str:
    if not bars:
        return "0"
    last = bars[-1]
    return f"{len(bars)}|{bars[0].get('t')}|{last.get('t')}|{last.get('c')}"


def _publish_text(path: Path, text: str) -> None:
    """Write the file when its contents changed. A repeat of the same text is left in place."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stamp = path.with_suffix(path.suffix + ".stamp")
    if path.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == digest:
        return
    path.write_text(text, encoding="utf-8")
    stamp.write_text(digest, encoding="utf-8")


def _publish_file(dest: Path, source: Path) -> None:
    """Copy a bar file into the warm container's folder once per version of that file."""
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    stamp = dest.with_suffix(dest.suffix + ".stamp")
    if dest.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == digest:
        return
    shutil.copyfile(source, dest)
    stamp.write_text(digest, encoding="utf-8")


def _ensure_warm(index: int) -> str:
    """The container for this engine stays up. The next test is a docker exec, not a new container."""
    name = _WARM_NAMES[index]
    slot = _slot_dir(index)
    slot.mkdir(parents=True, exist_ok=True)
    if _container_running(name):
        return name
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
        creationflags=flags,
    )
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "--entrypoint", "bash",
            "-v", f"{slot}:/in",
            IMAGE,
            "-c", "while true; do sleep 3600; done",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
        creationflags=flags,
    )
    if proc.returncode != 0 or not _container_running(name):
        detail = (proc.stderr or proc.stdout or "").strip()
        raise PineForgeUnavailable(detail or "PineForge did not stay running.")
    return name


def _container_running(name: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


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


def margin_percent(instrument: str, bars: list[dict], source: str) -> float:
    """Percent margin that lets the script's account hold one contract of this file."""
    from futuresfund.contracts import POINT_VALUE, root_of

    try:
        root = root_of(instrument)
    except ValueError:
        root = "ES"
    point = float(POINT_VALUE.get(root, 50))
    price = _chart_price(bars)
    capital = _script_capital(source)
    if price <= 0 or point <= 0:
        return 5.0
    percent = (capital * 0.8) / (price * point) * 100
    return max(0.1, min(100.0, round(percent, 4)))


def _chart_price(bars: list[dict]) -> float:
    price = 0.0
    for bar in bars or []:
        for key in ("h", "c", "high", "close"):
            value = bar.get(key)
            if value is None:
                continue
            try:
                price = max(price, float(value))
            except (TypeError, ValueError):
                continue
    return price


def _script_capital(source: str) -> float:
    match = re.search(r"initial_capital\s*=\s*([0-9.]+)", source or "")
    if not match:
        return 25000.0
    try:
        return float(match.group(1))
    except ValueError:
        return 25000.0


def _with_point_value(source: str, point: float) -> str:
    """Dollar stops use this contract's point value. Nasdaq is 20, not the 50 written for ES."""
    rewritten = re.sub(
        r"((?:pv|point_value|pointvalue)\s*=\s*input\.float\(\s*)[-0-9.]+",
        lambda match: f"{match.group(1)}{point:g}",
        source or "",
        flags=re.IGNORECASE,
    )
    if rewritten == (source or "") or point == 50:
        return rewritten
    return f"// Engine point value for this contract: {point:g}\n" + rewritten


def _contract_point(key: str, value, point: float):
    token = re.sub(r"[^a-z0-9]+", "", str(key).lower())
    if token in {"pv", "pointvalue", "pointvalue"} or token.startswith("pointvalue"):
        return point
    return value


def _with_futures_margin(source: str, percent: float = 5) -> str:
    """Let the script's own account hold one contract.

    PineForge's default margin is 100 percent of the contract notional.
    The percent is chosen from this file's highest price and the contract point value.
    """
    if re.search(r"\bmargin_long\s*=", source):
        return source
    match = re.search(r"\bstrategy\s*\(", source)
    if not match:
        return source
    written = f"{percent:g}"
    depth = 0
    for index in range(match.end() - 1, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[:index] + f", margin_long={written}, margin_short={written}" + source[index:]
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
