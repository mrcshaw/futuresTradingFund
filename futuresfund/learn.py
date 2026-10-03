"""Study the Pine files on disk. Adjust parameters on the engine, keep the notes, then file the script as learned."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
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
    "pv",
    "pointvalue",
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
_notes_lock = threading.Lock()


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


def tested_instruments(notes: dict) -> set[str]:
    """Instruments this strategy has already been run on. An older note was an ES test."""
    recorded = notes.get("instruments") if isinstance(notes, dict) else None
    if isinstance(recorded, dict) and recorded:
        return {str(name).upper() for name, record in recorded.items() if _instrument_finished(record)}
    if isinstance(notes, dict) and (notes.get("trials") or isinstance(notes.get("best"), dict) and notes.get("best")):
        return {"ES"}
    return set()


def pending_for_chart(notes: dict, timeframe: str, instruments: list[str] | None = None) -> list[str]:
    """Instruments that still need this one chart. A finished chart is not run again."""
    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    return [root for root in roots if not _chart_finished(notes or {}, root, timeframe)]


def pending_instruments(notes: dict, instruments: list[str] | None = None) -> list[str]:
    """Instruments that still need a run. One that is already listed is not tested again."""
    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    done = tested_instruments(notes or {})
    return [root for root in roots if root not in done]


def ready_to_file(notes: dict, instruments: list[str] | None = None) -> bool:
    """A script moves to learned only after every chart instrument has a result."""
    roots = [str(name).upper() for name in (instruments if instruments is not None else research_instruments())]
    return bool(roots) and not pending_instruments(notes or {}, roots)


def research_instruments() -> list[str]:
    """ES, gold, and Nasdaq. Nasdaq stays in the workflow even before its chart files are added."""
    from futuresfund.charts import chart_roots

    present = chart_roots()
    ordered = [name for name in ("ES", "QO", "NQ") if name in present or name == "NQ"]
    ordered.extend(name for name in present if name not in ordered)
    return ordered


def _instrument_finished(record) -> bool:
    """An instrument is finished when the 2-minute, 5-minute, and 15-minute charts have each been run."""
    if not isinstance(record, dict):
        return False
    charts = record.get("charts")
    if isinstance(charts, dict):
        return all(_attempted(charts.get(name)) for name in RESEARCH_CHARTS)
    return _attempted(record)


def _chart_finished(notes: dict, root: str, timeframe: str) -> bool:
    recorded = notes.get("instruments") if isinstance(notes, dict) else None
    if isinstance(recorded, dict) and recorded:
        record = recorded.get(root)
        if not isinstance(record, dict):
            return False
        charts = record.get("charts")
        if isinstance(charts, dict):
            return _attempted(charts.get(timeframe))
        return _attempted(record)
    if root == "ES" and isinstance(notes, dict) and (notes.get("trials") or isinstance(notes.get("best"), dict) and notes.get("best")):
        return _attempted({
            "trials": notes.get("trials"),
            "best": notes.get("best"),
            "error": notes.get("error"),
            "blocked": notes.get("blocked"),
        })
    return False


def _attempted(record) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("blocked") or record.get("error"):
        return False
    best = record.get("best")
    if isinstance(best, dict) and (best.get("error") or best.get("fatal")):
        return False
    if record.get("trials") or record.get("attempts"):
        return True
    best = record.get("best")
    return isinstance(best, dict) and bool(best)


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
    from futuresfund.backtrader_engine import warm_engines
    from futuresfund.roster import CHART_DESKS

    LEARNED.mkdir(parents=True, exist_ok=True)
    NOTES.mkdir(parents=True, exist_ok=True)
    instruments = ", ".join(research_instruments())
    warm_engines()
    for desk in CHART_DESKS:
        board.set_activity(desk["developer"], f"Engine {desk['engine']} is warm for the {desk['label']} chart")
        board.post(
            desk["developer"],
            f"Engine {desk['engine']} stays running. This desk runs only the {desk['label']} chart. The bar file is written once and reused.",
            kind="log",
            channel=desk["developer"],
        )
    frames = _load_frames()
    from futuresfund.charts import chart_columns, charts_have_volume

    volume_ready = charts_have_volume()
    if not volume_ready:
        listed = "; ".join(f"{name} is {', '.join(cols)}" for name, cols in chart_columns().items())
        for desk in CHART_DESKS:
            board.post(
                desk["developer"],
                "The chart files have no volume column. "
                f"{listed}. The EMA numbers sit next to the price, so they are a moving average, not contract volume. "
                "A script that reads volume cannot enter on these bars, so those scripts stay in the learning folder. "
                "The price alert can send volume, the profile, and delta. They stay here until that data is on the bars.",
                kind="report",
                channel=desk["developer"],
            )
    from futuresfund.strategy import absorb_research_notes

    absorb_research_notes()
    loaded = ", ".join(f"{name} {len(rows)} bars" for name, rows in frames.items())
    intro = (
        "The learning folder is only the starting point. Each chart uses 200 different results, even after a version meets the profit target, so a better one can still be found. "
        "Backtrader runs each chart in its own process. Engine 1 is the 2-minute chart, engine 2 is the 5-minute chart, and engine 3 is the 15-minute chart. Engine 4 tests a new script until it is profitable. "
        "When a script's entry is not already on the engine, the chart developer adds that rule before the attempts. "
        f"Each script is tested once on every instrument ({instruments}) and on each of the three charts. It moves to the learned folder only after those charts are done. "
        f"Loaded {loaded}. "
        "Every attempt changes one input and records how that change moved the result. The research guide is guidance, not a requirement. Consistency comes before a larger profit. "
        "A change that is less consistent or less profitable is reversed, and a change that improves that is continued. "
        "A run that repeats an earlier profit, drawdown, and trade count is not counted, and the search keeps going until 200 different results. "
        + _risk_line()
    )
    board.post(CHART_DESKS[0]["researcher"], intro, kind="report", channel=CHART_DESKS[0]["researcher"])
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
    held_back: set[tuple[str, str]] = set()
    claimed: set[tuple[str, str]] = set()
    claim_lock = threading.Lock()

    def take_job(timeframe: str) -> Path | None:
        with claim_lock:
            for path in queue(skip_volume=not volume_ready):
                key = (str(path), timeframe)
                if key in held_back or key in claimed:
                    continue
                if not pending_for_chart(_read_notes(path), timeframe):
                    continue
                claimed.add(key)
                return path
            return None

    def release_job(path: Path, timeframe: str) -> None:
        with claim_lock:
            claimed.discard((str(path), timeframe))

    def developer_loop(desk: dict) -> None:
        developer = desk["developer"]
        researcher = desk["researcher"]
        timeframe = desk["timeframe"]
        while not board.cancel.is_set():
            if not _wait(board, learning_pause):
                return
            path = take_job(timeframe)
            if path is None:
                board.post(developer, f"Waiting for the next Pine script on the {desk['label']} chart.", kind="log", channel=developer)
                if not _sleep(board, learning_pause, 20):
                    return
                continue
            board.post(developer, f"Taking {path.name} on the {desk['label']} chart.", kind="log", channel=developer)
            board.post(
                researcher,
                f"{developer} is studying {path.name} on the {desk['label']} chart.",
                kind="log",
                channel=researcher,
            )
            try:
                studied = _study(board, path, frames, learning_pause, developer, timeframe, researcher)
                error = None
            except Exception as exc:
                studied, error = "error", exc
            finally:
                release_job(path, timeframe)
            if studied == "error":
                if board.cancel.is_set():
                    return
                traceback.print_exc()
                board.post(
                    developer,
                    f"{path.name} stopped on an engine error: {error}. It was not marked finished.",
                    kind="error",
                    channel=developer,
                )
                board.post(
                    researcher,
                    f"{path.name} stopped on an engine error on the {desk['label']} chart: {error}. It stays in the learning folder.",
                    kind="report",
                    channel=researcher,
                )
                continue
            if studied == "failed":
                with claim_lock:
                    held_back.add((str(path), timeframe))
                board.post(
                    developer,
                    f"{path.name} did not finish a backtest on the {desk['label']} chart. Taking the next script.",
                    kind="log",
                    channel=developer,
                )
            elif studied == "blocked":
                with claim_lock:
                    held_back.add((str(path), timeframe))
                board.post(
                    researcher,
                    f"{path.name} needs volume on the {desk['label']} chart bars. It stays in the learning folder.",
                    kind="report",
                    channel=researcher,
                )
                board.post(
                    developer,
                    f"{path.name} needs volume on the chart bars. Taking the next script.",
                    kind="log",
                    channel=developer,
                )
            elif studied == "waiting" and not _sleep(board, learning_pause, 60):
                return

    from futuresfund.strategy_team import run_creation

    workers = [
        threading.Thread(target=developer_loop, args=(desk,), daemon=True, name=desk["developer"])
        for desk in CHART_DESKS
    ]
    workers.append(threading.Thread(target=run_creation, args=(board, learning_pause), daemon=True, name="strategy-creation"))
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def study_parameters(base: dict, execute, account_size: float = ACCOUNT_SIZE, limit: int = ATTEMPT_LIMIT) -> list[dict]:
    """Build off the last result until the profit target is met without crossing the trail."""
    return _search(base, execute, account_size, limit, None, None, None)


def next_plan(origin: dict, trials: list[dict], limit: int = ATTEMPT_LIMIT, *, extras: bool = True, extra_seen: set | None = None) -> dict | None:
    """Change one input. Continue it when the result got steadier, and reverse it when it did not."""
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
    current = dict(last.get("params") or base)
    focus = None
    direction = 1
    anchor = current
    old = None
    new = None
    if previous is not None:
        changed = _changed_keys(previous.get("params") or {}, current)
        if len(changed) == 1:
            focus = changed[0]
            old = (previous.get("params") or {}).get(focus)
            new = current.get(focus)
            if _rank(last) < _rank(previous):
                direction = -1
                anchor = dict(previous.get("params") or base)
    plan = _step_one(anchor, focus, old, new, direction, seen)
    if plan:
        return plan
    for name in _input_order(current):
        if name == focus:
            continue
        plan = _step_one(current, name, None, None, 1, seen)
        if plan:
            return plan
    widened = _widen(base, current, seen, len(trials), extras=extras)
    if widened is not None:
        return widened
    return _force_new(current, seen)


def _search(base, execute, account_size, limit, board, path, pause, facts=None, extras: bool = True, developer: str = "2min chart developer", researcher: str = "Chart researcher", until_profitable: bool = False):
    origin = {key: value for key, value in base.items() if key not in FROZEN and _tunable_value(value)}
    trials = []
    seen = set()
    seen_results = set()
    blocked = set()
    repeats = 0
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
                researcher,
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
            blocked.add(mark)
            continue
        seen.add(mark)
        if board is not None and path is not None:
            change = _change_text(trials[-1]["params"] if trials else None, plan)
            board.post(
                researcher,
                f"Attempt {attempt} on {label}. {change}.",
                kind="log",
                channel=researcher,
            )
            board.post(
                developer,
                f"Attempt {attempt} on {label}. {change}.",
                kind="log",
                channel=developer,
            )
        measured = execute(plan) or {}
        signature = _result_key(measured)
        if signature in seen_results and not measured.get("error") and not measured.get("fatal"):
            repeats += 1
            blocked.add(mark)
            if board is not None and path is not None:
                board.post(
                    researcher,
                    f"{label} repeated an earlier result, so this run is not counted. The same variable will be moved further.",
                    kind="log",
                    channel=researcher,
                )
            continue
        repeats = 0
        seen_results.add(signature)
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
            "instrument": measured.get("instrument") or "",
            "contract": measured.get("contract") or "",
        }
        row["passed"] = _passed(row, account_size)
        row["change"] = _change_text(trials[-1]["params"] if trials else None, plan)
        row["impact"] = _impact(trials[-1] if trials else None, row)
        if row.get("take_profit") is None:
            row["take_profit"] = measured.get("take_profit", plan.get("tp_dollars"))
        if measured.get("note"):
            row["note"] = measured["note"]
        trials.append(row)
        if row.get("stopped"):
            break
        if until_profitable and _row_profitable(row):
            if board is not None and path is not None:
                board.post(
                    developer,
                    f"{path.name} is profitable on this engine. Profit {row.get('net_profit')}, trades {row.get('trades')}. The search stops.",
                    kind="report",
                    channel=developer,
                )
            if board is not None and path is not None:
                _document(board, facts or {}, path, row, len(trials), developer, researcher)
            break
        if board is not None and path is not None:
            _document(board, facts or {}, path, row, len(trials), developer, researcher)
        if row.get("blocked") or row.get("fatal"):
            break
    return trials


def _row_profitable(row: dict) -> bool:
    """A result with a profit above zero and at least one trade."""
    if row.get("error") or row.get("fatal") or row.get("blocked"):
        return False
    try:
        profit = float(row.get("net_profit"))
        trades = int(row.get("trades") or 0)
    except (TypeError, ValueError):
        return False
    return profit > 0 and trades >= 1


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


def research_guidelines() -> str:
    """Guidance for the next change. These are not requirements."""
    return (
        "Research guide. These are guidelines, not requirements. "
        "Use them to choose one educated change. Consistency comes before a larger profit. "
        "Each run changes one variable. Measure how that one change moved profit, drawdown, and trade count "
        "on the 2-minute, 5-minute, and 15-minute charts.\n"
        "What we want\n"
        "- A smaller worst loss, even when the profit is smaller.\n"
        "- A result that still holds on later bars that were not used to pick the settings.\n"
        "- Entry, exit, and the moment the book goes flat, stated before the test.\n"
        "- More than one condition agreeing before an entry. A single trigger is a weaker rule.\n"
        "- A forward check. A high backtest win rate can shrink once the same rule runs on new bars.\n"
        "- Movement in the settings that decide whether a trade is allowed: trend length, confirmation, session, cooldown, and how far price must travel.\n"
        "- One purposeful change, large enough that the trade list can change. Continue a direction that made the book steadier. Reverse a direction that made it jumpier.\n"
        "What we do not want\n"
        "- A change that looks good only on the bars used to choose it.\n"
        "- A search of nearby values until one pair looks best.\n"
        "- A higher return from staying in the whole move while the drawdown is worse than holding.\n"
        "What to change\n"
        "- Move the settings that decide when a trade is allowed: trend length, confirmation, session, cooldown, and how far price must travel before entry or exit.\n"
        "- Make one purposeful change, large enough that the trade list can actually change. A nudge that leaves profit, drawdown, and trade count the same is not an attempt.\n"
        "- If the last change made the book steadier, continue that same variable in that direction. If it made the book jumpier or the profit less repeatable, undo that direction.\n"
    )


def notes_digest(limit: int = 20) -> str:
    path = NOTES / "LESSONS.md"
    if not path.is_file():
        return "No learned strategy notes yet."
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _study(board, path: Path, frames: dict, pause, developer: str = "2min chart developer", timeframe: str = "2m", researcher: str = "Chart researcher") -> None:
    if path.suffix.lower() != ".pine":
        return
    from futuresfund.roster import chart_desk

    notes = _read_notes(path)
    pending = pending_for_chart(notes, timeframe)
    if not pending:
        if ready_to_file(notes):
            _file_ready(board, path, notes)
        return
    root = pending[0]
    label = chart_desk(timeframe)["label"]
    board.post(
        researcher,
        f"Testing {path.name} on the {label} {root} chart. It stays in the learning folder until the other charts and instruments are finished.",
        kind="report",
        channel=researcher,
    )
    text = path.read_text(encoding="utf-8", errors="replace")
    facts = pine_facts(text) if text else {"title": path.stem, "inputs": [], "named": {}}
    frames = _load_frames(root)
    bars = frames.get(timeframe) or []
    if len(bars) < 80:
        board.post(
            developer,
            f"{root} is in the research workflow. chartData/{root.lower()} has no {label} file yet. "
            f"{path.name} stays in the learning folder until that chart is there.",
            kind="report",
            channel=developer,
        )
        return "waiting"
    from futuresfund.backtrader_engine import _bars_from
    from futuresfund.entry_rules import ensure_entry_rule

    if not ensure_entry_rule(board, developer, text, _bars_from(bars)):
        return "failed"
    execute = _engine_execute(text, frames, facts, board, root, developer, timeframe)
    trials = _search(
        _with_contract_point(_pine_params(facts), root),
        execute,
        ACCOUNT_SIZE,
        ATTEMPT_LIMIT,
        board,
        path,
        pause,
        facts,
        extras=False,
        developer=developer,
        researcher=researcher,
    )
    if board.cancel.is_set() or (trials and trials[-1].get("stopped")):
        return
    if trials and (trials[-1].get("fatal") or trials[-1].get("error")):
        board.post(
            researcher,
            f"{path.name} was not marked tested on the {label} {root} chart. The engine did not finish a backtest, so that chart will be tried again.",
            kind="report",
            channel=researcher,
        )
        return "failed"
    if trials and trials[-1].get("blocked"):
        board.post(
            developer,
            f"{path.name} was not filed. {trials[-1].get('note')}",
            kind="report",
            channel=developer,
        )
        return "blocked"
    quant, indicator = adjustment_notes(trials)
    best = max(trials, key=_rank) if trials else {}
    if isinstance(best, dict):
        best = dict(best)
        best["contract"] = f"{root}1!"
        best["timeframe"] = timeframe
    _save_notes(path, facts, trials, quant, indicator, best, root, timeframe)
    _library_notice(board, facts, path, best)
    from futuresfund.contracts import POINT_VALUE

    facts = dict(facts)
    facts["point_value"] = float(POINT_VALUE.get(root, facts.get("point_value") or 50))
    report = ceo_report(facts, {"measured": best, "passed": best.get("passed"), "params": best.get("params")}, quant, indicator, len(trials))
    report = _library_line(facts, path, best, root) + "\n\n" + report
    _send(board, f"{facts.get('title') or path.stem} on {root} {label}", report)
    notes = _read_notes(path)
    if ready_to_file(notes):
        _file_ready(board, path, notes)
    else:
        board.post(
            researcher,
            f"{path.name} finished the {label} {root} chart. It stays in the learning folder until the other charts are tested too.",
            kind="report",
            channel="headquarters",
        )
    board.set_status(researcher, "done")
    board.set_status(developer, "done")


def _file_ready(board, path: Path, notes: dict) -> None:
    with _notes_lock:
        if not path.is_file():
            return
        names = ", ".join(sorted(tested_instruments(notes)))
        _file_away(path, None)
    board.post(
        "Chart researcher",
        f"Filed {path.name} after it was tested on {names}. It is not tested on those instruments again.",
        kind="report",
        channel="headquarters",
    )


def _pine_params(facts: dict) -> dict:
    params = {}
    for item in facts.get("inputs") or []:
        if _tunable_value(item.get("default")):
            params[item["name"]] = item["default"]
    return params


def _with_contract_point(params: dict, instrument: str) -> dict:
    """Dollar inputs use this contract's point value. NQ is $20, not the $50 written for ES."""
    from futuresfund.contracts import POINT_VALUE, root_of

    try:
        root = root_of(instrument if "!" in str(instrument) else f"{instrument}1!")
    except ValueError:
        root = str(instrument or "ES").upper()
    point = float(POINT_VALUE.get(root, 50))
    updated = params if isinstance(params, dict) else {}
    for key in list(updated):
        if key in {"pv", "point_value", "pointvalue"}:
            updated[key] = point
    return updated


