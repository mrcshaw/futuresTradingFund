"""The live futures strategy: one timeframe, one direction, and whether alerts may trade it."""

from __future__ import annotations

import json
import re
from pathlib import Path

from futuresfund.book import load, save
from futuresfund.contracts import root_of
from futuresfund.crosstrade import trade_side
from futuresfund.research import active_rules

TIMEFRAMES = {
    "1m": "1 minute",
    "2m": "2 minutes",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "1D": "1 day",
}

_ALIASES = {
    "1": "1m", "1m": "1m", "1min": "1m", "1minute": "1m",
    "2": "2m", "2m": "2m", "2min": "2m",
    "5": "5m", "5m": "5m", "5min": "5m",
    "15": "15m", "15m": "15m", "15min": "15m",
    "30": "30m", "30m": "30m", "30min": "30m",
    "60": "1h", "60m": "1h", "1h": "1h", "1hr": "1h", "1hour": "1h",
    "240": "4h", "240m": "4h", "4h": "4h", "4hr": "4h",
    "d": "1D", "1d": "1D", "1day": "1D", "day": "1D", "daily": "1D",
}


def normalize_timeframe(value: str) -> str:
    key = str(value or "").strip().lower().replace(" ", "")
    found = _ALIASES.get(key)
    if not found:
        raise ValueError("Timeframe must be 1m, 2m, 5m, 15m, 30m, 1h, 4h, or 1D.")
    return found


def same_direction(left: str, right: str) -> bool:
    return trade_side(left or "") == trade_side(right or "")


def meeting_decision(current: str, new_rating: str) -> str:
    """Keep when the new rating has the same side. Adjust when it flips or goes flat."""
    return "keep" if same_direction(current, new_rating) else "adjust"


def get_strategy() -> dict | None:
    strategy = load().get("strategy")
    return strategy if isinstance(strategy, dict) else None


def lead_script() -> dict | None:
    """The script on headquarters. A meeting choice stays until the next meeting. Otherwise the best study."""
    book = load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    if strategy.get("last_meeting"):
        chosen = book.get("headquarters_meeting")
        if isinstance(chosen, dict) and chosen.get("pine"):
            chosen = dict(chosen)
            chosen["pine"] = _legal_defaults(chosen["pine"])
            return chosen
    research = book.get("headquarters_research")
    if isinstance(research, dict) and research.get("engine") == "backtrader" and research.get("pine"):
        research = dict(research)
        research["pine"] = _legal_defaults(research["pine"])
        return research
    stored = _best_saved_strategy(strategy)
    return stored


def remember_meeting(item: dict | None) -> None:
    """The meeting's choice replaces the research stand-in until the next meeting."""
    if not item or not item.get("pine"):
        return
    backtest = item.get("backtest") or {}
    book = load()
    book["headquarters_meeting"] = {
        "id": item.get("id"),
        "title": item.get("title"),
        "formula": item.get("formula") or "",
        "pine": item.get("pine") or "",
        "net_profit": backtest.get("net_profit"),
        "max_drawdown": backtest.get("max_drawdown"),
        "trades": backtest.get("trades"),
        "timeframe": item.get("timeframe") or "5m",
        "take_profit": item.get("take_profit"),
        "source": "meeting",
        "passed": bool(item.get("proven")),
    }
    save(book)


def remember_research(row: dict, filename: str, title: str) -> None:
    """Keep headquarters on the best study under the objective until a meeting decides."""
    book = load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    if strategy.get("last_meeting"):
        return
    if str(row.get("engine") or row.get("runner") or "") != "backtrader":
        return
    note = str(row.get("note") or "")
    if row.get("blocked") or int(row.get("trades") or 0) < 1:
        return
    if "price rule" in note or "not substituted" in note or "cannot enter" in note:
        return
    candidate = _candidate(row, filename, title)
    if not candidate or not candidate.get("pine"):
        return
    current = book.get("headquarters_research")
    if isinstance(current, dict) and _score(current) >= _score(candidate):
        return
    book["headquarters_research"] = candidate
    if isinstance(strategy, dict) and candidate.get("timeframe"):
        strategy = dict(strategy)
        strategy["timeframe"] = candidate["timeframe"]
        book["strategy"] = strategy
    save(book)


