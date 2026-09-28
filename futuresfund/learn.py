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
from futuresfund.prop_rules import for_account

ROOT = Path(__file__).resolve().parent
LEARNING = ROOT / "learningStrategies"
LEARNED = ROOT / "learnedStrategies"
NOTES = ROOT / "researchNotes"
ENGINE_ROOT = ROOT.parent.parent / "tradingEngine"
ENGINE_CHART = "5m"
RESEARCH_CHARTS = ("5m", "2m", "15m")
ATTEMPT_LIMIT = 200
ACCOUNT_SIZE = 50000
PROFIT_TARGET = 3000.0
FROZEN = {
    "initial_capital",
    "point_value",
    "tick_size",
    "commission_value",
    "slippage",
    "default_qty_value",
    "pyramiding",
    "trailing_drawdown_limit",
    "profit_target",
    "daily_loss_limit",
}
_FILTERS: dict = {}


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


def research_slate(limit: int = 3) -> list[dict]:
    """The strategy being studied and the next ones waiting. No attempt log."""
    from futuresfund.charts import charts_have_volume
    from futuresfund.pine_report import pine_facts

    jobs = queue(skip_volume=not charts_have_volume())
    slate = []
    for path in jobs[:limit]:
        title = path.stem
        try:
            found = pine_facts(path.read_text(encoding="utf-8", errors="replace")).get("title") or ""
            if found and found != "Untitled strategy":
                title = found
        except OSError:
            pass
        slate.append({"title": title, "file": path.name})
    return slate


def queue(learning: Path | None = None, *, skip_volume: bool = False) -> list[Path]:
    """Pine scripts waiting to be studied. A python twin is studied with its pine, not as a second job."""
    folder = learning or LEARNING
    if not folder.is_dir():
        return []
    pines = sorted(path for path in folder.glob("*.pine") if path.is_file())
    if skip_volume:
        pines = [path for path in pines if not _script_uses_volume(path)]
    return pines