def _input_overrides(facts: dict, params: dict) -> dict:
    by_name = {item["name"]: item for item in facts.get("inputs") or []}
    overrides = {}
    for key, value in params.items():
        item = by_name.get(key)
        if item:
            overrides[item.get("label") or key] = value
    return overrides


def _engine_execute(text: str, frames: dict, facts: dict, board, instrument: str = "ES", developer: str = "2min chart developer", timeframe: str = "2m", slot: int | None = None):
    from futuresfund.backtrader_engine import run_script
    from futuresfund.roster import chart_desk

    label = chart_desk(timeframe)["label"]

    def execute(params: dict) -> dict:
        params = _with_contract_point(params, instrument)
        overrides = _input_overrides(facts, params)
        cancel = None if board is None else board.cancel
        title = facts.get("title") or "the script"
        shelf = "GC1!" if instrument in {"QO", "GC", "MGC"} else f"{instrument}1!"
        if cancel is not None and cancel.is_set():
            return {"stopped": True, "runner": "backtrader", "engine": "backtrader", "timeframe": timeframe, "net_profit": None, "max_drawdown": None, "trades": 0}
        bars = frames.get(timeframe) or []
        if len(bars) < 80:
            return {
                "timeframe": timeframe,
                "error": f"The {label} chart is not loaded.",
                "fatal": True,
                "net_profit": None,
                "max_drawdown": None,
                "trades": 0,
                "passed": False,
                "runner": "backtrader",
                "engine": "backtrader",
                "instrument": instrument,
                "contract": shelf,
            }

        def measure():
            return run_script(text, bars, timeframe, overrides, cancel, instrument, title, slot) or {}

        if board is None:
            measured = measure()
        else:
            from futuresfund.crew import get_crew

            measured = get_crew(board).run(
                developer,
                f"Running {title} on the {label} {instrument} chart",
                measure,
            )
            if not measured:
                if cancel is not None and cancel.is_set():
                    return {
                        "stopped": True,
                        "runner": "backtrader",
                        "engine": "backtrader",
                        "timeframe": timeframe,
                        "net_profit": None,
                        "max_drawdown": None,
                        "trades": 0,
                    }
                measured = {}
        measured["timeframe"] = timeframe
        measured["instrument"] = instrument
        measured["contract"] = shelf
        measured["passed"] = _passed(measured, ACCOUNT_SIZE)
        measured["take_profit"] = params.get("tp_dollars") or facts.get("target")
        measured["frames"] = [{
            "timeframe": timeframe,
            "net_profit": measured.get("net_profit"),
            "max_drawdown": measured.get("max_drawdown"),
            "trades": measured.get("trades"),
            "passed": bool(measured.get("passed")),
        }]
        if board is not None:
            detail = measured.get("error") or (
                f"profit {measured.get('net_profit')}, drawdown {measured.get('max_drawdown')}, trades {measured.get('trades')}"
            )
            board.post(
                developer,
                f"{title} finished the {label} {instrument} chart. {detail}.",
                kind="log",
                channel=developer,
            )
        return measured

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