def absorb_research_notes() -> None:
    """Read finished studies once so headquarters is current before the next attempt."""
    notes = Path(__file__).resolve().parent / "researchNotes"
    if not notes.is_dir():
        return
    book = load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    if strategy.get("last_meeting"):
        return
    book.pop("headquarters_research", None)
    best = None
    changed = False
    for path in notes.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        row = data.get("best") if isinstance(data.get("best"), dict) else {}
        if not row or _stand_in(row):
            continue
        if str(row.get("engine") or row.get("runner") or "") != "backtrader":
            continue
        candidate = _candidate(row, str(data.get("file") or path.stem), str(data.get("title") or path.stem))
        if not candidate or not candidate.get("pine"):
            continue
        if best is None or _score(candidate) > _score(best):
            best = candidate
            changed = True
    if best:
        book["headquarters_research"] = best
        if isinstance(strategy, dict) and best.get("timeframe"):
            strategy = dict(strategy)
            strategy["timeframe"] = best["timeframe"]
            book["strategy"] = strategy
    if changed or "headquarters_research" not in book:
        save(book)


_STAND_INS = {"donchian", "ema_cross", "rsi_revert", "vwap_side", "bollinger", "macd", "poc_pullback", "supertrend"}
_LEADER_CACHE: dict = {"stamp": None, "rows": None}
_LEADER_FILE_CACHE: dict[str, tuple[int, dict | None]] = {}


def _stand_in(row: dict) -> bool:
    note = str(row.get("note") or "")
    if "price rule" in note or "not substituted" in note or "cannot enter" in note:
        return True
    runner = str(row.get("runner") or "")
    return runner in _STAND_INS


def book_account(strategy: dict | None) -> str:
    """The account already stored on the book, otherwise the first configured name, otherwise paper."""
    named = str((strategy or {}).get("account") or "").strip()
    if named:
        return named
    from futuresfund.config import prop_accounts

    configured = sorted(prop_accounts())
    return configured[0] if configured else "paper"


def consistent_leaders(limit: int = 3) -> list[dict]:
    """The headquarters leaders: studies that hit the profit target inside the trailing drawdown, both set on
    headquarters, at 130 to 600 trades a year. Fewer than 130 is too quiet and more than 600 is too many trades. Two or three trades is not a strategy. The lowest drawdown first, then the profit. Trade count
    only breaks a tie.

    How many charts finished green is shown, not ranked. A stand-in price rule is not included. The
    account number is not part of the ranking.
    """
    from futuresfund.objective import rank

    notes = Path(__file__).resolve().parent / "researchNotes"
    stamp = _notes_stamp(notes)
    cached = _LEADER_CACHE.get("rows")
    if _LEADER_CACHE.get("stamp") == stamp and cached is not None:
        return [dict(row) for row in cached[:limit]]
    ranked: dict[str, dict] = {}
    if notes.is_dir():
        for path in notes.glob("*.json"):
            row = _leader_row(path)
            if row is None:
                continue
            key = str(row["title"]).strip().lower()
            current = ranked.get(key)
            if current is None or rank(row) > rank(current):
                ranked[key] = row
    rows = sorted(ranked.values(), key=rank, reverse=True)
    _LEADER_CACHE["stamp"] = stamp
    _LEADER_CACHE["rows"] = rows
    return [dict(row) for row in rows[:limit]]


def _notes_stamp(notes: Path) -> tuple:
    """Changes when a study is added, removed, or rewritten. A rewrite alone does not move the folder's own time."""
    if not notes.is_dir():
        return (0, 0, 0)
    latest = 0
    count = 0
    for path in notes.glob("*.json"):
        try:
            latest = max(latest, path.stat().st_mtime_ns)
        except OSError:
            continue
        count += 1
    return (notes.stat().st_mtime, latest, count)


def _leader_row(path: Path) -> dict | None:
    key = str(path)
    try:
        modified = path.stat().st_mtime_ns
    except OSError:
        _LEADER_FILE_CACHE.pop(key, None)
        return None
    cached = _LEADER_FILE_CACHE.get(key)
    if cached is not None and cached[0] == modified:
        return cached[1]
    row = _read_leader_row(path)
    _LEADER_FILE_CACHE[key] = (modified, row)
    return row


