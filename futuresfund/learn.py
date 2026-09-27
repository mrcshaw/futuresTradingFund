"""Study the Pine files on disk. Adjust parameters on the engine, keep the notes, then file the script as learned."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

from futuresfund.pine_report import ceo_report, pine_facts
from futuresfund.prop_rules import passes

ROOT = Path(__file__).resolve().parent
LEARNING = ROOT / "learningStrategies"
LEARNED = ROOT / "learnedStrategies"
NOTES = ROOT / "researchNotes"
ENGINE_ROOT = ROOT.parent.parent / "tradingEngine"
ATTEMPT_LIMIT = 200
ACCOUNT_SIZE = 50000
FROZEN = {
    "initial_capital",
    "point_value",
    "tick_size",
    "commission_value",
    "slippage",
    "default_qty_value",
    "pyramiding",
}


def candidates(base: dict, limit: int = ATTEMPT_LIMIT) -> list[dict]:
    """Original settings first, then one change at a time, never past the attempt cap."""
    origin = {key: value for key, value in base.items() if _tunable_value(value)}
    plans = [dict(origin)]
    seen = {_key(origin)}
    for name, value in origin.items():
        if name in FROZEN:
            continue
        for nxt in neighbors(name, value):
            if len(plans) >= limit:
                return plans
            plan = dict(origin)
            plan[name] = nxt
            mark = _key(plan)
            if mark in seen:
                continue
            seen.add(mark)
            plans.append(plan)
    return plans


def neighbors(name: str, value):
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int) and not isinstance(value, bool):
        raw = [int(value * scale) for scale in (0.5, 0.75, 1.25, 1.5, 2)]
        raw += [value - 2, value - 1, value + 1, value + 2]
        out = []
        for item in raw:
            item = _clamp(name, item)
            if item != value and item not in out and item > 0:
                out.append(item)
        return out
    if isinstance(value, float):
        out = []
        for scale in (0.5, 0.75, 1.25, 1.5, 2):
            item = round(value * scale, 4)
            if item != value and item not in out and item > 0:
                out.append(item)
        return out
    return []


def queue(learning: Path | None = None) -> list[Path]:
    """Pine scripts waiting to be studied. A python twin is studied with its pine, not as a second job."""
    folder = learning or LEARNING
    if not folder.is_dir():
        return []
    pines = sorted(path for path in folder.glob("*.pine") if path.is_file())
    used = set()
    jobs = []
    for pine in pines:
        jobs.append(pine)
        twin = companion(pine)
        if twin:
            used.add(twin.resolve())
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("test_") or path.name.startswith("_"):
            continue
        if path.resolve() in used:
            continue
        jobs.append(path)
    return jobs


def companion(pine: Path) -> Path | None:
    direct = pine.with_suffix(".py")
    if direct.is_file() and not direct.name.startswith("test_"):
        return direct
    alt = pine.with_name(pine.stem + "_strategy.py")
    if alt.is_file():
        return alt
    return None


def run_learning(board) -> None:
    """Keep studying until the learning folder is empty. A meeting pauses the engine between attempts."""
    from futuresfund.research import load_bars
    from futuresfund.session import learning_pause

    LEARNED.mkdir(parents=True, exist_ok=True)
    NOTES.mkdir(parents=True, exist_ok=True)
    board.post(
        "Quantitative Researcher",
        "The learning folder is the source. Each script gets at most 200 engine attempts. Notes stay in the research notes.",
        kind="report",
        channel="Quantitative Researcher",
    )
    import os
    if not os.environ.get("SMTP_PASSWORD", "").strip():
        board.post(
            "Portfolio Manager",
            "Research reports are addressed to thefutureoffuturestrading@gmail.com. SMTP_PASSWORD is empty, so Gmail has not accepted one yet. Each report is still saved in the research notes.",
            kind="report",
            channel="Portfolio Manager",
        )
    while not board.cancel.is_set():
        if not _wait(board, learning_pause):
            return
        jobs = queue()
        if not jobs:
            board.post(
                "Quantitative Researcher",
                "The learning folder is empty. The notes in research notes stay available. The engine will pick up a script if one is added.",
                kind="report",
                channel="headquarters",
            )
            if not _sleep(board, learning_pause, 30):
                return
            continue
        path = jobs[0]
        board.set_status("Quantitative Researcher", "working")
        board.set_status("Indicator Researcher", "working")
        board.set_activity("Quantitative Researcher", f"Studying {path.name}")
        try:
            _study(board, path, load_bars(), learning_pause)
        except Exception as exc:
            board.post(
                "Quantitative Developer",
                f"{path.name} stopped on an engine error: {exc}. The notes record the error and the script is filed so the next one can start.",
                kind="error",
                channel="Quantitative Developer",
            )
            traceback.print_exc()
            _save_notes(path, {"title": path.stem}, [], f"The engine stopped: {exc}", "No indicator pass was completed.", {})
            twin = companion(path) if path.suffix == ".pine" else None
            if path.is_file():
                _file_away(path, twin)


def study_parameters(base: dict, execute, account_size: float = ACCOUNT_SIZE, limit: int = ATTEMPT_LIMIT) -> list[dict]:
    """Run adjustments until one clears the trail or the attempt cap is reached."""
    trials = []
    for params in candidates(base, limit):
        measured = execute(params) or {}
        row = {
            "params": params,
            "net_profit": measured.get("net_profit"),
            "max_drawdown": measured.get("max_drawdown"),
            "trades": measured.get("trades"),
            "win_rate": measured.get("win_rate"),
            "error": measured.get("error"),
        }
        row["passed"] = _passed(row, account_size)
        trials.append(row)
        if row["passed"]:
            break
    return trials


def adjustment_notes(trials: list[dict]) -> tuple[str, str]:
    """A table the portfolio manager can put in the report without another pass."""
    if not trials:
        return "The engine recorded no attempts.", "The engine recorded no attempts."
    origin = trials[0]
    by_profit = sorted(trials, key=lambda row: float(row.get("net_profit") or -10**12), reverse=True)
    made = [row for row in trials if float(row.get("net_profit") or 0) > 0]
    lost = [row for row in trials if float(row.get("net_profit") or 0) < 0]
    winner = max(made, key=lambda row: float(row["net_profit"])) if made else None
    loser = min(lost, key=lambda row: float(row["net_profit"])) if lost else None
    lines = [
        "Attempt results. Each row changes one setting from the original.",
        "",
        f"{'Change':<44} {'Profit':>14} {'Drawdown':>14} {'Trades':>8} {'Trail':>7}",
        f"{'-' * 44} {'-' * 14} {'-' * 14} {'-' * 8} {'-' * 7}",
        _note_row("Original", origin),
    ]
    for row in by_profit[:5]:
        if row is origin:
            continue
        lines.append(_note_row(_change_label(origin, row), row))
    lines.append("")
    lines.append("Most profitable change")
    lines.append(_note_row(_change_label(origin, by_profit[0]), by_profit[0]))
    lines.append("")
    lines.append("A change that made money" if winner else "No change made money")
    if winner:
        lines.append(_note_row(_change_label(origin, winner), winner))
    lines.append("")
    lines.append("A change that lost money" if loser else "No change lost money")
    if loser:
        lines.append(_note_row(_change_label(origin, loser), loser))
    quant = "\n".join(lines)
    indicator = (
        "Indicator and filter settings were changed one at a time. "
        f"The most profitable settings are {_short_params((by_profit[0].get('params') or {}))}. "
        f"Attempts recorded: {len(trials)}."
    )
    return quant, indicator


def _note_row(label: str, row: dict) -> str:
    trail = "yes" if row.get("passed") else "no"
    return (
        f"{label:<44} {_money(row.get('net_profit')):>14} {_money(row.get('max_drawdown')):>14} "
        f"{str(row.get('trades')):>8} {trail:>7}"
    )


def _change_label(origin: dict, row: dict) -> str:
    keys = [key for key in origin.get("params") or {} if row.get("params", {}).get(key) != origin["params"].get(key)]
    if not keys:
        return "Original"
    if len(keys) == 1:
        key = keys[0]
        return f"{key} {origin['params'].get(key)} to {row['params'].get(key)}"
    return "several settings"


def _money(value) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def notes_digest(limit: int = 20) -> str:
    path = NOTES / "LESSONS.md"
    if not path.is_file():
        return "No learned strategy notes yet."
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _study(board, path: Path, bars: list, pause) -> None:
    text = path.read_text(encoding="utf-8", errors="replace") if path.suffix == ".pine" else ""
    twin = companion(path) if path.suffix == ".pine" else path
    facts = pine_facts(text) if text else {"title": path.stem, "inputs": [], "named": {}}
    base, execute = _executor(twin, bars, facts)
    trials = []
    for params in candidates(base):
        if not _wait(board, pause):
            return
        measured = execute(params)
        row = {
            "params": params,
            "net_profit": measured.get("net_profit"),
            "max_drawdown": measured.get("max_drawdown"),
            "trades": measured.get("trades"),
            "win_rate": measured.get("win_rate"),
            "error": measured.get("error"),
            "runner": measured.get("runner"),
        }
        row["passed"] = _passed(row, ACCOUNT_SIZE)
        trials.append(row)
        _document(board, facts, path, row, len(trials))
        if row["passed"]:
            break
    quant, indicator = adjustment_notes(trials)
    best = max(trials, key=_rank) if trials else {}
    _save_notes(path, facts, trials, quant, indicator, best)
    report = ceo_report(facts, {"measured": best, "passed": best.get("passed"), "params": best.get("params")}, quant, indicator, len(trials))
    _send(board, facts.get("title") or path.stem, report)
    _file_away(path, twin if twin and twin != path else None)
    board.post(
        "Quantitative Researcher",
        f"Filed {path.name} after {len(trials)} attempts. Notes remain for the next strategy.",
        kind="report",
        channel="headquarters",
    )
    board.set_status("Quantitative Researcher", "done")
    board.set_status("Indicator Researcher", "done")


def _executor(path: Path | None, bars: list, facts: dict):
    cls = _load_class(path) if path else None
    if cls is not None:
        frame = _frame(bars)
        base = _class_params(cls)

        def execute(params: dict) -> dict:
            try:
                from engine.backtester import Backtester

                report = Backtester(frame, cls, **params).run(progress=False)
                metrics = report.metrics
                return {
                    "net_profit": float(metrics.get("net_profit") or 0),
                    "max_drawdown": abs(float(metrics.get("max_drawdown") or 0)),
                    "trades": int(metrics.get("total_trades") or 0),
                    "win_rate": metrics.get("win_rate"),
                    "runner": cls.__name__,
                }
            except Exception as exc:
                return {"error": str(exc), "runner": cls.__name__}

        return base, execute
    name = _family(facts, path)
    base = _family_base(name)

    def execute(params: dict) -> dict:
        from futuresfund.research import evaluate_rule

        try:
            row = evaluate_rule(bars, "ES1!", ACCOUNT_SIZE, 3000, name, params)
        except Exception as exc:
            return {"error": str(exc), "runner": name}
        if not row:
            return {"error": "The bars were too short for this rule.", "runner": name}
        backtest = row["backtest"]
        return {
            "net_profit": backtest.get("net_profit"),
            "max_drawdown": backtest.get("max_drawdown"),
            "trades": backtest.get("trades"),
            "win_rate": backtest.get("win_rate"),
            "runner": name,
        }

    return base, execute


def _family(facts: dict, path: Path | None) -> str:
    blob = f"{facts.get('title') or ''} {path.stem if path else ''}".lower()
    if "bollinger" in blob or "mean" in blob:
        return "bollinger"
    if "macd" in blob:
        return "macd"
    if "supertrend" in blob:
        return "supertrend"
    if "rsi" in blob:
        return "rsi_revert"
    if "vwap" in blob:
        return "vwap_side"
    if "poc" in blob:
        return "poc_pullback"
    if "break" in blob or "orb" in blob or "donchian" in blob:
        return "donchian"
    return "ema_cross"


def _family_base(name: str) -> dict:
    bases = {
        "ema_cross": {"fast": 12, "slow": 50, "stop": 250},
        "bollinger": {"length": 20, "dev": 2, "stop": 250},
        "macd": {"fast": 12, "slow": 26, "signal": 9, "stop": 250},
        "supertrend": {"length": 10, "mult": 3, "stop": 250},
        "rsi_revert": {"period": 14, "low": 30, "high": 70, "stop": 250},
        "vwap_side": {"length": 20, "stop": 250},
        "poc_pullback": {"lookback": 50, "rows": 12, "stop": 250},
        "donchian": {"length": 20, "stop": 250},
    }
    return dict(bases.get(name) or bases["ema_cross"])


def _load_class(path: Path):
    if not path or not path.is_file():
        return None
    engine = str(ENGINE_ROOT)
    if engine not in sys.path:
        sys.path.insert(0, engine)
    try:
        spec = importlib.util.spec_from_file_location(f"learn_{path.stem}", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    from engine.strategy import Strategy

    found = [
        obj for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, Strategy) and obj is not Strategy
    ]
    return found[0] if found else None


def _class_params(cls) -> dict:
    params = {}
    for key, value in vars(cls).items():
        if key.startswith("_") or key in FROZEN:
            continue
        if _tunable_value(value):
            params[key] = value
    return params


def _frame(bars: list):
    import pandas as pd
    from engine.data_loader import DataLoader

    frame = pd.DataFrame({
        "datetime": [bar.get("t") for bar in bars],
        "open": [float(bar["o"]) for bar in bars],
        "high": [float(bar["h"]) for bar in bars],
        "low": [float(bar["l"]) for bar in bars],
        "close": [float(bar["c"]) for bar in bars],
        "volume": [float(bar.get("v") or 0) for bar in bars],
    })
    return DataLoader._normalize(frame)


def _document(board, facts: dict, path: Path, row: dict, number: int) -> None:
    from futuresfund.lab import record_trial

    title = f"{facts.get('title') or path.stem} attempt {number}"
    record_trial({
        "id": f"{path.stem}-{number}",
        "title": title,
        "rule": {"name": row.get("runner")},
        "proven": bool(row.get("passed")),
        "backtest": {
            "net_profit": row.get("net_profit"),
            "max_drawdown": row.get("max_drawdown"),
            "trades": row.get("trades"),
        },
    }, title)
    if number == 1 or number % 10 == 0 or row.get("passed"):
        board.post(
            "Quantitative Developer",
            f"Engine {number}: {path.name} profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.",
            kind="report",
            channel="Quantitative Developer",
        )


def _save_notes(path: Path, facts: dict, trials: list, quant: str, indicator: str, best: dict) -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    slug = path.stem.replace(" ", "_")
    payload = {
        "file": path.name,
        "title": facts.get("title") or path.stem,
        "attempts": len(trials),
        "best": best,
        "quantitative_researcher": quant,
        "indicator_researcher": indicator,
        "trials": trials,
    }
    (NOTES / f"{slug}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    lesson = (
        f"{payload['title']}: attempts {len(trials)}, "
        f"best profit {best.get('net_profit')}, drawdown {best.get('max_drawdown')}, "
        f"trail {'cleared' if best.get('passed') else 'not cleared'}. {quant}"
    )
    with (NOTES / "LESSONS.md").open("a", encoding="utf-8") as handle:
        handle.write(lesson + "\n")


def _send(board, title: str, report: str) -> None:
    from futuresfund.mailer import send_report

    folder = NOTES / "mail"
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() else "_" for ch in title)[:80]
    (folder / f"{safe}.txt").write_text(report, encoding="utf-8")
    result = send_report(report, subject=f"Futures fund research report: {title}")
    board.post(
        "Portfolio Manager",
        report if result.get("sent") else f"{report}\n\n{result.get('reason')}",
        kind="report",
        channel="Portfolio Manager",
    )


def _file_away(path: Path, twin: Path | None) -> None:
    LEARNED.mkdir(parents=True, exist_ok=True)
    _move(path)
    if twin and twin.is_file():
        _move(twin)


def _move(path: Path) -> None:
    target = LEARNED / path.name
    if target.exists():
        target = LEARNED / f"{path.stem}_{int(time.time())}{path.suffix}"
    shutil.move(str(path), str(target))


def _passed(row: dict, account_size: float) -> bool:
    if row.get("error") or row.get("net_profit") is None or row.get("max_drawdown") is None:
        return False
    return passes(
        account_size,
        net_profit=float(row["net_profit"]),
        max_drawdown=float(row["max_drawdown"]),
        trades=int(row.get("trades") or 0),
    )


def _rank(row: dict):
    profit = float(row.get("net_profit") or -10**12)
    drawdown = float(row.get("max_drawdown") or 10**12)
    if row.get("passed"):
        return (2, profit, -drawdown)
    if profit > 0:
        return (1, profit - drawdown, -drawdown)
    return (0, profit, -drawdown)


def _better(row: dict, origin: dict) -> bool:
    profit = float(row.get("net_profit") or -10**12)
    base = float(origin.get("net_profit") or -10**12)
    drawdown = float(row.get("max_drawdown") or 10**12)
    base_dd = float(origin.get("max_drawdown") or 10**12)
    return profit > base and drawdown <= base_dd


def _wait(board, pause) -> bool:
    while pause.is_set():
        board.set_activity("Portfolio Manager", "Meeting in progress. Strategy study is paused.")
        if board.cancel.is_set():
            return False
        time.sleep(1)
    return not board.cancel.is_set()


def _sleep(board, pause, seconds: int) -> bool:
    for _ in range(seconds):
        if not _wait(board, pause):
            return False
        time.sleep(1)
    return True


def _tunable_value(value) -> bool:
    return isinstance(value, (bool, int, float)) and not isinstance(value, str)


def _clamp(name: str, value: int) -> int:
    lowered = name.lower()
    if "hour" in lowered or lowered.endswith("_hr"):
        return max(0, min(23, value))
    if "minute" in lowered or lowered.endswith("_mn"):
        return max(0, min(59, value))
    return value


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, default=str)


def _short_params(params: dict) -> str:
    parts = [f"{key}={value}" for key, value in sorted(params.items())[:8]]
    return ", ".join(parts) if parts else "the original inputs"