def _load_frames(root: str = "ES") -> dict[str, list]:
    """Chart export plus any later live candles. Research must not stop where the file stopped."""
    from futuresfund.charts import load_chart
    from futuresfund.chart_feed import live_bars

    incoming = live_bars(root)
    return {name: _with_latest(load_chart(name, root), incoming, name) for name in RESEARCH_CHARTS}


def _with_latest(bars: list, incoming: list, timeframe: str) -> list:
    if not incoming:
        return bars
    from futuresfund.chart_feed import _epoch_ms, fold_bars

    have = {_epoch_ms(bar.get("t")) for bar in bars}
    have.discard(None)
    minutes = [row for row in incoming if row.get("timeframe") == "1m"]
    folded = fold_bars(minutes, timeframe) if minutes else []
    fresh = []
    for bar in folded:
        if bar.get("forming"):
            continue
        stamp = _epoch_ms(bar.get("t")) or 0
        if not stamp or stamp in have:
            continue
        have.add(stamp)
        fresh.append({
            "t": bar.get("t"),
            "o": bar.get("o"),
            "h": bar.get("h"),
            "l": bar.get("l"),
            "c": bar.get("c"),
            "v": bar.get("v") or 0,
        })
    merged = list(bars) + fresh
    merged.sort(key=lambda bar: _epoch_ms(bar.get("t")) or 0)
    return merged


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
    return ", ".join(f"{key} {before.get(key)} to {after.get(key)}" for key in keys)


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