def _read_leader_row(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    best = data.get("best") if isinstance(data.get("best"), dict) else {}
    runner = str(best.get("runner") or "")
    engine = str(best.get("engine") or runner)
    if runner in _STAND_INS or engine in _STAND_INS:
        return None
    if engine != "backtrader" and runner != "backtrader":
        return None
    if _stand_in(best) or best.get("net_profit") is None:
        return None
    try:
        profit = float(best["net_profit"])
        drawdown = float(best.get("max_drawdown") or 0)
        trades = int(best.get("trades") or 0)
    except (TypeError, ValueError):
        return None
    from futuresfund.objective import counts, pace

    if not counts(best):
        return None
    green, chart_count = _chart_tally(data, best)
    title = str(data.get("title") or "").strip()
    if not title or title.lower().startswith("untitled"):
        title = path.stem.replace("_", " ")
    return {
        "id": path.stem,
        "title": title,
        "file": str(data.get("file") or ""),
        "timeframe": str(best.get("timeframe") or "5m"),
        "net_profit": profit,
        "max_drawdown": drawdown,
        "trades": trades,
        "trades_per_day": pace(best),
        "green": green,
        "charts": f"{green} of {chart_count} charts finished green",
    }


def _chart_tally(data: dict, best: dict) -> tuple[int, int]:
    """How many finished charts made money. Reported next to a leader, not part of its rank."""
    rows = []
    instruments = data.get("instruments") if isinstance(data.get("instruments"), dict) else {}
    for record in instruments.values():
        if not isinstance(record, dict):
            continue
        charts = record.get("charts") if isinstance(record.get("charts"), dict) else {}
        for chart in charts.values():
            if isinstance(chart, dict) and isinstance(chart.get("best"), dict):
                rows.append(chart["best"])
        if not charts and isinstance(record.get("best"), dict):
            rows.append(record["best"])
    if not rows:
        rows = [frame for frame in (best.get("frames") or []) if isinstance(frame, dict)] or [best]
    tested = 0
    green = 0
    for row in rows:
        try:
            row_trades = int(row.get("trades") or 0)
            row_profit = float(row.get("net_profit") or 0)
        except (TypeError, ValueError):
            continue
        if row_trades <= 0:
            continue
        tested += 1
        if row_profit > 0:
            green += 1
    return green, tested


def pine_text(filename: str) -> str:
    return _read_pine(filename)


def leader_script(strategy_id: str) -> dict | None:
    """The Pine script for one of the three headquarters strategies."""
    row = next((item for item in consistent_leaders(3) if item["id"] == strategy_id), None)
    if row is None:
        return None
    return {"id": row["id"], "title": row["title"], "pine": pine_text(row.get("file") or "")}


def _score(item: dict | None) -> tuple:
    """The research objective: the profit target inside the headquarters drawdown, the lowest drawdown, then the profit."""
    from futuresfund.objective import rank

    if not item:
        return (-1, 0, -10**12, 0.0, 0)
    return rank(item)


def _prefer(left: dict | None, right: dict | None) -> dict | None:
    if _score(left) >= _score(right):
        return left or right
    return right


def _best_saved_strategy(strategy: dict) -> dict | None:
    rows = []
    for item in strategy.get("strategies") or []:
        if not item.get("pine"):
            continue
        backtest = item.get("backtest") or {}
        rows.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "formula": item.get("formula") or "",
            "pine": item.get("pine") or "",
            "net_profit": backtest.get("net_profit"),
            "max_drawdown": backtest.get("max_drawdown"),
            "trades": backtest.get("trades"),
            "timeframe": item.get("timeframe") or strategy.get("timeframe") or "5m",
            "take_profit": item.get("take_profit"),
            "passed": bool(item.get("proven")),
            "source": "book",
        })
    if not rows:
        return None
    return max(rows, key=_score)


def _candidate(row: dict, filename: str, title: str) -> dict | None:
    pine = _pine_for(row, filename, title)
    if not pine:
        return None
    names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
    timeframe = row.get("timeframe") or "5m"
    take_profit = row.get("take_profit")
    target = f" Take profit ${float(take_profit):,.0f}." if isinstance(take_profit, (int, float)) and float(take_profit) > 0 else ""
    from futuresfund.objective import pace

    rate = pace(row)
    paced = f", {rate:g} a day" if rate is not None else ""
    formula = (
        f"PineTS ran this Pine script on the {names.get(timeframe, timeframe)} chart. "
        f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}{paced}.{target}"
    )
    return {
        "id": filename,
        "title": title,
        "formula": formula,
        "pine": pine,
        "net_profit": row.get("net_profit"),
        "max_drawdown": row.get("max_drawdown"),
        "trades": row.get("trades"),
        "trades_per_day": rate,
        "days": row.get("days"),
        "instrument": row.get("instrument") or "",
        "timeframe": timeframe,
        "take_profit": take_profit,
        "passed": bool(row.get("passed")),
        "source": "research",
        "engine": "backtrader",
    }