def _script_uses_volume(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "volume" in text.lower()


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
    board.set_research("running")
    try:
        _run_learning(board)
    finally:
        if board.cancel.is_set():
            board.post(
                "System",
                "Researchers stopped. The strategy was not changed and no order was sent.",
                kind="system",
                channel="headquarters",
            )
            board.acknowledge_stop()


def _run_learning(board) -> None:
    from futuresfund.session import learning_pause
    from futuresfund.pineforge_engine import PineForgeUnavailable

    LEARNED.mkdir(parents=True, exist_ok=True)
    NOTES.mkdir(parents=True, exist_ok=True)
    board.set_activity("Quantitative Developer", "Loading the 2-minute, 5-minute, and 15-minute charts")
    frames = _load_frames()
    from futuresfund.charts import chart_columns, charts_have_volume

    volume_ready = charts_have_volume()
    if not volume_ready:
        listed = "; ".join(f"{name} is {', '.join(cols)}" for name, cols in chart_columns().items())
        board.post(
            "Quantitative Developer",
            "The chart files have no volume column. "
            f"{listed}. The EMA numbers sit next to the price, so they are a moving average, not contract volume. "
            "A script that reads volume cannot enter on these bars, so those scripts stay in the learning folder. "
            "The price alert can send volume, the profile, and delta. They stay here until that data is on the bars.",
            kind="report",
            channel="Quantitative Developer",
        )
    from futuresfund.strategy import absorb_research_notes

    absorb_research_notes()
    loaded = ", ".join(f"{name} {len(rows)} bars" for name, rows in frames.items())
    board.post(
        "Quantitative Researcher",
        "The learning folder is only the starting point. Each script uses all 200 PineForge attempts, even after a version meets the profit target, so a better one can still be found. "
        "PineForge, from pineforge-engine, runs the Pine script. The 5-minute chart is the engine chart, and every attempt also runs the 2-minute and 15-minute charts. "
        f"Loaded {loaded}. "
        "A change that lowers profit is reversed, and a change that raises it is continued. "
        + _risk_line(),
        kind="report",
        channel="Quantitative Researcher",
    )
    import os
    from futuresfund.config import load_env

    load_env()
    if not os.environ.get("SMTP_PASSWORD", "").strip():
        board.post(
            "Portfolio Manager",
            "Research reports are addressed to thefutureoffuturestrading@gmail.com. SMTP_PASSWORD is empty, so Gmail has not accepted one yet. Each report is still saved in the research notes.",
            kind="report",
            channel="Portfolio Manager",
        )
    held_back: set[Path] = set()
    while not board.cancel.is_set():
        if not _wait(board, learning_pause):
            return
        jobs = [path for path in queue(skip_volume=not volume_ready) if path not in held_back]
        if not jobs:
            waiting = queue() if not volume_ready else []
            board.post(
                "Quantitative Researcher",
                (
                    f"{len(waiting)} scripts are still in the learning folder, and each one reads volume. "
                    "They stay there until the price alert has recorded volume on the bars."
                    if waiting
                    else "The learning folder is empty. The notes in research notes stay available. The engine will pick up a script if one is added."
                ),
                kind="report",
                channel="headquarters",
            )
            if not _sleep(board, learning_pause, 30):
                return
            continue
        path = jobs[0]
        from futuresfund.crew import get_crew

        crew = get_crew(board)
        crew.run("Quantitative Researcher", f"Studying {path.name}", lambda: None)
        crew.run("Indicator Researcher", f"Reading the indicators in {path.name}", lambda: None)
        crew.run("Quantitative Developer", f"Loading {path.name} into the shared engine", lambda: None)
        try:
            if _study(board, path, frames, learning_pause) == "blocked":
                held_back.add(path)
                board.post(
                    "Quantitative Researcher",
                    f"{path.name} needs volume on the chart bars. It stays in the learning folder, and the next script is next.",
                    kind="report",
                    channel="Quantitative Researcher",
                )
        except PineForgeUnavailable as exc:
            board.post(
                "Quantitative Developer",
                str(exc),
                kind="error",
                channel="Quantitative Developer",
            )
            board.cancel.set()
            return
        except Exception as exc:
            if board.cancel.is_set():
                return
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
    """Build off the last result until the profit target is met without crossing the trail."""
    return _search(base, execute, account_size, limit, None, None, None)


def next_plan(origin: dict, trials: list[dict], limit: int = ATTEMPT_LIMIT, *, extras: bool = True, extra_seen: set | None = None) -> dict | None:
    """Pick the next settings from the last profit change. A harmful step is reversed."""
    if len(trials) >= limit:
        return None
    seen = {_key(row.get("params") or {}) for row in trials}
    if extra_seen:
        seen.update(extra_seen)
    base = {key: value for key, value in origin.items() if key not in FROZEN and _tunable_value(value)}
    if not trials:
        return dict(base)

    last = trials[-1]
    previous = trials[-2] if len(trials) > 1 else None
    best = max(trials, key=lambda row: (_profit(row), -float(row.get("max_drawdown") or 10**12)))
    anchor = dict(best.get("params") or base)

    if previous is not None:
        changed = _changed_keys(previous.get("params") or {}, last.get("params") or {})
        if len(changed) == 1:
            name = changed[0]
            old = previous["params"].get(name)
            new = last["params"].get(name)
            delta = _profit(last) - _profit(previous)
            if delta < 0:
                flipped = _reverse_step(name, old, new)
                plan = dict(previous.get("params") or {})
                if flipped is not None:
                    plan[name] = flipped
                    if _key(plan) not in seen:
                        return plan
            elif delta > 0:
                nxt = _continue_step(name, old, new)
                plan = dict(last.get("params") or {})
                if nxt is not None:
                    plan[name] = nxt
                    if _key(plan) not in seen:
                        return plan

    for name, value in anchor.items():
        if name in FROZEN or name in {"added_indicator", "added_length"}:
            continue
        for nxt in _probe_steps(name, value):
            plan = dict(anchor)
            plan[name] = nxt
            if _key(plan) not in seen:
                return plan
    if extras:
        for extra in _creative(anchor):
            plan = dict(anchor)
            plan.update(extra)
            if _key(plan) not in seen:
                return plan
    return _widen(base, anchor, seen, len(trials), extras=extras)


def _search(base, execute, account_size, limit, board, path, pause, facts=None, extras: bool = True):
    origin = {key: value for key, value in base.items() if key not in FROZEN and _tunable_value(value)}
    trials = []
    seen = set()
    blocked = set()
    bounds = _input_bounds(facts)
    while len(trials) < limit:
        if board is not None and board.cancel.is_set():
            break
        if pause is not None and not _wait(board, pause):
            break
        attempt = len(trials) + 1
        label = path.name if path is not None else "the script"

        def choose():
            return next_plan(origin, trials, limit, extras=extras, extra_seen=blocked)

        if board is not None:
            from futuresfund.crew import get_crew

            raw = get_crew(board).run(
                "Quantitative Researcher",
                f"Choosing settings for attempt {attempt} on {label}",
                choose,
            )
        else:
            raw = choose()
        if raw is None:
            break
        plan = _clamp_plan(raw, bounds)
        mark = _key(plan)
        if mark in seen:
            blocked.add(_key(raw))
            if len(blocked) > 400:
                break
            continue
        seen.add(mark)
        measured = execute(plan) or {}
        row = {
            "params": plan,
            "net_profit": measured.get("net_profit"),
            "max_drawdown": measured.get("max_drawdown"),
            "trades": measured.get("trades"),
            "win_rate": measured.get("win_rate"),
            "error": measured.get("error"),
            "runner": measured.get("runner"),
            "stop_reason": measured.get("stop_reason") or "",
            "breached": bool(measured.get("breached")),
            "timeframe": measured.get("timeframe") or ENGINE_CHART,
            "frames": measured.get("frames") or [],
            "blocked": bool(measured.get("blocked")),
            "stopped": bool(measured.get("stopped")),
            "fatal": bool(measured.get("fatal")),
            "engine": measured.get("engine") or "",
        }
        row["passed"] = _passed(row, account_size)
        row["change"] = _change_text(trials[-1]["params"] if trials else None, plan)
        if row.get("take_profit") is None:
            row["take_profit"] = measured.get("take_profit", plan.get("tp_dollars"))
        if measured.get("note"):
            row["note"] = measured["note"]
        trials.append(row)
        if row.get("stopped"):
            break
        if board is not None and path is not None:
            _document(board, facts or {}, path, row, len(trials))
        if row.get("blocked") or row.get("fatal"):
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
        "Attempt results. Each row builds on the prior result, not a fresh copy of the original script.",
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
    best_params = by_profit[0].get("params") or {}
    added = best_params.get("added_indicator")
    extra = f" The best version adds a {added} filter on new entries." if added else ""
    indicator = (
        "The next setting follows the last result. A change that lowers profit is reversed. "
        "A change that raises profit is continued. When those knobs stall, a filter such as VWMA, EMA, or RSI is added. "
        f"The best version uses {_short_params(best_params)}.{extra} "
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


def _study(board, path: Path, frames: dict, pause) -> None:
    if path.suffix.lower() != ".pine":
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    facts = pine_facts(text) if text else {"title": path.stem, "inputs": [], "named": {}}
    from futuresfund.pine_compat import prepare_for_pineforge

    _, fixes = prepare_for_pineforge(text)
    if fixes:
        board.post(
            "Quantitative Developer",
            f"{path.name}: {' '.join(fixes)} The script posted on headquarters stays version 5.",
            kind="report",
            channel="Quantitative Developer",
        )
    execute = _pineforge_execute(text, frames, facts, board)
    trials = _search(_pine_params(facts), execute, ACCOUNT_SIZE, ATTEMPT_LIMIT, board, path, pause, facts, extras=False)
    if board.cancel.is_set() or (trials and trials[-1].get("stopped")):
        return
    if trials and trials[-1].get("blocked"):
        board.post(
            "Quantitative Developer",
            f"{path.name} was not filed. {trials[-1].get('note')}",
            kind="report",
            channel="Quantitative Developer",
        )
        return "blocked"
    quant, indicator = adjustment_notes(trials)
    best = max(trials, key=_rank) if trials else {}
    _save_notes(path, facts, trials, quant, indicator, best)
    _library_notice(board, facts, path, best)
    report = ceo_report(facts, {"measured": best, "passed": best.get("passed"), "params": best.get("params")}, quant, indicator, len(trials))
    _send(board, facts.get("title") or path.stem, report)
    _file_away(path, None)
    board.post(
        "Quantitative Researcher",
        f"Filed {path.name} after {len(trials)} PineForge attempts. Notes remain for the next strategy.",
        kind="report",
        channel="headquarters",
    )
    board.set_status("Quantitative Researcher", "done")
    board.set_status("Indicator Researcher", "done")


def _pine_params(facts: dict) -> dict:
    params = {}
    for item in facts.get("inputs") or []:
        if _tunable_value(item.get("default")):
            params[item["name"]] = item["default"]
    return params


def _input_overrides(facts: dict, params: dict) -> dict:
    by_name = {item["name"]: item for item in facts.get("inputs") or []}
    overrides = {}
    for key, value in params.items():
        item = by_name.get(key)
        if item:
            overrides[item.get("label") or key] = value
    return overrides


def _pineforge_execute(text: str, frames: dict, facts: dict, board):
    from futuresfund.pineforge_engine import run_script

    def execute(params: dict) -> dict:
        rows = []
        overrides = _input_overrides(facts, params)
        cancel = None if board is None else board.cancel
        for name in RESEARCH_CHARTS:
            if cancel is not None and cancel.is_set():
                return {"stopped": True, "runner": "pineforge", "engine": "pineforge", "net_profit": None, "max_drawdown": None, "trades": 0}
            bars = frames.get(name) or []
            if len(bars) < 80:
                rows.append({
                    "timeframe": name,
                    "error": f"The {name} chart is not loaded.",
                    "net_profit": None,
                    "max_drawdown": None,
                    "trades": 0,
                    "passed": False,
                    "runner": "pineforge",
                    "engine": "pineforge",
                })
                continue
            if board is not None:
                from futuresfund.crew import get_crew

                names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
                title = facts.get("title") or "the script"
                chart = names.get(name, name)

                def run_engine(bars=bars, chart_name=name, chart_label=chart):
                    return run_script(text, bars, chart_name, overrides, cancel)

                measured = get_crew(board).run(
                    "Quantitative Developer",
                    f"Starting the engine on the {chart} chart for {title}",
                    run_engine,
                ) or {}
            else:
                measured = run_script(text, bars, name, overrides, cancel)
            measured["timeframe"] = name
            measured["passed"] = _passed(measured, ACCOUNT_SIZE)
            measured["take_profit"] = params.get("tp_dollars") or facts.get("target")
            if measured.get("stopped") or measured.get("fatal"):
                return measured
            rows.append(measured)
        usable = [row for row in rows if row.get("net_profit") is not None]
        if not usable:
            return {"error": "The 2-minute, 5-minute, and 15-minute charts could not be loaded.", "fatal": True, "runner": "pineforge", "engine": "pineforge", "net_profit": None, "max_drawdown": None, "trades": 0}
        best = dict(_best_frame(usable))
        best["frames"] = [
            {
                "timeframe": row.get("timeframe"),
                "net_profit": row.get("net_profit"),
                "max_drawdown": row.get("max_drawdown"),
                "trades": row.get("trades"),
                "passed": bool(row.get("passed")),
            }
            for row in rows
        ]
        best["engine"] = "pineforge"
        best["runner"] = "pineforge"
        return best

    return execute


def _chart_blurb(frames) -> str:
    if not frames:
        return ""
    names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
    parts = [
        f"{names.get(item.get('timeframe'), item.get('timeframe'))} profit {item.get('net_profit')} "
        f"drawdown {item.get('max_drawdown')} trades {item.get('trades')}"
        for item in frames
    ]
    return " Charts: " + "; ".join(parts) + "."


def _load_frames() -> dict[str, list]:
    from futuresfund.charts import load_chart

    return {name: load_chart(name) for name in RESEARCH_CHARTS}


def _across_charts(path: Path | None, frames: dict, facts: dict):
    """One attempt runs on the 5-minute engine chart and on the 2-minute and 15-minute charts."""
    runners = {}
    base = None
    for name in RESEARCH_CHARTS:
        bars = frames.get(name) or []
        if len(bars) < 80:
            runners[name] = None
            continue
        found, execute = _executor(path, bars, facts)
        runners[name] = execute
        if base is None:
            base = found
    if base is None:
        raise ValueError("The 2-minute, 5-minute, and 15-minute charts could not be loaded.")

    def execute(params: dict) -> dict:
        rows = []
        for name in RESEARCH_CHARTS:
            runner = runners.get(name)
            if runner is None:
                rows.append({
                    "timeframe": name,
                    "error": f"The {name} chart is not loaded.",
                    "net_profit": None,
                    "max_drawdown": None,
                    "trades": 0,
                    "passed": False,
                })
                continue
            measured = runner(params) or {}
            measured["timeframe"] = name
            measured["passed"] = _passed(measured, ACCOUNT_SIZE)
            rows.append(measured)
        best = dict(_best_frame(rows))
        best["frames"] = [
            {
                "timeframe": row.get("timeframe"),
                "net_profit": row.get("net_profit"),
                "max_drawdown": row.get("max_drawdown"),
                "trades": row.get("trades"),
                "passed": bool(row.get("passed")),
            }
            for row in rows
        ]
        return best

    return base, execute


def _best_frame(rows: list[dict]) -> dict:
    """Keep the chart that cleared the goal. A tie stays on the 5-minute engine chart."""
    def key(row: dict):
        profit = row.get("net_profit")
        profit = float(profit) if profit is not None else -10**12
        drawdown = float(row.get("max_drawdown") or 10**12)
        return (1 if row.get("passed") else 0, profit, -drawdown, 1 if row.get("timeframe") == ENGINE_CHART else 0)

    return max(rows, key=key)


def _executor(path: Path | None, bars: list, facts: dict):
    cls = _load_class(path) if path else None
    no_volume = not _bars_have_volume(bars)
    if cls is not None:
        frame = _frame(bars)
        base = _class_params(cls)
        blocked = no_volume and _needs_volume(cls, facts, path)

        def execute(params: dict) -> dict:
            if blocked:
                return {
                    "net_profit": 0.0,
                    "max_drawdown": 0.0,
                    "trades": 0,
                    "runner": cls.__name__,
                    "take_profit": params.get("tp_dollars"),
                    "blocked": True,
                    "note": (
                        "The chart export has no volume column, so this script cannot enter. "
                        "A different strategy was not substituted."
                    ),
                }
            try:
                from engine.backtester import Backtester

                kind = params.get("added_indicator")
                runner = _with_indicator(cls, str(kind)) if kind else cls
                clean = {
                    key: value
                    for key, value in params.items()
                    if key not in {"added_indicator"}
                }
                clean["initial_capital"] = float(ACCOUNT_SIZE)
                clean["trailing_drawdown_limit"] = float(for_account(ACCOUNT_SIZE)["max_drawdown"])
                clean["profit_target"] = float(PROFIT_TARGET)
                report = Backtester(frame, runner, **clean).run(progress=False)
                metrics = report.metrics
                reason = str(getattr(report, "eval_stop_reason", "") or "")
                trades = int(metrics.get("total_trades") or 0)
                result = {
                    "net_profit": float(metrics.get("net_profit") or 0),
                    "max_drawdown": abs(float(metrics.get("max_drawdown") or 0)),
                    "trades": trades,
                    "win_rate": metrics.get("win_rate"),
                    "runner": runner.__name__,
                    "stop_reason": reason,
                    "breached": "Trailing DD" in reason,
                    "take_profit": params.get("tp_dollars"),
                }
                return result
            except Exception as exc:
                return {"error": str(exc), "runner": cls.__name__}

        return base, execute
    name = _family(facts, path)
    base = _family_base(name)

    def execute(params: dict) -> dict:
        from futuresfund.research import evaluate_rule

        try:
            usable = {
                key: value
                for key, value in params.items()
                if key not in {"added_indicator", "added_length"}
            }
            row = evaluate_rule(bars, "ES1!", ACCOUNT_SIZE, PROFIT_TARGET, name, usable)
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


def _bars_have_volume(bars: list) -> bool:
    return any(float(bar.get("v") or 0) > 0 for bar in bars)


def _needs_volume(cls, facts: dict, path: Path | None) -> bool:
    blob = " ".join([
        getattr(cls, "__name__", ""),
        str(facts.get("title") or ""),
        path.name if path else "",
    ]).lower()
    return any(word in blob for word in ("delta", "volume", "vwma", "obv", "poc", "profile"))


def _price_rule(params: dict, facts: dict, path: Path | None) -> tuple[str, dict, float]:
    kind = str(params.get("added_indicator") or "")
    raw_length = params.get("length") or params.get("profile_lookback") or params.get("ema_len") or 20
    try:
        length = int(float(raw_length))
    except (TypeError, ValueError):
        length = 20
    length = max(10, min(60, length))
    raw_stop = params.get("stop") or params.get("stop_dollars") or 250
    try:
        stop = float(raw_stop)
    except (TypeError, ValueError):
        stop = 250.0
    stop = max(50.0, min(2000.0, stop))
    raw_target = params.get("tp_dollars") or params.get("take_profit") or 0
    try:
        take_profit = float(raw_target)
    except (TypeError, ValueError):
        take_profit = 0.0
    if kind in {"ema", "sma", "wma", "vwma"}:
        fast = max(5, min(20, max(5, length // 3)))
        slow = max(fast + 5, min(120, max(length, fast + 10)))
        return "ema_cross", {"fast": fast, "slow": slow, "stop": stop}, take_profit
    if kind == "rsi":
        period = max(8, min(21, length // 3 or 14))
        return "rsi_revert", {"period": period, "low": 30, "high": 70, "stop": stop}, take_profit
    return "donchian", {"length": length, "stop": stop}, take_profit


def _price_result(bars: list, params: dict, facts: dict, path: Path | None) -> dict:
    from futuresfund.research import evaluate_rule

    name, rule, take_profit = _price_rule(params, facts, path)
    note = "These bars have no volume, so a delta rule cannot enter. This attempt trades price with the script's stop and take profit."
    try:
        row = evaluate_rule(bars, "ES1!", ACCOUNT_SIZE, PROFIT_TARGET, name, rule, qty=1, take_profit=take_profit or None)
    except Exception as exc:
        return {"error": str(exc), "runner": name, "take_profit": take_profit, "note": note}
    if not row:
        return {"error": "The price rule could not run on these bars.", "runner": name, "take_profit": take_profit, "note": note}
    backtest = row["backtest"]
    return {
        "net_profit": backtest.get("net_profit"),
        "max_drawdown": backtest.get("max_drawdown"),
        "trades": backtest.get("trades"),
        "win_rate": backtest.get("win_rate"),
        "runner": name,
        "breached": bool(backtest.get("breached")),
        "stop_reason": "Trailing DD breached" if backtest.get("breached") else "",
        "take_profit": take_profit,
        "note": note,
    }


def _change_text(before: dict | None, after: dict) -> str:
    if not before:
        return "Starting settings"
    keys = _changed_keys(before, after)
    if not keys:
        return "Same settings"
    shown = keys[:3]
    return ", ".join(f"{key} {before.get(key)} to {after.get(key)}" for key in shown)


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
    change = row.get("change") or "Starting settings"
    timeframe = row.get("timeframe") or ENGINE_CHART
    names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
    label = names.get(str(timeframe), str(timeframe))
    charts = _chart_blurb(row.get("frames"))
    take_profit = row.get("take_profit")
    target = f" Take profit ${float(take_profit):,.0f}." if isinstance(take_profit, (int, float)) and float(take_profit) > 0 else ""
    note = f" {row.get('note')}" if row.get("note") else ""
    if board is not None:
        from futuresfund.crew import get_crew

        crew = get_crew(board)
        crew.run(
            "Quantitative Researcher",
            f"Recording attempt {number} of {ATTEMPT_LIMIT} on {path.name}",
            lambda: None,
        )
        crew.run(
            "Indicator Researcher",
            f"Reviewing attempt {number} of {ATTEMPT_LIMIT} on {path.name}: profit {row.get('net_profit')}, trades {row.get('trades')}",
            lambda: None,
        )
    from futuresfund.strategy import remember_research

    remember_research(row, path.name, facts.get("title") or path.stem)
    board.post(
        "Quantitative Developer",
        f"Attempt {number} of {ATTEMPT_LIMIT} on the {label} chart: {path.name}. {change}. "
        f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.{target} "
        f"{_attempt_note(row)}{note}{charts}",
        kind="report",
        channel="Quantitative Developer",
    )


def _library_notice(board, facts: dict, path: Path, best: dict) -> None:
    """A finished profitable study goes on the strategy library. The analyst records the account."""
    title = facts.get("title") or path.stem
    try:
        profit = float(best.get("net_profit"))
    except (TypeError, ValueError):
        profit = None
    trades = int(best.get("trades") or 0)
    if profit is None or profit <= 0 or trades < 1 or str(best.get("engine") or best.get("runner") or "") != "pineforge":
        if board is not None:
            board.post(
                "Trading Analyst",
                f"{title} finished testing and was not added to the strategy library. "
                f"Profit {best.get('net_profit')}, trades {best.get('trades')}.",
                kind="report",
                channel="Trading Analyst",
            )
        return
    from futuresfund.library import contract_of

    _symbol, label = contract_of(str(title), path.name, best.get("contract"))
    note = (
        f"{title} is in the {label} strategy library. "
        f"{best.get('timeframe')} profit {profit}, drawdown {best.get('max_drawdown')}, trades {trades}."
    )
    if board is not None:
        board.post("Quantitative Researcher", note, kind="report", channel="Quantitative Researcher")
        board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")


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

    def deliver():
        return send_report(report, subject=f"Futures fund research report: {title}")

    from futuresfund.crew import get_crew

    result = get_crew(board).run(
        "Portfolio Manager",
        f"Sending the research report for {title}",
        deliver,
    ) or {}
    if result.get("sent"):
        lead = f"Emailed the research report for {title} to {result.get('to')}."
        board.set_activity("Portfolio Manager", f"Emailed the research report for {title}")
    else:
        lead = result.get("reason") or "The report was not emailed."
        board.set_activity("Portfolio Manager", "The research report was not emailed")
    board.post(
        "Portfolio Manager",
        f"{lead}\n\n{report}",
        kind="report",
        channel="Portfolio Manager",
    )


def _file_away(path: Path, twin: Path | None) -> None:
    """Only the Pine script is filed. The Python copy stays put."""
    if path.suffix.lower() != ".pine":
        return
    LEARNED.mkdir(parents=True, exist_ok=True)
    _move(path)


def _move(path: Path) -> None:
    target = LEARNED / path.name
    if target.exists():
        target = LEARNED / f"{path.stem}_{int(time.time())}{path.suffix}"
    shutil.move(str(path), str(target))


def _risk_line() -> str:
    from futuresfund.prop_rules import active_limits

    rules = active_limits(ACCOUNT_SIZE)
    kind = "trailing" if rules["trailing"] else "fixed"
    return (
        f"The ${rules['max_drawdown']:,.0f} {kind} drawdown must not be crossed "
        f"before the ${rules['profit_target']:,.0f} profit target is met."
    )


def _passed(row: dict, account_size: float) -> bool:
    """The profit target has to be met, and the trailing drawdown must not have been crossed first."""
    if row.get("error") or row.get("net_profit") is None or row.get("max_drawdown") is None:
        return False
    from futuresfund.prop_rules import active_limits

    rules = active_limits(account_size)
    trail = float(rules["max_drawdown"])
    target = float(rules["profit_target"])
    if bool(row.get("breached")) or "Trailing DD" in str(row.get("stop_reason") or ""):
        return False
    if float(row["max_drawdown"]) > trail:
        return False
    if "Profit target" in str(row.get("stop_reason") or ""):
        return int(row.get("trades") or 0) >= 1
    return float(row["net_profit"]) >= target and int(row.get("trades") or 0) >= 1


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


def _input_bounds(facts) -> dict:
    bounds = {}
    for item in (facts or {}).get("inputs") or []:
        if item.get("min") is not None or item.get("max") is not None:
            bounds[item["name"]] = (item.get("min"), item.get("max"))
    return bounds


def _clamp_plan(plan: dict | None, bounds: dict) -> dict | None:
    if not plan:
        return plan
    limited = {}
    for key, value in plan.items():
        limited[key] = _limit_value(key, value, bounds)
    return limited


def _limit_value(name: str, value, bounds: dict):
    """Keep a setting inside the input's min and max so TradingView can load the script."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, int):
        value = _clamp(name, int(value))
    low, high = bounds.get(name, (None, None))
    number = float(value)
    if low is not None:
        number = max(float(low), number)
    if high is not None:
        number = min(float(high), number)
    if isinstance(value, int):
        return int(round(number))
    return round(number, 4)


def _key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, default=str)


def _short_params(params: dict) -> str:
    parts = [f"{key}={value}" for key, value in sorted(params.items())[:8]]
    return ", ".join(parts) if parts else "the original inputs"


def _attempt_note(row: dict) -> str:
    if row.get("breached") or "Trailing DD" in str(row.get("stop_reason") or ""):
        return "The trailing drawdown was crossed before the profit target."
    if row.get("passed"):
        return "The profit target was met without crossing the trailing drawdown. The search continues to improve on it."
    return "The profit target is not met yet."


def _profit(row: dict) -> float:
    value = row.get("net_profit")
    if value is None:
        return -10**12
    return float(value)


def _changed_keys(before: dict, after: dict) -> list[str]:
    names = set(before) | set(after)
    return sorted(name for name in names if before.get(name) != after.get(name))


def _probe_steps(name: str, value):
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int) and not isinstance(value, bool):
        raw = [value - 1, value + 1, value - 2, value + 2, value - 4, value + 4, int(value * 0.5), int(value * 1.5), int(value * 2)]
        return _fresh_steps(name, value, raw)
    if isinstance(value, float):
        raw = [round(value * scale, 4) for scale in (0.8, 1.25, 0.5, 1.5, 2)]
        return _fresh_steps(name, value, raw)
    return []


def _fresh_steps(name: str, value, raw) -> list:
    out = []
    for item in raw:
        if isinstance(value, int) and not isinstance(value, bool):
            item = _clamp(name, int(item))
        if item == value or item in out:
            continue
        if isinstance(item, (int, float)) and item <= 0:
            continue
        out.append(item)
    return out


def _reverse_step(name: str, old, new):
    """If decreasing hurt, the next value increases, and the other way around."""
    if isinstance(old, bool) or not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return None
    delta = new - old
    if delta == 0:
        return None
    flipped = old - delta
    if isinstance(old, int) and not isinstance(old, bool):
        flipped = _clamp(name, int(round(flipped)))
    else:
        flipped = round(float(flipped), 4)
    if flipped == old or flipped == new or flipped <= 0:
        return None
    return flipped


def _continue_step(name: str, old, new):
    if isinstance(old, bool) or not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return None
    delta = new - old
    if delta == 0:
        return None
    nxt = new + delta
    if isinstance(old, int) and not isinstance(old, bool):
        nxt = _clamp(name, int(round(nxt)))
    else:
        nxt = round(float(nxt), 4)
    if nxt == new or nxt <= 0:
        return None
    return nxt


def _creative(anchor: dict) -> list[dict]:
    if anchor.get("added_indicator"):
        return []
    return [
        {"added_indicator": kind, "added_length": length}
        for kind in ("vwma", "ema", "sma", "wma", "rsi")
        for length in (8, 14, 20, 21, 34, 50, 55, 100, 200)
    ]


_INDICATORS = ("vwma", "ema", "sma", "wma", "rsi")
_LENGTHS = (5, 8, 10, 13, 14, 20, 21, 34, 50, 55, 89, 100, 144, 200)


def _widen(origin: dict, anchor: dict, seen: set[str], attempt: int, extras: bool = True) -> dict | None:
    """Keep producing a new version until the 200 attempts are used."""
    keys = [
        key for key, value in origin.items()
        if key not in FROZEN and key not in {"added_indicator", "added_length"} and _tunable_value(value)
    ]
    if not keys and not extras:
        return None
    for offset in range(attempt, attempt + 800):
        plan = dict(anchor)
        if keys:
            name = keys[offset % len(keys)]
            value = origin.get(name, anchor.get(name))
            plan[name] = _widened_value(name, value, offset)
            if extras and offset % 2 == 0:
                plan["added_indicator"] = _INDICATORS[(offset // 2) % len(_INDICATORS)]
                plan["added_length"] = _LENGTHS[(offset // 2) % len(_LENGTHS)]
        elif extras:
            plan["added_indicator"] = _INDICATORS[offset % len(_INDICATORS)]
            plan["added_length"] = max(2, _LENGTHS[offset % len(_LENGTHS)] + offset // len(_LENGTHS))
        if _key(plan) not in seen:
            return plan
    return None


def _widened_value(name: str, value, offset: int):
    if isinstance(value, bool):
        return (not value) if offset % 2 else value
    if isinstance(value, int) and not isinstance(value, bool):
        step = (offset % 61) - 20
        return _clamp(name, max(1, int(value) + step))
    if isinstance(value, float):
        scale = 0.2 + (offset % 40) * 0.1
        return round(max(0.01, float(value) * scale), 4)
    return value


def _with_indicator(cls, kind: str):
    """Gate new entries with an added average or RSI. Exits stay on the original strategy."""
    key = (cls, kind)
    cached = _FILTERS.get(key)
    if cached is not None:
        return cached

    class Filtered(cls):
        added_indicator = kind

        def init(self):
            super().init()
            from engine.indicators import ta

            length = max(2, int(getattr(self, "added_length", 20) or 20))
            if kind == "vwma":
                self._gate = ta.vwma(self.close, self.volume, length)
            elif kind == "ema":
                self._gate = ta.ema(self.close, length)
            elif kind == "sma":
                self._gate = ta.sma(self.close, length)
            elif kind == "wma":
                self._gate = ta.wma(self.close, length)
            else:
                self._gate = ta.rsi(self.close, length)

        def entry(self, id, direction, qty=None, limit=None, stop=None, comment=""):
            if not self._gate_allows(direction):
                return
            super().entry(id, direction, qty=qty, limit=limit, stop=stop, comment=comment)

        def _gate_allows(self, direction) -> bool:
            import math

            from engine.types import Direction

            value = self._val(self._gate)
            price = self._val(self.close)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return True
            if kind == "rsi":
                if direction == Direction.LONG:
                    return value <= 70
                if direction == Direction.SHORT:
                    return value >= 30
                return True
            if direction == Direction.LONG:
                return price >= value
            if direction == Direction.SHORT:
                return price <= value
            return True

    Filtered.__name__ = f"{cls.__name__}_{kind}"
    _FILTERS[key] = Filtered
    return Filtered