def _document(board, facts: dict, path: Path, row: dict, number: int, developer: str = "2min chart developer", researcher: str = "Chart researcher") -> None:
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
            researcher,
            f"Recording attempt {number} of {ATTEMPT_LIMIT} on {path.name}",
            lambda: None,
        )
        board.post(
            researcher,
            f"Attempt {number} of {ATTEMPT_LIMIT} on {path.name}. {row.get('impact') or row.get('change')}. "
            f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.",
            kind="report",
            channel=researcher,
        )
        board.post(
            "Risk Manager",
            f"Attempt {number} of {ATTEMPT_LIMIT} on {path.name}. {_attempt_note(row)} "
            f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.",
            kind="report",
            channel="Risk Manager",
        )
        board.post(
            "Trading Analyst",
            f"Attempt {number} of {ATTEMPT_LIMIT} on {path.name}. {row.get('impact') or row.get('change')}.",
            kind="log",
            channel="Trading Analyst",
        )
    from futuresfund.strategy import remember_research

    remember_research(row, path.name, facts.get("title") or path.stem)
    board.post(
        developer,
        f"Attempt {number} of {ATTEMPT_LIMIT} on the {label} chart: {path.name}. {change}. "
        f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.{target} "
        f"{_attempt_note(row)}{note}{charts}",
        kind="report",
        channel=developer,
    )
    _offer_library(board, facts, path, row)


