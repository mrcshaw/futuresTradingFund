"""What the creation team learns from: every strategy this desk has backtested.

A study is one Pine script and its results on every chart and instrument the developers ran.
The exemplars are the studies whose best chart hit the headquarters profit target inside the
headquarters drawdown at 4 or more trades a week, ranked by the research objective in objective.py:
the lowest drawdown first, then profit. Trade count only breaks a tie. The cautions are the studies that
lost money or drew down past the trailing limit. Both go to the strategy developer before a new script
is written.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTES = ROOT / "researchNotes"
FOLDERS = ("learningStrategies", "learnedStrategies", "createdStrategies", "checkScripts")
SKIP = {"library_index.json", "strategy_cards.json", "creation_sources.json", "indicator_library.json", "creation_lessons.json"}

CHART_TRADES = 3        # a chart with fewer trades says little

_cache: dict[str, tuple] = {}
_cache_lock = threading.Lock()


def account_size() -> float:
    from futuresfund.objective import limits

    return float(limits()["size"])


def trailing_limit(size: float | None = None) -> float:
    """The trailing drawdown set on headquarters."""
    from futuresfund.objective import drawdown_limit

    return drawdown_limit()


def drawdown_cap(size: float | None = None) -> float:
    """The most drawdown a one-year strategy may have and still be learned from: $35,000 on a $50,000 account."""
    from futuresfund.objective import research_drawdown_cap

    return research_drawdown_cap(account_size() if size is None else float(size))


def profit_target(size: float | None = None) -> float:
    """The profit target set on headquarters."""
    from futuresfund.objective import profit_target as target

    return target()


def meets_target(row: dict, size: float | None = None) -> bool:
    """Acceptable: hit the profit target with drawdown inside the one-year cap, at 4 or more trades a week."""
    if not isinstance(row, dict) or row.get("error") or row.get("fatal") or row.get("breached"):
        return False
    from futuresfund.objective import counts

    return counts(row)


def target_text(size: float | None = None) -> str:
    from futuresfund.objective import pace_text

    return (
        f"Creation target. Account ${account_size():,.0f}: profit target ${profit_target():,.0f} set on headquarters, "
        f"one-year drawdown no higher than ${drawdown_cap():,.0f}. "
        f"A created script is accepted when, on the 5-minute ES chart, it reaches the profit target inside that drawdown "
        f"while averaging {pace_text()} over the tested range. Fewer than 130 is too quiet and more than 600 is too many trades. Two or three trades is not a strategy. "
        "It is promoted when the same settings also stay inside that drawdown on the 2-minute "
        "and 15-minute ES charts. Among acceptable scripts the smaller drawdown wins; a smaller drawdown beats a larger profit. "
        "Trade count only breaks a tie."
    )


# Studies.


def studies(notes: Path | None = None) -> list[dict]:
    """One summary per finished or partly finished study, from the research notes."""
    store = notes or NOTES
    if not store.is_dir():
        return []
    rows = []
    for path in sorted(store.glob("*.json")):
        if path.name in SKIP or path.name.startswith("_"):
            continue
        summary = _summary(path)
        if summary is not None:
            rows.append(summary)
    return rows


def _summary(path: Path) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _cache.get(key)
    if cached and cached[0] == stamp:
        return dict(cached[1]) if cached[1] is not None else None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    summary = _summarize(path, data) if isinstance(data, dict) else None
    with _cache_lock:
        _cache[key] = (stamp, summary)
    return dict(summary) if summary is not None else None


def _summarize(path: Path, data: dict) -> dict | None:
    charts = []
    trials: list[dict] = []
    instruments = data.get("instruments") if isinstance(data.get("instruments"), dict) else {}
    for root, record in instruments.items():
        if not isinstance(record, dict):
            continue
        per_chart = record.get("charts") if isinstance(record.get("charts"), dict) else {}
        if per_chart:
            for timeframe, chart in per_chart.items():
                if not isinstance(chart, dict):
                    continue
                row = _chart_row(str(root), str(timeframe), chart.get("best"))
                if row:
                    charts.append(row)
                trials.extend(item for item in chart.get("trials") or [] if isinstance(item, dict))
        else:
            row = _chart_row(str(root), str((record.get("best") or {}).get("timeframe") or "5m"), record.get("best"))
            if row:
                charts.append(row)
            trials.extend(item for item in record.get("trials") or [] if isinstance(item, dict))
    if not charts:
        row = _chart_row("ES", str((data.get("best") or {}).get("timeframe") or "5m"), data.get("best"))
        if row:
            charts.append(row)
        trials.extend(item for item in data.get("trials") or [] if isinstance(item, dict))
    if not charts:
        return None
    limit = trailing_limit()
    tested = [row for row in charts if row["trades"] >= CHART_TRADES]
    green = [row for row in tested if row["profit"] > 0 and row["profit"] > row["drawdown"]]
    safe = [row for row in green if row["drawdown"] <= limit]
    losing = [row for row in tested if row["profit"] < 0]
    total_profit = sum(row["profit"] for row in tested)
    total_drawdown = sum(row["drawdown"] for row in tested)
    title = str(data.get("title") or "").strip()
    if not title or title.lower().startswith("untitled"):
        title = path.stem.replace("_", " ")
    file = str(data.get("file") or f"{path.stem}.pine")
    pool = tested or charts
    best = max(pool, key=chart_rank)
    steadiest = max(safe, key=chart_rank) if safe else None
    source = read_source(file, path.stem)
    return {
        "id": path.stem,
        "title": title,
        "file": file,
        "attempts": int(data.get("attempts") or 0),
        "charts": charts,
        "tested": len(tested),
        "green": len(green),
        "safe_green": len(safe),
        "losing": len(losing),
        "worst_drawdown": max((row["drawdown"] for row in tested), default=0.0),
        "total_profit": round(total_profit, 2),
        "total_drawdown": round(total_drawdown, 2),
        "efficiency": round(total_profit / max(total_drawdown, 1.0), 2),
        "trades": sum(row["trades"] for row in tested),
        "best": best,
        "steadiest": steadiest,
        "indicators": indicator_names(source) if source else [],
        "helped": _helped(trials),
        "hurt": _hurt(trials),
        "has_source": bool(source),
    }


def chart_rank(row: dict) -> tuple:
    """A chart row under the research objective: the target at pace, lowest drawdown, then profit."""
    from futuresfund.objective import rank

    return rank(_measured(row))


def _chart_row(root: str, timeframe: str, best) -> dict | None:
    if not isinstance(best, dict) or best.get("error") or best.get("fatal"):
        return None
    runner = str(best.get("engine") or best.get("runner") or "")
    if runner and runner != "backtrader":
        return None
    try:
        profit = float(best.get("net_profit"))
        drawdown = float(best.get("max_drawdown") or 0)
        trades = int(best.get("trades") or 0)
    except (TypeError, ValueError):
        return None
    return {
        "instrument": root.upper(),
        "timeframe": timeframe,
        "profit": round(profit, 2),
        "drawdown": round(drawdown, 2),
        "trades": trades,
        "days": best.get("days"),
        "params": best.get("params") if isinstance(best.get("params"), dict) else {},
    }


def _measured(row: dict) -> dict:
    """A chart row as the objective reads it."""
    return {
        "net_profit": row.get("profit"),
        "max_drawdown": row.get("drawdown"),
        "trades": row.get("trades"),
        "days": row.get("days"),
        "instrument": row.get("instrument"),
        "timeframe": row.get("timeframe"),
    }


def _helped(trials: list[dict]) -> list[str]:
    """Changes that cut the drawdown while the result stayed profitable."""
    found = []
    previous = None
    for row in trials:
        try:
            profit = float(row.get("net_profit"))
            drawdown = float(row.get("max_drawdown") or 0)
        except (TypeError, ValueError):
            previous = row
            continue
        change = str(row.get("change") or "")
        if previous is not None and change and change not in {"Starting settings", "Same settings"}:
            try:
                before_dd = float(previous.get("max_drawdown") or 0)
                before_profit = float(previous.get("net_profit"))
            except (TypeError, ValueError):
                before_dd, before_profit = drawdown, profit
            if profit > 0 and drawdown < before_dd and profit >= before_profit * 0.8:
                found.append((before_dd - drawdown, f"{change} (drawdown {before_dd:,.0f} to {drawdown:,.0f}, profit {profit:,.0f})"))
        previous = row
    found.sort(key=lambda item: -item[0])
    seen: list[str] = []
    for _, text in found:
        name = text.split(" ", 1)[0]
        if any(item.startswith(name + " ") for item in seen):
            continue
        seen.append(text)
        if len(seen) == 2:
            break
    return seen


def _hurt(trials: list[dict]) -> list[str]:
    """Changes that made the drawdown worse."""
    found = []
    previous = None
    for row in trials:
        try:
            drawdown = float(row.get("max_drawdown") or 0)
        except (TypeError, ValueError):
            previous = row
            continue
        change = str(row.get("change") or "")
        if previous is not None and change and change not in {"Starting settings", "Same settings"}:
            try:
                before_dd = float(previous.get("max_drawdown") or 0)
            except (TypeError, ValueError):
                before_dd = drawdown
            if drawdown > before_dd * 1.5 and drawdown - before_dd > 200:
                found.append((drawdown - before_dd, f"{change} (drawdown {before_dd:,.0f} to {drawdown:,.0f})"))
        previous = row
    found.sort(key=lambda item: -item[0])
    return [text for _, text in found[:1]]


def read_source(file: str, stem: str = "") -> str:
    """The Pine source of a study, wherever the script is filed now."""
    names = []
    name = Path(file).name if file else ""
    if name:
        names.append(name)
        if not name.endswith(".pine"):
            names.append(f"{Path(name).stem}.pine")
    if stem:
        names.append(f"{stem}.pine")
        names.append(f"{stem.replace('_', ' ')}.pine")
    for folder in FOLDERS:
        for candidate in names:
            path = ROOT / folder / candidate
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
    return ""


def indicator_names(source: str) -> list[str]:
    try:
        from futuresfund.indicators import scan_script

        rows = scan_script("study", source)
    except Exception:
        rows = []
    names = []
    for row in rows:
        name = str(row.get("name") or "")
        if name and name not in names:
            names.append(name)
    return names[:8]


# Ranking.


def exemplars(limit: int = 6, notes: Path | None = None) -> list[dict]:
    """Studies whose best chart hit the profit target inside the trailing drawdown with enough trades, lowest drawdown first."""
    from futuresfund.objective import counts

    rows = []
    for row in studies(notes):
        best = row["best"]
        if counts(_measured(best)):
            rows.append(row)
    rows.sort(key=lambda row: chart_rank(row["best"]), reverse=True)
    return rows[:limit]


def cautions(limit: int = 4, notes: Path | None = None) -> list[dict]:
    """Studies that lost money or drew down past the trailing limit."""
    cap = drawdown_cap()
    rows = [row for row in studies(notes) if row["losing"] >= 1 or row["worst_drawdown"] > cap]
    rows.sort(key=lambda row: (row["worst_drawdown"], row["losing"]), reverse=True)
    return rows[:limit]


# Text for the strategy developer.


def entry_excerpt(source: str, limit: int = 700) -> str:
    """The lines of a script that decide the entry and the exit, with the conditions the entry reads."""
    lines = (source or "").splitlines()
    keep: set[int] = set()
    conditions: set[int] = set()
    for index, line in enumerate(lines):
        if "strategy.entry" in line or "strategy.exit" in line or "strategy.close(" in line:
            keep.add(index)
            if "strategy.entry" not in line:
                continue
            back = index - 1
            while back >= 0 and back >= index - 6:
                stripped = lines[back].strip()
                if stripped.startswith("if ") or stripped.startswith("if(") or stripped.startswith("else if"):
                    keep.add(back)
                    conditions.add(back)
                    break
                back -= 1
    words = set()
    for index in conditions:
        words.update(re.findall(r"\b[A-Za-z_]\w*\b", lines[index]))
    words -= {"if", "and", "or", "not", "strategy", "position_size", "na"}
    for index, line in enumerate(lines):
        match = re.match(r"\s*(?:var\s+)?(?:bool\s+|float\s+|int\s+)?([A-Za-z_]\w*)\s*(?::=|=)(?!=)", line)
        if not match or match.group(1) not in words:
            continue
        if "input." in line or "strategy(" in line or line.strip().startswith("for "):
            continue
        keep.add(index)
    text = "\n".join(lines[index].rstrip() for index in sorted(keep))
    if len(text) > limit:
        text = text[: limit - 4].rstrip() + "\n..."
    return text


def describe(row: dict, *, with_entry: bool = False, excerpt_limit: int = 600) -> str:
    best = row["best"]
    steady = row.get("steadiest")
    parts = [
        f"{row['title']} ({row['file']}): best chart {best['instrument']} {best['timeframe']} drawdown {best['drawdown']:,.0f}, "
        f"profit {best['profit']:,.0f}, {best['trades']} trades; green on {row['safe_green']} of {row['tested']} charts inside the drawdown limit; "
        f"profit {row['total_profit']:,.0f} against drawdown {row['total_drawdown']:,.0f} across those charts ({row['efficiency']:.1f} to 1)."
    ]
    if steady and steady is not best:
        parts.append(
            f"Steadiest chart {steady['instrument']} {steady['timeframe']}: drawdown {steady['drawdown']:,.0f}, profit {steady['profit']:,.0f}, {steady['trades']} trades."
        )
    if row.get("indicators"):
        parts.append("Indicators: " + ", ".join(row["indicators"]) + ".")
    if row.get("helped"):
        parts.append("Changes that cut the drawdown: " + "; ".join(row["helped"]) + ".")
    if row.get("hurt"):
        parts.append("A change that hurt: " + "; ".join(row["hurt"]) + ".")
    text = " ".join(parts)
    if with_entry:
        source = read_source(row["file"], row["id"])
        excerpt = entry_excerpt(source, excerpt_limit) if source else ""
        if excerpt:
            text += f"\nEntry and exit as written:\n{excerpt}"
    return text


def caution_text(row: dict) -> str:
    worst = max(row["charts"], key=lambda chart: chart["drawdown"])
    losses = [chart for chart in row["charts"] if chart["profit"] < 0 and chart["trades"] >= CHART_TRADES]
    text = f"{row['title']}: drawdown {worst['drawdown']:,.0f} on {worst['instrument']} {worst['timeframe']} ({worst['trades']} trades)"
    if losses:
        text += f"; lost money on {len(losses)} of {row['tested']} charts"
    if row.get("hurt"):
        text += f"; {row['hurt'][0]}"
    return text + "."


def brief(limit: int = 5200, *, exemplar_count: int = 5, caution_count: int = 3, excerpts: int = 2, notes: Path | None = None) -> str:
    """What the strategy developer reads before writing: the target, what worked, what did not."""
    from futuresfund.objective import describe as objective_text

    lines = [target_text(), objective_text(), ""]
    from futuresfund.objective import goal_line, pace_text

    top = exemplars(exemplar_count, notes)
    if top:
        lines.append(
            f"What has worked. These finished studies hit the ${profit_target():,.0f} target inside the ${trailing_limit():,.0f} drawdown "
            f"at {pace_text()}. {goal_line()} The take profit is larger than the stop. Build from their ideas:"
        )
        for number, row in enumerate(top, start=1):
            lines.append(f"{number}. " + describe(row, with_entry=number <= excerpts))
    else:
        lines.append(
            f"No finished study has hit the ${profit_target():,.0f} target inside the ${trailing_limit():,.0f} drawdown at {pace_text()} yet. "
            "Build from the library, keep the stop tight, and take profits at a size the chart reaches most days, so the script trades every day."
        )
    bad = cautions(caution_count, notes)
    if bad:
        lines.append("")
        lines.append("What has not worked. Do not repeat these without a tighter stop or a filter:")
        for row in bad:
            lines.append("- " + caution_text(row))
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 4].rstrip() + "\n..."
    return text
