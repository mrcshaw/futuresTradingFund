"""Account-percent risk used by every research backtest.

The stop, the take profit, and the trail are a percent of the account. The
engine turns that percent into dollars from the account size. The point value
follows the contract, so the same percent is the same share of the account on
NQ, ES, and gold. The risk manager reads the last hour of completed backtests
and suggests a fix for the problem that shows up most. Apply adds that line to
the research guide. Deny leaves the guide as it is.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

RISK_DEFAULTS = {
    "stop_dollars": 1000.0,
    "tp_dollars": 1500.0,
    "use_trail": False,
    "trail_pct": 50.0,
    "trail_step_amt": 500.0,
    "trail_step_pct": 50.0,
    "trail_min_dollars": 1000.0,
}
STOP_PCT_CAP = 3.0
TP_PCT_CAP = 4.0
STOP_CAP = 1500.0  # 3 percent of a $50,000 account, for a leftover dollar stop
TP_CAP = 2000.0    # 4 percent of a $50,000 account, for a leftover dollar target

GUIDE_PATH = Path(__file__).resolve().parent / "researchNotes" / "how_to_research.md"
SUGGESTIONS_PATH = Path(__file__).resolve().parent / "researchNotes" / "risk_suggestions.json"
HOUR_SECONDS = 3600
_LOCK = threading.Lock()
_ISSUE_ORDER = ("loss-past-stop", "drawdown-before-profit", "losing-and-too-many-trades")
_PROPOSED = {
    "loss-past-stop": (
        "Do not raise the stop to cover a fill that went past it. "
        "One trade down more than 3.6 percent of the account stays off the list. "
        "The stop starts at $50 and rises by $100 up to $1,500."
    ),
    "drawdown-before-profit": (
        "When a run draws down 5 percent of the account or more, try the trail before raising the stop. "
        "use_trail true keeps 50 percent of the peak, starts after 2 percent of the account, "
        "and tightens by 50 percent every 1 percent of the account. "
        "The stop caps at 3 percent of the account and the take profit caps at 4 percent."
    ),
    "losing-and-too-many-trades": (
        "On a losing script with more than 600 trades a year, tighten the entry before moving the stop "
        "or the take profit. The stop starts at $50 and the take profit starts at $500."
    ),
}
_REASON = {
    "loss-past-stop": "One trade lost more than 3.6 percent of the account.",
    "drawdown-before-profit": "Drawdown reached 5 percent of the account.",
    "losing-and-too-many-trades": "The run lost money and traded more than 600 times a year.",
}


def point_value(root: str) -> float:
    """Dollars per point for this contract. An unknown root uses the ES value."""
    from futuresfund.contracts import POINT_VALUE, root_of

    return float(POINT_VALUE.get(root_of(root or "ES"), 50))


def merge_risk(inputs: dict | None, root: str) -> dict:
    """The settings for this run, with the dollar risk filled in where the script did not set it."""
    merged = dict(inputs or {})
    for key, value in RISK_DEFAULTS.items():
        if merged.get(key) in (None, ""):
            merged[key] = value
    merged["pv"] = point_value(root)
    merged["_risk_style"] = True
    given = inputs or {}
    stop_pct = _number(given.get("stop_pct"))
    target_pct = _number(given.get("tp_pct"))
    if stop_pct is not None:
        merged["stop_pct"] = min(float(stop_pct), STOP_PCT_CAP)
        merged["stop_dollars"] = _account_size() * merged["stop_pct"] / 100.0
    if target_pct is not None:
        merged["tp_pct"] = min(float(target_pct), TP_PCT_CAP)
        merged["tp_dollars"] = _account_size() * merged["tp_pct"] / 100.0
    stop = _number(merged.get("stop_dollars"))
    target = _number(merged.get("tp_dollars"))
    if stop_pct is None and stop is not None and stop > STOP_CAP:
        merged["stop_dollars"] = STOP_CAP
    if target_pct is None and target is not None and target > TP_CAP:
        merged["tp_dollars"] = TP_CAP
    return merged


def _account_size() -> float:
    try:
        from futuresfund.learn import ACCOUNT_SIZE

        return float(ACCOUNT_SIZE)
    except Exception:
        return 50000.0


def note_risk(row: dict, filename: str, title: str, now: float | None = None) -> str | None:
    """Remember this completed backtest, then suggest a fix for the most common problem in the last hour."""
    if not isinstance(row, dict) or row.get("net_profit") is None or row.get("error") or row.get("fatal"):
        return None
    stamp = time.time() if now is None else float(now)
    with _LOCK:
        data = _read()
        data["runs"].append({"at": stamp, "stats": _stats(row, filename, title)})
        message, _changed = _review(data, stamp)
        _write(data)
        return message


def look(now: float | None = None) -> tuple[str | None, list[dict]]:
    """Read the last hour and return a new suggestion message plus the current list."""
    stamp = time.time() if now is None else float(now)
    with _LOCK:
        data = _read()
        message, changed = _review(data, stamp)
        if changed:
            _write(data)
        return message, [dict(item) for item in data["suggestions"]]


def suggestions(now: float | None = None) -> list[dict]:
    _, rows = look(now)
    return rows


def apply(suggestion_id: str) -> dict:
    """Add the suggested line to the research guide."""
    with _LOCK:
        data = _read()
        found = _find(data, suggestion_id)
        sentence = str(found.get("proposed") or "").strip()
        if sentence and found.get("status") != "applied":
            _add_guideline(sentence)
        found["status"] = "applied"
        _write(data)
        return dict(found)


def approve(suggestion_id: str) -> dict:
    """The earlier name for apply."""
    return apply(suggestion_id)


def deny(suggestion_id: str, now: float | None = None) -> dict:
    """Leave the research guide unchanged, mark denied, and wait an hour before the next suggestion."""
    stamp = time.time() if now is None else float(now)
    with _LOCK:
        data = _read()
        found = _find(data, suggestion_id)
        if found.get("status") != "applied":
            found["status"] = "denied"
            data["last_suggestion_at"] = stamp
            _write(data)
        return dict(found)


def _review(data: dict, now: float) -> tuple[str | None, bool]:
    """Generate a new suggestion at most once an hour.

    A pending or applied line stays as it is. A denied line can be suggested again
    after the hour, from the backtests in that new hour.
    """
    changed = False
    if _refresh_pending(data):
        changed = True
    fresh = [run for run in data["runs"] if _number(run.get("at")) is not None and now - float(run["at"]) <= HOUR_SECONDS]
    if len(fresh) != len(data["runs"]):
        data["runs"] = fresh
        changed = True

    last_at = _number(data.get("last_suggestion_at")) or 0.0
    if now - last_at < HOUR_SECONDS:
        return None, changed

    counts: dict[str, list[dict]] = {issue: [] for issue in _ISSUE_ORDER}
    for run in fresh:
        stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
        for issue in _issues(stats):
            counts[issue].append(run)

    waiting = {
        _issue_of(item)
        for item in data["suggestions"]
        if item.get("status") in {"applied", "approved", "pending"}
    }
    ranked = sorted(
        (issue for issue, runs in counts.items() if runs),
        key=lambda issue: (-len(counts[issue]), _ISSUE_ORDER.index(issue)),
    )
    chosen = next((issue for issue in ranked if issue not in waiting), None)
    if chosen is None:
        return None, changed

    idea = _pack(chosen, counts[chosen], len(fresh))
    if any(item.get("id") == chosen for item in data["suggestions"]):
        idea["id"] = f"{chosen}-{int(now)}"
    data["suggestions"].append(idea)
    data["last_suggestion_at"] = now
    return idea["message"], True


def _drawdown_cap() -> float:
    from futuresfund.objective import account_drawdown_cap

    return account_drawdown_cap()


def _trade_loss_cap() -> float:
    from futuresfund.objective import account_trade_loss_cap

    return account_trade_loss_cap()


def _issues(stats: dict) -> list[str]:
    found = []
    loser = _number(stats.get("biggest_loser"))
    if loser is not None and loser < -_trade_loss_cap():
        found.append("loss-past-stop")
    profit = _number(stats.get("net_profit"))
    drawdown = _number(stats.get("max_drawdown"))
    if drawdown is not None and drawdown >= _drawdown_cap():
        found.append("drawdown-before-profit")
    yearly = stats.get("trades_per_year")
    if profit is not None and profit <= 0 and isinstance(yearly, (int, float)) and not isinstance(yearly, bool) and yearly > 600:
        found.append("losing-and-too-many-trades")
    return found


def _issue_of(item: dict) -> str:
    issue = str(item.get("issue") or item.get("id") or "")
    if issue in _PROPOSED:
        return issue
    for name in _ISSUE_ORDER:
        if issue == name or issue.startswith(f"{name}-"):
            return name
    return issue


def _proposed(issue: str) -> str:
    """The suggestion sentence. The drawdown line uses the box on the strategy library page."""
    if issue == "drawdown-before-profit":
        from futuresfund.objective import drawdown_share_text

        share = drawdown_share_text()
        return (
            f"When a run draws down {share} or more, try the trail before raising the stop. "
            "use_trail true keeps 50 percent of the peak, starts after 2 percent of the account, "
            "and tightens by 50 percent every 1 percent of the account. "
            "The stop caps at 3 percent of the account and the take profit caps at 4 percent."
        )
    return _PROPOSED.get(issue, "")


def _reason(issue: str) -> str:
    if issue == "drawdown-before-profit":
        from futuresfund.objective import drawdown_share_text

        return f"Drawdown reached {drawdown_share_text()}."
    return _REASON.get(issue, "")


def _refresh_pending(data: dict) -> bool:
    """A waiting card uses the current sentence, so Apply does not write an old dollar stop."""
    changed = False
    for item in data["suggestions"]:
        if item.get("status") != "pending":
            continue
        issue = _issue_of(item)
        sentence = _proposed(issue)
        if not sentence:
            continue
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        justification = _because(stats, _reason(issue)) if stats else str(item.get("justification") or "")
        if item.get("proposed") == sentence and item.get("justification") == justification:
            continue
        item["issue"] = issue
        item["proposed"] = sentence
        item["justification"] = justification
        item["message"] = f"Suggestion for the research guide: {sentence} {justification}".strip()
        changed = True
    return changed


def _pack(issue: str, runs: list[dict], hour_count: int) -> dict:
    stats = _hour_stats(runs, hour_count)
    proposed = _proposed(issue)
    justification = _because(stats, _reason(issue))
    return {
        "id": issue,
        "issue": issue,
        "proposed": proposed,
        "justification": justification,
        "stats": stats,
        "status": "pending",
        "message": f"Suggestion for the research guide: {proposed} {justification}",
    }


def _hour_stats(runs: list[dict], hour_count: int) -> dict:
    stats = {"hour_runs": hour_count, "issue_runs": len(runs)}
    rows = [run.get("stats") or {} for run in runs]

    def pick(key: str, choose):
        values = [_number(row.get(key)) for row in rows]
        values = [value for value in values if value is not None]
        if values:
            stats[key] = choose(values)

    pick("biggest_loser", min)
    pick("max_drawdown", max)
    pick("net_profit", min)
    pick("trades_per_year", max)
    sample = rows[0] if rows else {}
    for key in ("script", "file", "timeframe", "instrument", "trades", "win_rate", "profit_factor"):
        if sample.get(key) is not None:
            stats[key] = sample[key]
    names = []
    for row in rows:
        name = row.get("script")
        if name and name not in names:
            names.append(str(name))
        if len(names) == 3:
            break
    if names:
        stats["scripts"] = names
    return stats


def _because(stats: dict, reason: str) -> str:
    parts = [
        f"In the last hour, {stats.get('issue_runs')} of {stats.get('hour_runs')} completed backtests had this problem",
        reason,
    ]
    names = stats.get("scripts") or []
    if names:
        extra = stats.get("issue_runs", 0) - len(names)
        shown = ", ".join(names)
        if extra > 0:
            shown += f", and {extra} more"
        parts.append(shown)
    for label, key in (
        ("worst profit", "net_profit"),
        ("worst drawdown", "max_drawdown"),
        ("most trades a year", "trades_per_year"),
        ("largest loss", "biggest_loser"),
    ):
        if stats.get(key) is not None:
            parts.append(f"{label} {stats[key]}")
    return ". ".join(str(part) for part in parts) + "."


def _find(data: dict, suggestion_id: str) -> dict:
    found = next((item for item in data["suggestions"] if item.get("id") == suggestion_id), None)
    if found is None:
        raise KeyError(suggestion_id)
    return found


def _stats(row: dict, filename: str, title: str) -> dict:
    from futuresfund.objective import trades_per_year

    stats = {"script": title or filename, "file": filename}
    for key in ("timeframe", "instrument", "net_profit", "max_drawdown", "trades", "win_rate", "profit_factor", "biggest_loser", "days"):
        if row.get(key) is not None:
            stats[key] = row.get(key)
    yearly = trades_per_year(row)
    if yearly is not None:
        stats["trades_per_year"] = yearly
    return stats


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read() -> dict:
    try:
        data = json.loads(SUGGESTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    suggestions = data.get("suggestions")
    runs = data.get("runs")
    return {
        "suggestions": [row for row in suggestions if isinstance(row, dict)] if isinstance(suggestions, list) else [],
        "runs": [row for row in runs if isinstance(row, dict)] if isinstance(runs, list) else [],
        "last_suggestion_at": _number(data.get("last_suggestion_at")) or 0.0,
    }


def _write(data: dict) -> None:
    SUGGESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUGGESTIONS_PATH.write_text(
        json.dumps({
            "suggestions": data.get("suggestions") or [],
            "runs": data.get("runs") or [],
            "last_suggestion_at": _number(data.get("last_suggestion_at")) or 0.0,
        }, indent=2),
        encoding="utf-8",
    )


def _add_guideline(sentence: str) -> None:
    try:
        text = GUIDE_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if sentence in text:
        return
    heading = "Risk adjustments"
    if heading not in text:
        text = text.rstrip() + f"\n\n{heading}\n{sentence}\n"
    else:
        text = text.rstrip() + f"\n{sentence}\n"
    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUIDE_PATH.write_text(text, encoding="utf-8")