def _offer_library(board, facts: dict, path: Path, row: dict) -> None:
    """The trading analyst files a profitable instrument result. A repeat of a worse result is skipped."""
    if str(row.get("engine") or row.get("runner") or "") != "backtrader":
        return
    try:
        profit = float(row.get("net_profit"))
    except (TypeError, ValueError):
        return
    if profit <= 0 or int(row.get("trades") or 0) < 1:
        return
    instrument = str(row.get("instrument") or "ES").upper()
    shelf = "GC1!" if instrument in {"QO", "GC", "MGC"} else f"{instrument}1!"
    from futuresfund.library import contract_of, record_profitable

    title = facts.get("title") or path.stem
    symbol, _label = contract_of(str(title), path.name, shelf)
    change = str(row.get("change") or "").strip()
    shown = str(title)
    if change and change not in {"Original", "Starting settings"} and not change.startswith("Starting measurement"):
        shown = f"{title} — {change}"
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    fingerprint = json.dumps([symbol, row.get("timeframe"), params, profit, int(row.get("trades") or 0)], sort_keys=True, default=str)
    entry = {
        "id": f"{path.stem}-{__import__('hashlib').sha1(fingerprint.encode()).hexdigest()[:12]}",
        "title": shown,
        "script": str(title),
        "file": path.name,
        "contract": symbol,
        "contract_label": _label,
        "timeframe": row.get("timeframe") or "",
        "net_profit": profit,
        "max_drawdown": row.get("max_drawdown"),
        "trades": int(row.get("trades") or 0),
        "win_rate": row.get("win_rate"),
        "params": params,
        "engine": "backtrader",
        "runner": "backtrader",
    }
    if record_profitable(entry) and board is not None:
        _library_notice(board, facts, path, entry)