def _pine_for(row: dict, filename: str, title: str) -> str:
    from futuresfund.pines import render

    runner = str(row.get("runner") or "")
    params = dict(row.get("params") or {})
    names = {"5m": "5-minute", "2m": "2-minute", "15m": "15-minute"}
    chart = names.get(row.get("timeframe") or "5m", row.get("timeframe") or "5-minute")
    banner = (
        f"// Headquarters. Tested on the {chart} chart. "
        f"Profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}.\n"
    )
    if runner in {"donchian", "ema_cross", "rsi_revert", "vwap_side", "bollinger", "macd", "poc_pullback"}:
        tuned = _family_params(runner, params)
        return _version_five(render(runner, tuned, f"{title} {chart}", 50, 50000), banner)
    text = _read_pine(filename)
    if not text:
        return ""
    return _version_five(_apply_inputs(text, params), banner)


def _version_five(text: str, banner: str) -> str:
    """Headquarters shows version 5, with the version line first so it can be pasted."""
    body = re.sub(r"//@version\s*=\s*\d+", "//@version=5", text, count=1)
    if body.startswith("//@version=5"):
        rest = body.split("\n", 1)[1] if "\n" in body else ""
        return f"//@version=5\n{banner}{rest}"
    return f"//@version=5\n{banner}{body}"


