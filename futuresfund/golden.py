"""The golden strategy: how this desk trades.

The owner enters the idea, a Pine script that shows it, and the Strategy Tester export of
that script. The idea and the script are the standard for the researchers and the creation
team. The export's results are the bar: the desk is looking for a result like it on every
instrument, on one of the three charts. The engine runs the script and compares every
trade with the export, so the owner can see how closely it follows.

The file is researchNotes/golden/golden.json, with each export beside it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

CONFIRM_SHARE = 0.95
CONFIRM_TRADES = 20
_LOCK = threading.Lock()
_NUMBERS = ("net_profit", "max_drawdown", "trades", "win_rate", "profit_factor", "biggest_winner", "biggest_loser")


def _home() -> Path:
    from futuresfund import learn

    return Path(learn.NOTES) / "golden"


def _ledger() -> Path:
    return _home() / "golden.json"


def _load() -> dict:
    try:
        data = json.loads(_ledger().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": "", "rows": []}
    return data if isinstance(data, dict) and isinstance(data.get("rows"), list) else {"active": "", "rows": []}


def _write(data: dict) -> None:
    path = _ledger()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def rows() -> dict:
    with _LOCK:
        return _load()


def active() -> dict | None:
    with _LOCK:
        data = _load()
    return next((row for row in data["rows"] if row.get("id") == data.get("active")), None)


def confirmed(row: dict | None) -> bool:
    """The engine reproduced the export: at least 95 percent of the trades it has bars for, and enough of them to mean something."""
    check = (row or {}).get("check") or {}
    covered = int(check.get("covered") or 0)
    return covered >= CONFIRM_TRADES and int(check.get("matched") or 0) >= CONFIRM_SHARE * covered


def enter(pine: str, export_name: str, export_text: str, idea: str, *, wait: bool = False) -> dict:
    """File a golden strategy and make it the standard. The trade comparison runs on its own thread."""
    from futuresfund.backtrader_engine import load_tv_trades
    from futuresfund.pine_check import tv_problems
    from futuresfund.tv_broker import tv_metrics

    idea = " ".join((idea or "").split())
    if not idea:
        raise ValueError("Describe the idea of this strategy.")
    pine = (pine or "").strip() + "\n"
    if "strategy(" not in pine:
        raise ValueError("Paste the whole Pine strategy.")
    refused = tv_problems(pine)
    if refused:
        raise ValueError("TradingView would refuse this script. " + " ".join(refused[:3]))
    name = Path(str(export_name or "")).name
    if not name.lower().endswith(".csv") or not (export_text or "").strip():
        raise ValueError("Add the List of trades CSV that TradingView exported for this script.")
    key = hashlib.sha1((pine + export_text).encode("utf-8")).hexdigest()[:12]
    folder = _home() / key
    folder.mkdir(parents=True, exist_ok=True)
    export = folder / name
    export.write_text(export_text, encoding="utf-8")
    trades = load_tv_trades(export)
    if not trades:
        raise ValueError("The export has no closed trades.")
    numbers = tv_metrics(trades)
    title = re.search(r'strategy\s*\(\s*"([^"]+)"', pine)
    row = {
        "id": key,
        "title": title.group(1).strip() if title else Path(name).stem,
        "export": f"{key}/{name}",
        "idea": idea,
        "pine": pine,
        "numbers": {field: numbers.get(field) for field in _NUMBERS},
        "first_trade": trades[0]["entry_time"],
        "last_trade": max(trade["exit_time"] for trade in trades),
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
        "check": {"status": "checking"},
    }
    row.update(_chart_of(export))
    with _LOCK:
        data = _load()
        data["rows"] = [item for item in data["rows"] if item.get("id") != key] + [row]
        data["active"] = key
        _write(data)
    if wait:
        _check(key)
    else:
        threading.Thread(target=_check, args=(key,), daemon=True, name="golden-check").start()
    return row


def _chart_of(export: Path) -> dict:
    from futuresfund.tv_checks import _contract_for, _root_for, _timeframe

    contract = _contract_for(export.stem)
    return {
        "contract": contract[1] if contract else f"{_root_for(export.stem)}1!",
        "timeframe": _timeframe(export),
    }


def _check(key: str) -> None:
    """Run the script and compare each trade with the export."""
    from futuresfund.tv_checks import check_export

    row = next((item for item in rows()["rows"] if item.get("id") == key), None)
    if row is None:
        return
    try:
        report = check_export(_home() / row["export"], source=row["pine"], title=row["title"])
        check = {
            "status": "done",
            "error": report.get("error") or "",
            "expected": report.get("expected"),
            "covered": report.get("covered"),
            "matched": report.get("matched"),
            "extra": report.get("extra"),
            "export_profit": report.get("covered_profit"),
            "engine_profit": report.get("engine_covered_profit"),
            "inputs": report.get("inputs"),
        }
    except Exception as exc:  # the page reports it, the desk keeps running
        check = {"status": "done", "error": str(exc)}
    with _LOCK:
        data = _load()
        for item in data["rows"]:
            if item.get("id") == key:
                item["check"] = check
        _write(data)


def make_active(key: str) -> bool:
    with _LOCK:
        data = _load()
        if not any(item.get("id") == key for item in data["rows"]):
            return False
        data["active"] = key
        _write(data)
    return True


def remove(key: str) -> bool:
    """Take a golden strategy off the list. Its files stay in researchNotes/golden."""
    with _LOCK:
        data = _load()
        kept = [item for item in data["rows"] if item.get("id") != key]
        if len(kept) == len(data["rows"]):
            return False
        data["rows"] = kept
        if data.get("active") == key:
            data["active"] = kept[-1]["id"] if kept else ""
        _write(data)
    return True


def standard_line(row: dict | None = None) -> str:
    """The idea everyone builds on. Blank when no golden strategy is set."""
    row = row if row is not None else active()
    if not row or not row.get("idea"):
        return ""
    return (
        f"The golden strategy is how this desk trades: {row['idea']} "
        "The desk is looking for results like it on every instrument, on one of the three charts."
    )


def results_line(row: dict | None = None) -> str:
    """The golden strategy's Strategy Tester results in one sentence. Blank when none is set."""
    row = row if row is not None else active()
    numbers = (row or {}).get("numbers") or {}
    if numbers.get("net_profit") is None:
        return ""
    contract = str(row.get("contract") or "")
    try:
        from futuresfund.contracts import root_of

        contract = root_of(contract)
    except ValueError:
        pass
    span = ""
    if row.get("first_trade") and row.get("last_trade"):
        span = f" from {str(row['first_trade'])[:10]} to {str(row['last_trade'])[:10]}"
    chart = f" {row['timeframe']}" if row.get("timeframe") else ""
    return (
        f"The golden strategy made ${float(numbers['net_profit']):,.0f} with a ${float(numbers.get('max_drawdown') or 0):,.0f} drawdown "
        f"over {int(numbers.get('trades') or 0)} trades, a {float(numbers.get('win_rate') or 0):g}% win rate, and a "
        f"{float(numbers.get('profit_factor') or 0):g} profit factor on {contract}{chart}{span}."
    )


def creation_brief() -> str:
    """What the strategy developers read: the idea, its results, and the script that shows it."""
    row = active()
    line = standard_line(row)
    if not line:
        return ""
    results = results_line(row)
    head = f"{line} {results}" if results else line
    return (
        f"{head} This script shows the idea. "
        "Write a new script from the same idea. Do not copy it.\n"
        f"{row['pine']}"
    )