def _write_instrument(path: Path, facts: dict, instruments: dict, best: dict) -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    existing = _read_notes(path)
    payload = dict(existing)
    payload["file"] = path.name
    payload["title"] = facts.get("title") or existing.get("title") or path.stem
    payload["instruments"] = instruments
    payload["best"] = best
    slug = path.stem.replace(" ", "_")
    (NOTES / f"{slug}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _library_notice(board, facts: dict, path: Path, best: dict) -> None:
    """A finished profitable study goes on the strategy library. The analyst records the account."""
    title = facts.get("title") or path.stem
    try:
        profit = float(best.get("net_profit"))
    except (TypeError, ValueError):
        profit = None
    trades = int(best.get("trades") or 0)
    if profit is None or profit <= 0 or trades < 1 or str(best.get("engine") or best.get("runner") or "") != "backtrader":
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
        from futuresfund.roster import chart_desk

        researcher = chart_desk(str((best or {}).get("timeframe") or "2m"))["researcher"]
        board.post(researcher, note, kind="report", channel=researcher)
        board.post("Trading Analyst", note, kind="report", channel="Trading Analyst")


def _read_notes(path: Path) -> dict:
    slug = path.stem.replace(" ", "_")
    file = NOTES / f"{slug}.json"
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_failure(path: Path, message: str) -> None:
    notes = _read_notes(path)
    pending = pending_instruments(notes)
    root = pending[0] if pending else "ES"
    _save_notes(
        path,
        {"title": path.stem},
        [],
        f"The engine stopped: {message}",
        "No indicator pass was completed.",
        {"error": message, "net_profit": None, "max_drawdown": None, "trades": 0, "contract": f"{root}1!"},
        root,
    )


def _save_notes(path: Path, facts: dict, trials: list, quant: str, indicator: str, best: dict, instrument: str = "ES", timeframe: str = "") -> None:
    NOTES.mkdir(parents=True, exist_ok=True)
    slug = path.stem.replace(" ", "_")
    with _notes_lock:
        existing = _read_notes(path)
        instruments = existing.get("instruments") if isinstance(existing.get("instruments"), dict) else {}
        if not instruments and (existing.get("trials") or isinstance(existing.get("best"), dict) and existing.get("best")):
            instruments["ES"] = {
                "attempts": existing.get("attempts") or len(existing.get("trials") or []),
                "best": existing.get("best") or {},
                "quantitative_researcher": existing.get("quantitative_researcher") or "",
                "indicator_researcher": existing.get("indicator_researcher") or "",
                "trials": existing.get("trials") or [],
            }
        root = str(instrument or "ES").upper()
        chart = {
            "attempts": len(trials),
            "best": best,
            "quantitative_researcher": quant,
            "indicator_researcher": indicator,
            "trials": trials,
            "error": (best or {}).get("error") or "",
            "blocked": bool((best or {}).get("blocked")),
        }
        prior = instruments.get(root) if isinstance(instruments.get(root), dict) else {}
        charts = dict(prior.get("charts") or {}) if isinstance(prior.get("charts"), dict) else {}
        if timeframe:
            charts[timeframe] = chart
            chosen_chart = best if isinstance(best, dict) else {}
            for item in charts.values():
                candidate = item.get("best") if isinstance(item, dict) else None
                if isinstance(candidate, dict) and candidate and _rank(candidate) > _rank(chosen_chart or {}):
                    chosen_chart = candidate
            instruments[root] = {
                "attempts": sum(int(item.get("attempts") or 0) for item in charts.values() if isinstance(item, dict)),
                "best": chosen_chart,
                "charts": charts,
                "profitable": list(prior.get("profitable") or []),
                "quantitative_researcher": quant,
                "indicator_researcher": indicator,
                "trials": trials,
            }
        else:
            instruments[root] = chart
        chosen = best if isinstance(best, dict) else {}
        for record in instruments.values():
            candidate = record.get("best") if isinstance(record, dict) else None
            if isinstance(candidate, dict) and candidate and _rank(candidate) > _rank(chosen or {}):
                chosen = candidate
        payload = {
            "file": path.name,
            "title": facts.get("title") or existing.get("title") or path.stem,
            "attempts": sum(int(record.get("attempts") or 0) for record in instruments.values() if isinstance(record, dict)),
            "best": chosen,
            "quantitative_researcher": quant,
            "indicator_researcher": indicator,
            "trials": trials,
            "instruments": instruments,
        }
        (NOTES / f"{slug}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    lesson = (
        f"{payload['title']}: attempts {len(trials)}, "
        f"best profit {best.get('net_profit')}, drawdown {best.get('max_drawdown')}, "
        f"trail {'cleared' if best.get('passed') else 'not cleared'}. {quant}"
    )
    with (NOTES / "LESSONS.md").open("a", encoding="utf-8") as handle:
        handle.write(lesson + "\n")


def _library_line(facts: dict, path: Path, best: dict, root: str) -> str:
    """Tell the portfolio manager which library shelf received this result."""
    from futuresfund.library import contract_of

    symbol, label = contract_of(facts.get("title") or path.stem, path.name, best.get("contract") or f"{root}1!")
    try:
        profit = float(best.get("net_profit"))
        trades = int(best.get("trades") or 0)
    except (TypeError, ValueError):
        profit, trades = None, 0
    engine = str(best.get("engine") or best.get("runner") or "")
    if engine == "backtrader" and profit is not None and profit > 0 and trades >= 1:
        return (
            f"Filed in the {label} strategy library ({symbol}). "
            f"{best.get('timeframe')} profit {profit}, drawdown {best.get('max_drawdown')}, trades {trades}."
        )
    return (
        f"Not added to the {label} strategy library ({symbol}). "
        f"Profit {best.get('net_profit')}, trades {best.get('trades')}."
    )


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


def _consistency(row: dict) -> tuple[int, float]:
    """How many charts are profitable, and the profit left after drawdown on those charts."""
    frames = [frame for frame in row.get("frames") or [] if isinstance(frame, dict)]
    if not frames:
        profit = float(row.get("net_profit") or 0)
        drawdown = float(row.get("max_drawdown") or 0)
        green = 1 if profit > 0 and profit > drawdown else 0
        return green, profit - drawdown
    green = 0
    score = 0.0
    for frame in frames:
        profit = float(frame.get("net_profit") or 0)
        drawdown = float(frame.get("max_drawdown") or 0)
        if profit > 0 and profit > drawdown:
            green += 1
        score += profit - drawdown
    return green, score


def _rank(row: dict):
    """Prefer a result that is profitable on more charts, then the one with more profit left after drawdown."""
    green, score = _consistency(row)
    profit = float(row.get("net_profit") or -10**12)
    drawdown = float(row.get("max_drawdown") or 10**12)
    if row.get("passed"):
        return (3, green, score, profit, -drawdown)
    if profit > 0:
        return (2, green, score, profit - drawdown, -drawdown)
    return (1, green, score, profit, -drawdown)


def _result_key(measured: dict) -> tuple:
    frames = tuple(
        (
            frame.get("timeframe"),
            frame.get("net_profit"),
            frame.get("max_drawdown"),
            frame.get("trades"),
        )
        for frame in measured.get("frames") or []
        if isinstance(frame, dict)
    )
    return (
        measured.get("net_profit"),
        measured.get("max_drawdown"),
        measured.get("trades"),
        frames,
    )


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


def _input_order(params: dict) -> list[str]:
    """Settings that decide whether a trade is allowed come first. One of them is changed at a time."""
    words = ("session", "cooldown", "length", "len", "stop", "tp", "zone", "shift", "lookback", "confirm", "slope", "ema")

    def rank(name: str) -> tuple:
        lowered = name.lower()
        return (0 if any(word in lowered for word in words) else 1, lowered)

    return sorted(
        (
            key for key, value in params.items()
            if key not in FROZEN and key not in {"added_indicator", "added_length"} and _tunable_value(value)
        ),
        key=rank,
    )


def _step_one(anchor: dict, name: str | None, old, new, direction: int, seen: set[str]) -> dict | None:
    """Return a plan that differs in exactly one input."""
    if not name or name not in anchor:
        names = _input_order(anchor)
        name = names[0] if names else None
    if not name:
        return None
    current = anchor.get(name)
    candidates = []
    if direction > 0 and old is not None and new is not None:
        nxt = _continue_step(name, old, new)
        if nxt is not None:
            candidates.append(nxt)
    if direction < 0 and old is not None and new is not None:
        nxt = _reverse_step(name, old, new)
        if nxt is not None:
            candidates.append(nxt)
    candidates.extend(_probe_steps(name, current))
    for nxt in candidates:
        plan = dict(anchor)
        plan[name] = nxt
        if plan[name] == current:
            continue
        if _key(plan) not in seen and len(_changed_keys(anchor, plan)) == 1:
            return plan
    return None


def _impact(previous: dict | None, row: dict) -> str:
    """What the one changed variable did to the result."""
    if previous is None:
        return "Starting measurement. The next run changes one variable from here."
    keys = _changed_keys(previous.get("params") or {}, row.get("params") or {})
    name = keys[0] if len(keys) == 1 else "That change"
    profit_move = _profit(row) - _profit(previous)
    drawdown_move = float(row.get("max_drawdown") or 0) - float(previous.get("max_drawdown") or 0)
    if _rank(row) > _rank(previous):
        return (
            f"{name} made the book steadier. Profit moved {profit_move:.0f}. Drawdown moved {drawdown_move:.0f}. "
            "The next run continues this variable."
        )
    if _rank(row) < _rank(previous):
        return (
            f"{name} made the book jumpier. Profit moved {profit_move:.0f}. Drawdown moved {drawdown_move:.0f}. "
            "The next run undoes this direction."
        )
    return f"{name} left the result unchanged."


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


def _force_new(anchor: dict, seen: set[str]) -> dict | None:
    """One more unseen value for one input. The search stops only when nothing new is left."""
    for name in _input_order(anchor):
        value = anchor.get(name)
        if isinstance(value, bool):
            options = [not value]
        elif isinstance(value, int) and not isinstance(value, bool):
            options = [_clamp(name, max(1, int(value) + step)) for step in range(1, 121)]
            options += [_clamp(name, max(1, int(value) - step)) for step in range(1, 61)]
        elif isinstance(value, float):
            options = [round(max(0.01, float(value) + step * 0.5), 4) for step in range(1, 121)]
            options += [round(max(0.01, float(value) * scale), 4) for scale in (0.5, 0.75, 1.5, 2, 3)]
        else:
            continue
        for nxt in options:
            if nxt == value:
                continue
            plan = dict(anchor)
            plan[name] = nxt
            if _key(plan) not in seen and len(_changed_keys(anchor, plan)) == 1:
                return plan
    return None


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