def _family_params(runner: str, params: dict) -> dict:
    length = params.get("length") or params.get("profile_lookback") or params.get("ema_len") or 20
    try:
        length = max(10, min(60, int(float(length))))
    except (TypeError, ValueError):
        length = 20
    stop = params.get("stop") or params.get("stop_dollars") or 250
    try:
        stop = float(stop)
    except (TypeError, ValueError):
        stop = 250.0
    if runner == "ema_cross":
        fast = max(5, min(20, max(5, length // 3)))
        slow = max(fast + 5, min(120, max(length, fast + 10)))
        return {"fast": fast, "slow": slow, "stop": stop}
    if runner == "rsi_revert":
        return {"period": max(8, min(21, length // 3 or 14)), "low": 30, "high": 70, "stop": stop}
    if runner == "bollinger":
        return {"length": length, "dev": float(params.get("dev") or 2), "stop": stop}
    if runner == "macd":
        return {"fast": int(params.get("fast") or 12), "slow": int(params.get("slow") or 26), "signal": int(params.get("signal") or 9), "stop": stop}
    if runner == "poc_pullback":
        return {"lookback": length, "rows": int(params.get("profile_rows") or params.get("rows") or 12), "stop": stop}
    if runner == "vwap_side":
        return {"length": length, "stop": stop}
    return {"length": length, "stop": stop}


def _read_pine(filename: str) -> str:
    root = Path(__file__).resolve().parent
    name = Path(filename)
    stem = name.stem[:-9] if name.stem.endswith("_strategy") else name.stem
    candidates = [name.name, f"{stem}.pine", f"{name.stem}.pine", f"{stem}.txt", f"{name.stem}.txt"]
    if name.name and not name.name.startswith("created_"):
        candidates.append(f"created_{name.name}")
        candidates.append(f"created_{stem}.txt")
    for folder in (root / "learningStrategies", root / "learnedStrategies", root / "createdStrategies"):
        for candidate in candidates:
            path = folder / candidate
            if not path.is_file() or path.suffix.lower() not in {".pine", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix.lower() == ".txt" and "//@version" not in "\n".join(text.splitlines()[:40]):
                continue
            return text
    return ""


def _apply_inputs(text: str, params: dict) -> str:
    for key, value in params.items():
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, (int, float)):
            literal = _literal(_clamp_default(text, key, value))
        else:
            continue
        text = re.sub(
            rf"({re.escape(key)}\s*=\s*input\.(?:int|float|bool)\s*\()\s*[^,\)]+",
            rf"\g<1>{literal}",
            text,
            count=1,
        )
    return _legal_defaults(text)


def _clamp_default(text: str, key: str, value):
    match = re.search(
        rf"{re.escape(key)}\s*=\s*input\.(?:int|float|bool)\s*\((.*?)\)",
        text,
        re.DOTALL,
    )
    if not match or isinstance(value, bool):
        return value
    low = _bound_in(match.group(1), "minval")
    high = _bound_in(match.group(1), "maxval")
    number = float(value)
    if low is not None:
        number = max(float(low), number)
    if high is not None:
        number = min(float(high), number)
    if isinstance(value, int) and not isinstance(value, bool):
        return int(round(number))
    return number


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return str(value)


def _bound_in(body: str, key: str):
    match = re.search(rf"\b{key}\s*=\s*(-?\d+(?:\.\d+)?)", body or "")
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def _legal_defaults(text: str) -> str:
    """A pasted script has to be legal in TradingView: the default stays inside min and max."""
    pattern = re.compile(r"(input\.(?:int|float)\s*\(\s*)(-?\d+(?:\.\d+)?)(\s*,[^)]*)", re.DOTALL)

    def fix(match: re.Match) -> str:
        number = match.group(2)
        rest = match.group(3)
        low = _bound_in(rest, "minval")
        high = _bound_in(rest, "maxval")
        value = float(number)
        if low is not None:
            value = max(float(low), value)
        if high is not None:
            value = min(float(high), value)
        shown = str(int(value)) if re.fullmatch(r"-?\d+", number) else format(value, ".10g")
        return match.group(1) + shown + rest

    return pattern.sub(fix, text)


def store_strategy(strategy: dict) -> dict:
    book = load()
    saved = dict(strategy)
    saved.pop("running", None)
    book["strategy"] = saved
    save(book)
    return saved


def series_match(strategy: dict | None, signal: dict) -> tuple[bool, str]:
    """An alert may add its bar only for the account, contract, and timeframe of this book."""
    if not strategy or not (strategy.get("strategies") or strategy.get("rule") or strategy.get("account") or strategy.get("running")):
        return False, "No tested strategy yet. Upload the bar file and run the initial analysis first."
    alert_frame = signal.get("timeframe")
    book_frame = strategy.get("timeframe")
    if alert_frame and book_frame:
        from futuresfund.timeframe import bar_minutes

        if bar_minutes(alert_frame) > bar_minutes(book_frame):
            return False, (
                f"Alert timeframe {alert_frame} is coarser than the strategy timeframe {book_frame}. "
                "A finer alert can watch the stop while the strategy candle is open."
            )
    try:
        if strategy.get("contract") and root_of(signal["instrument"]) != root_of(str(strategy.get("contract") or "")):
            return False, "Alert contract does not match the strategy contract."
    except ValueError as exc:
        return False, str(exc)
    from futuresfund.alerts import BAR_FEED_ID

    if signal.get("id") == BAR_FEED_ID:
        return True, "The price alert is recorded for the chart."
    running = [row for row in strategy.get("running") or [] if isinstance(row, dict)]
    if running:
        found, detail = _named_strategy(running, signal.get("strategy"))
        if found is None:
            return False, detail
        return True, f"The alert is {found.get('title')} for {signal.get('account')}."
    named = str(signal.get("account") or strategy.get("account") or "").strip()
    if not named:
        return False, "The alert has no account, so the order was not sent."
    return True, f"The order uses account {named}."


def _named_strategy(running: list[dict], name) -> tuple[dict | None, str]:
    """The running strategy named in the TradingView alert. The account can be shared."""
    text = str(name or "").strip()
    if not text:
        return None, "The alert needs a strategy name."
    folded = _fold_name(text)
    for row in running:
        if folded in {_fold_name(row.get("title")), _fold_name(row.get("id"))}:
            return row, ""
    return None, f"No running strategy is named {text}."


def _fold_name(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def accept_alert(strategy: dict | None, signal: dict) -> tuple[bool, str]:
    """An interval alert may trade only when a proven strategy is active for this book."""
    matched, detail = series_match(strategy, signal)
    if not matched:
        return False, detail
    if not active_rules(strategy):
        return False, "No active strategy is armed. The bar was still added for the next meeting."
    return True, detail
