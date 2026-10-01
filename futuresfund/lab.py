"""Indicator and paper research. The engine adjusts one rule until it is proven or 100 changes do not improve it."""

from __future__ import annotations

import json
import threading
from datetime import datetime

from futuresfund.board import Board
from futuresfund.config import LAB_PATH
from futuresfund.prop_rules import describe
from futuresfund.research import compare_timeframes, evaluate_rule, load_bars
from futuresfund.strategy import get_strategy, store_strategy

ATTEMPT_LIMIT = 100
MEETING_SLATE = 3
_lock = threading.Lock()
_running = False

SOURCES = (
    {
        "desk": "Chart researcher",
        "title": "Public chart indicators",
        "url": "https://www.tradingview.com/scripts/indicators/",
        "note": (
            "The public indicator library is moving-average crosses, channels, RSI, Bollinger bands, "
            "MACD, Supertrend, volume-weighted price, and point-of-contact pullbacks. "
            "Private scripts are not copied. The developer tests those forms on the bar file and changes the settings."
        ),
    },
    {
        "desk": "Chart researcher",
        "title": "Goldman Sachs quantitative strategy notes",
        "url": "https://github.com/s0ap/gs-quantitative-strategies-research-notes",
        "note": (
            "Those notes are factor and trend research from the 1990s. "
            "The part this engine can test is trend-following and mean reversion on one futures contract."
        ),
    },
    {
        "desk": "Chart researcher",
        "title": "Quantformer",
        "url": "https://arxiv.org/abs/2404.00424",
        "note": (
            "Quantformer is a transformer price model. "
            "This desk does not train that model. The testable claim is that a price factor can be adjusted until the backtest is profitable or it is scrapped."
        ),
    },
)


def running() -> bool:
    with _lock:
        return _running


def load_report() -> dict:
    if not LAB_PATH.is_file():
        return _empty()
    try:
        data = json.loads(LAB_PATH.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("messages", [])
    data.setdefault("trials", [])
    data.setdefault("accepted", [])
    data.setdefault("scrapped", [])
    data.setdefault("status", "idle")
    return data


def save_report(report: dict) -> None:
    LAB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAB_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")


def search(bars: list[dict], *, instrument: str, account_size: float, profit_target: float,
           timeframe: str = "5m", account: str = "", board: Board | None = None,
           frames: dict[str, list[dict]] | None = None) -> dict:
    """Keep testing until three rules clear the Apex trail. Scrap a family after 100 changes with no better profit, then post a new batch."""
    report = load_report()
    report["status"] = "running"
    report["started"] = _now()
    report["trials"] = []
    report["scrapped"] = []
    report["attempts"] = 0
    report.setdefault("playbook", [])
    rules = describe(account_size)
    _note(report, board, "Risk Manager", rules)
    _note(report, board, "Chart researcher", SOURCES[0]["note"] + " " + SOURCES[0]["url"])
    _note(report, board, "Chart researcher", SOURCES[1]["note"] + " " + SOURCES[1]["url"])
    _note(report, board, "Chart researcher", SOURCES[2]["note"] + " " + SOURCES[2]["url"])
    if frames and len(frames) > 1:
        counts = ", ".join(f"{key} {len(frames[key])} bars" for key in ("2m", "5m", "15m") if key in frames)
        _note(
            report, board, "2min chart developer",
            f"Each setting is backtested on {counts}. The timeframe that clears the Apex trail with the best profit is the one kept. The others are the comparison.",
        )
    accepted: list[dict] = []
    kept: set[str] = set()
    batch = 0
    while True:
        if board is not None and board.cancel.is_set():
            break
        for name, grid in _families(batch):
            if name in kept:
                continue
            if board is not None and board.cancel.is_set():
                break
            _assign(report, board, name, batch)
            best_net = None
            best_row = None
            stale = 0
            proven = None
            for params in grid[:ATTEMPT_LIMIT]:
                row = _score(frames, bars, instrument, account_size, profit_target, name, params)
                report["attempts"] += 1
                if row is None:
                    stale += 1
                    continue
                net = float(row["backtest"]["net_profit"])
                report["trials"].append({
                    "id": row["id"],
                    "title": row["title"],
                    "name": name,
                    "params": params,
                    "net_profit": net,
                    "max_drawdown": row["backtest"]["max_drawdown"],
                    "trades": row["backtest"]["trades"],
                    "proven": row["proven"],
                })
                report["trials"] = report["trials"][-80:]
                improved = best_net is None or net > best_net
                if improved:
                    best_net = net
                    best_row = row
                    stale = 0
                else:
                    stale += 1
                if row["proven"] and (proven is None or net > float(proven["backtest"]["net_profit"])):
                    proven = row
                if stale >= ATTEMPT_LIMIT:
                    break
            if proven is None:
                title = best_row["title"] if best_row else name
                net_text = best_row["backtest"]["net_profit"] if best_row else "n/a"
                report["scrapped"].append({"name": name, "best": title, "net_profit": net_text, "changes": stale, "batch": batch + 1})
                _note(
                    report, board, "Risk Manager",
                    f"Scrapping this batch of {name}. After {stale} changes without a better profit, the best result was {title} at {net_text}.",
                )
                continue
            proven["active"] = False
            proven["recommended"] = False
            proven["accepted"] = True
            proven["held"] = True
            proven["perceived_profit"] = proven["backtest"]["net_profit"]
            proven["family"] = name
            decision, replaced = _keep(accepted, kept, proven)
            others = ", ".join(item["title"] for item in accepted) or "none yet"
            if decision == "passed":
                weakest = min(accepted, key=lambda item: float(item["backtest"]["net_profit"]))
                _note(
                    report, board, "Chart researcher",
                    f"Compared {proven['title']} with the meeting list ({others}). "
                    f"Profit {proven['backtest']['net_profit']}, drawdown {proven['backtest']['max_drawdown']}. "
                    f"It clears the trail, and it does not beat {weakest['title']}, so it stays off the list.",
                )
                continue
            _note(
                report, board, "Chart researcher",
                f"Compared {proven['title']} with the rules already kept. "
                f"Profit {proven['backtest']['net_profit']}, drawdown {proven['backtest']['max_drawdown']}. "
                f"{_frame_text(proven)} "
                + (f"It replaces {replaced['title']}." if replaced else "It is kept for the meeting."),
            )
            _note(
                report, board, "Trading Analyst",
                f"{proven['title']} is on the meeting list. {len(accepted)} of {MEETING_SLATE} are filled. It is not live.",
            )
            report["accepted"] = [_public(row) for row in accepted]
            if proven["backtest"].get("reached_target"):
                _remember(report, proven)
            if board is not None:
                _file_candidate(proven, instrument, timeframe, account, account_size, profit_target)
        if len(accepted) >= MEETING_SLATE:
            break
        batch += 1
        _note(
            report, board, "Chart researcher",
            f"{len(accepted)} of {MEETING_SLATE} rules clear the Apex trail. "
            "Posting another batch of settings for the developer to implement and backtest.",
            channels=("R&D", "2min chart developer"),
        )
    accepted.sort(key=lambda row: float(row["backtest"]["net_profit"]), reverse=True)
    report["accepted"] = [_public(row) for row in accepted[:MEETING_SLATE]]
    if board is not None and accepted:
        _publish(accepted[:MEETING_SLATE], instrument, timeframe, account, account_size, profit_target)
    if len(accepted) >= MEETING_SLATE:
        names = ", ".join(row["title"] for row in accepted[:MEETING_SLATE])
        _note(
            report, board, "Portfolio Manager",
            f"Three rules are ready for the next meeting: {names}. None are live until you turn one on.",
            channels=("R&D", "Portfolio Manager"),
        )
    else:
        _note(report, board, "Portfolio Manager", "Research stopped before three rules cleared the Apex trail.")
    report["status"] = "done"
    report["finished"] = _now()
    save_report(report)
    return report


def start_search(board: Board) -> bool:
    """Run the search on the stored bars. Returns False when one is already running."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def _run() -> None:
        global _running
        ready = False
        try:
            strategy = get_strategy() or {}
            bars = load_bars()
            if len(bars) < 80:
                report = load_report()
                _note(report, board, "2min chart developer", "The bar file is too short to test. Load the 15-minute ES file first.")
                report["status"] = "error"
                save_report(report)
                return
            from futuresfund.charts import load_chart

            frames = {"15m": bars}
            for frame in ("2m", "5m"):
                extra = load_chart(frame)
                if len(extra) >= 80:
                    frames[frame] = extra
            search(
                bars,
                instrument=str(strategy.get("contract") or "ES1!"),
                account_size=float(strategy.get("account_size") or 50000),
                profit_target=float(strategy.get("profit_target") or 3000),
                timeframe=str(strategy.get("timeframe") or "5m"),
                account=str(strategy.get("account") or ""),
                board=board,
                frames=frames,
            )
        finally:
            if board is not None:
                board.set_status("Chart researcher", "idle")
                board.set_status("Chart researcher", "idle")
                board.set_status("2min chart developer", "idle")
            with _lock:
                _running = False
    threading.Thread(target=_run, daemon=True, name="futures-lab").start()
    return True


def _keep(accepted: list[dict], kept: set[str], proven: dict) -> tuple[str, dict | None]:
    """Add a passing rule, or replace the weakest when the new profit is higher."""
    if len(accepted) < MEETING_SLATE:
        accepted.append(proven)
        kept.add(proven["family"])
        return "added", None
    weakest = min(accepted, key=lambda item: float(item["backtest"]["net_profit"]))
    if float(proven["backtest"]["net_profit"]) <= float(weakest["backtest"]["net_profit"]):
        return "passed", None
    kept.discard(weakest.get("family"))
    accepted.remove(weakest)
    accepted.append(proven)
    kept.add(proven["family"])
    return "replaced", weakest


def _publish(accepted: list[dict], instrument: str, timeframe: str, account: str, account_size: float, profit_target: float) -> None:
    """The meeting list is these rules. A rule the user already turned live stays on the book."""
    strategy = get_strategy() or {
        "contract": instrument,
        "timeframe": timeframe,
        "account": account,
        "account_size": account_size,
        "profit_target": profit_target,
        "qty": 1,
        "stance": "paused",
        "strategies": [],
    }
    chosen = {row["id"]: row for row in accepted}
    rows = []
    for item in strategy.get("strategies") or []:
        if item.get("active"):
            rows.append(item)
            chosen.pop(item.get("id"), None)
        elif item.get("id") in chosen:
            rows.append(chosen.pop(item["id"]))
    rows = list(chosen.values()) + rows
    strategy["strategies"] = rows
    strategy["stance"] = "paused"
    strategy["proven"] = False
    store_strategy(strategy)


def slate_ready() -> bool:
    return len(load_report().get("accepted") or []) >= MEETING_SLATE


def _stops(batch: int) -> tuple[int, ...]:
    ladders = (
        (250, 500, 1000, 1500),
        (100, 200, 400, 800),
        (150, 300, 600, 1200),
    )
    return ladders[batch % len(ladders)]


def _families(batch: int = 0) -> list[tuple[str, list[dict]]]:
    stops = _stops(batch)
    base = [
        ("ema_cross", [{"fast": fast, "slow": slow} for fast in range(5, 31, 5) for slow in range(20, 121, 20) if fast < slow]),
        ("donchian", [{"length": length} for length in range(10, 61, 5)]),
        ("rsi_revert", [{"period": period, "low": low, "high": high} for period in (8, 14, 21) for low in (20, 30, 40) for high in (60, 70, 80)]),
        ("bollinger", [{"length": length, "dev": dev} for length in (14, 20, 30) for dev in (2, 3)]),
        ("macd", [{"fast": fast, "slow": slow, "signal": 9} for fast in (8, 12, 16) for slow in (21, 26, 34) if fast < slow]),
        ("supertrend", [{"length": length, "mult": mult} for length in (7, 10, 14) for mult in (2, 3, 4)]),
        ("vwap_side", [{"length": length} for length in range(10, 61, 10)]),
        ("poc_pullback", [{"lookback": look, "rows": rows} for look in (30, 50, 80, 100) for rows in (6, 8, 12, 16)]),
    ]
    families = []
    for name, grid in base:
        expanded = [{**params, "stop": stop} for params in grid for stop in stops]
        shift = (batch * ATTEMPT_LIMIT) % len(expanded)
        families.append((name, expanded[shift:] + expanded[:shift]))
    return families


def _assign(report: dict, board: Board | None, name: str, batch: int) -> None:
    if board is not None:
        board.set_status("Chart researcher", "working")
        board.set_status("2min chart developer", "working")
    _note(
        report, board, "Chart researcher",
        f"Batch {batch + 1}: test {name} with a stop inside the trailing allowance. "
        "Developer, implement it and backtest the settings on the engine.",
        channels=("R&D", "2min chart developer"),
    )


def _file_candidate(row: dict, instrument: str, timeframe: str, account: str, account_size: float, profit_target: float) -> None:
    strategy = get_strategy() or {
        "contract": instrument,
        "timeframe": timeframe,
        "account": account,
        "account_size": account_size,
        "profit_target": profit_target,
        "qty": row.get("qty") or 1,
        "stance": "paused",
        "strategies": [],
    }
    rows = [item for item in strategy.get("strategies") or [] if item.get("id") != row["id"]]
    rows.insert(0, row)
    from futuresfund.prop_rules import gate

    if not gate(row, account_size):
        return
    strategy["strategies"] = rows
    strategy.setdefault("stance", "paused")
    store_strategy(strategy)


def _remember(report: dict, row: dict) -> None:
    entry = {
        "id": row["id"],
        "title": row["title"],
        "formula": row["formula"],
        "backtest": row["backtest"],
        "recorded": _now(),
        "why": "Reached the profit target and never touched the intraday trailing floor.",
    }
    report["playbook"] = [item for item in report.get("playbook") or [] if item.get("id") != row["id"]]
    report["playbook"].insert(0, entry)
    report["playbook"] = report["playbook"][:20]


def _score(frames, bars, instrument, account_size, profit_target, name, params):
    if frames and len(frames) > 1:
        return compare_timeframes(frames, instrument, account_size, profit_target, name, params)
    return evaluate_rule(bars, instrument, account_size, profit_target, name, params)


def _frame_text(row: dict) -> str:
    frames = row.get("frames") or []
    if not frames:
        return ""
    parts = [
        f"{item['timeframe']} profit {item['net_profit']} drawdown {item['max_drawdown']}"
        + ("" if item["proven"] else " (over the trail)")
        for item in frames
    ]
    return "Timeframes: " + "; ".join(parts) + "."


def _public(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "formula": row["formula"],
        "backtest": row["backtest"],
        "frames": row.get("frames") or [],
        "accepted": True,
    }


def _note(report: dict, board: Board | None, author: str, text: str, channels: tuple[str, ...] = ("R&D",)) -> None:
    report.setdefault("messages", []).append({"time": _now(), "author": author, "text": text})
    report["messages"] = report["messages"][-200:]
    save_report(report)
    if board is not None:
        for channel in channels:
            board.post(author, text, kind="report", channel=channel)


def record_trial(row: dict, note: str) -> None:
    """Keep a developer backtest on the engine page. The meeting result is otherwise only a chat line."""
    report = load_report()
    backtest = row.get("backtest") or {}
    report.setdefault("trials", []).append({
        "id": row.get("id"),
        "title": row.get("title"),
        "name": (row.get("rule") or {}).get("name"),
        "net_profit": backtest.get("net_profit"),
        "max_drawdown": backtest.get("max_drawdown"),
        "trades": backtest.get("trades"),
        "proven": bool(row.get("proven")),
    })
    report["trials"] = report["trials"][-80:]
    report["attempts"] = int(report.get("attempts") or 0) + 1
    report["status"] = "done"
    report.setdefault("messages", []).append({"time": _now(), "author": "2min chart developer", "text": note})
    report["messages"] = report["messages"][-200:]
    save_report(report)


def _empty() -> dict:
    return {"status": "idle", "messages": [], "trials": [], "accepted": [], "scrapped": [], "playbook": [], "attempts": 0}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
