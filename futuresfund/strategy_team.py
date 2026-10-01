"""The strategy creation team.

The card keeper reads measured test changes. The strategy developer writes
one new Pine script from those cards. The script checker rejects a script
that has no entry or exit. The creation tester runs it once on engine 4.
The chart researcher then leaves it for the three chart developers. The
lesson writer puts the finished changes back on the cards.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from futuresfund.learn import LEARNING, NOTES, RESEARCH_CHARTS
from futuresfund.roster import CHART_RESEARCHER

CARD_KEEPER = "Card keeper"
STRATEGY_DEVELOPER = "Strategy developer"
SCRIPT_CHECKER = "Script checker"
CREATION_TESTER = "Creation tester"
LESSON_WRITER = "Lesson writer"
CREATION_ENGINE = 3
CARDS_PATH = NOTES / "strategy_cards.json"
_CREATED = "created_"


def build_cards(learning: Path | None = None, notes: Path | None = None) -> list[dict]:
    """One card per Pine file. A card carries the measured one-variable changes."""
    folder = learning or LEARNING
    store = notes or NOTES
    cards = []
    if not folder.is_dir():
        return cards
    for path in sorted(folder.glob("*.pine")):
        if not path.is_file() or path.name.startswith(_CREATED):
            continue
        record = _note_for(store, path)
        changes = _changes(record)
        cards.append({
            "file": path.name,
            "title": record.get("title") or path.stem,
            "changes": changes,
        })
    return cards


def measured_cards(cards: list[dict]) -> list[dict]:
    """Cards that already have a tested change."""
    return [card for card in cards if card.get("changes")]


def write_cards(cards: list[dict], path: Path | None = None) -> Path:
    target = path or CARDS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"cards": cards}, indent=2), encoding="utf-8")
    return target


def extract_pine(text: str) -> str:
    """The Pine script inside a model reply. The version line stays first."""
    body = text or ""
    fenced = re.findall(r"```(?:pine)?\s*([\s\S]*?)```", body, flags=re.IGNORECASE)
    for block in fenced:
        if "//@version" in block or "strategy(" in block:
            body = block
            break
    start = body.find("//@version")
    if start < 0:
        start = body.find("strategy(")
    if start < 0:
        return ""
    return body[start:].strip()


def pine_problems(source: str) -> list[str]:
    """What is missing before a new script can reach the engine."""
    text = source or ""
    problems = []
    if "//@version" not in text:
        problems.append("The version line is missing.")
    if "strategy(" not in text:
        problems.append("strategy() is missing.")
    if "strategy.entry" not in text and "strategy.order" not in text:
        problems.append("An entry is missing.")
    if "strategy.close" not in text and "strategy.exit" not in text:
        problems.append("An exit is missing.")
    return problems


def baseline_ready(rows: list[dict]) -> bool:
    """A first pass is usable when one chart produces a trade list and none fail to compile."""
    if not rows:
        return False
    if any(row.get("fatal") or row.get("error") for row in rows):
        return False
    return any(int(row.get("trades") or 0) >= 1 for row in rows)


def script_name(title: str, folder: Path | None = None) -> str:
    """A new file name that does not replace a script already in the folder."""
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "strategy").lower()).strip("_")[:40] or "strategy"
    folder = folder or LEARNING
    name = f"{_CREATED}{slug}.pine"
    stem = f"{_CREATED}{slug}"
    number = 2
    while (folder / name).exists():
        name = f"{stem}_{number}.pine"
        number += 1
    return name


def prompt_for(cards: list[dict]) -> str:
    """Ask for one new script. The cards are the only results the model may use."""
    lines = [
        "Write one new Pine strategy from these cards.",
        "Use one idea from the cards. Say which card it came from, then the script.",
        "The first line is //@version=5.",
        "Include strategy(), an entry, and an exit or a close.",
        "Do not invent a profit, a drawdown, or a trade count.",
        "",
    ]
    for card in measured_cards(cards)[:4]:
        lines.append(f"Card: {card['title']} ({card['file']})")
        for change in card["changes"][:3]:
            lines.append(
                f"- {change['change']} Profit {change['net_profit']}, "
                f"drawdown {change['max_drawdown']}, trades {change['trades']}. {change['impact']}"
            )
        lines.append("")
    return "\n".join(lines).strip()


def run_creation(board, pause) -> None:
    """Keep writing one script at a time while research is running."""
    from futuresfund.learn import _load_frames, _sleep, _wait

    while not board.cancel.is_set():
        if not _wait(board, pause):
            return
        cards = build_cards()
        write_cards(cards)
        ready = measured_cards(cards)
        board.post(
            CARD_KEEPER,
            f"{len(cards)} scripts are in the learning folder. {len(ready)} have a measured change.",
            kind="log",
            channel=CARD_KEEPER,
        )
        if not ready:
            board.post(
                CARD_KEEPER,
                "No measured change is on a card yet. The strategy developer waits until a chart study records one.",
                kind="report",
                channel=CARD_KEEPER,
            )
            if not _sleep(board, pause, 60):
                return
            continue
        waiting = _created_waiting()
        if waiting:
            board.post(
                CHART_RESEARCHER,
                f"{waiting.name} is still in the learning folder. The chart developers are testing it on the 2-minute, 5-minute, and 15-minute charts.",
                kind="log",
                channel=CHART_RESEARCHER,
            )
            if not _sleep(board, pause, 60):
                return
            continue
        reply = _ask(board, STRATEGY_DEVELOPER, "Writing one Pine script from the cards", prompt_for(ready))
        source = extract_pine(reply)
        problems = pine_problems(source)
        if problems:
            board.post(
                SCRIPT_CHECKER,
                "The script was sent back. " + " ".join(problems),
                kind="report",
                channel=SCRIPT_CHECKER,
            )
            if not _sleep(board, pause, 60):
                return
            continue
        board.post(
            SCRIPT_CHECKER,
            "The script has a version line, an entry, and an exit. Engine 4 can run it once.",
            kind="log",
            channel=SCRIPT_CHECKER,
        )
        rows = _baseline(board, source, _load_frames("ES"))
        if board.cancel.is_set():
            return
        if not baseline_ready(rows):
            board.post(
                CREATION_TESTER,
                "Engine 4 did not return a trade list. The script stays out of the learning folder.",
                kind="report",
                channel=CREATION_TESTER,
            )
            if not _sleep(board, pause, 60):
                return
            continue
        title = _title_from(source)
        name = script_name(title)
        LEARNING.mkdir(parents=True, exist_ok=True)
        (LEARNING / name).write_text(source if source.endswith("\n") else source + "\n", encoding="utf-8")
        summary = _baseline_summary(rows)
        board.post(
            CREATION_TESTER,
            f"Engine 4 finished {name} at the original settings. {summary}",
            kind="report",
            channel=CREATION_TESTER,
        )
        board.post(
            CHART_RESEARCHER,
            f"{name} is in the learning folder. The 2-minute, 5-minute, and 15-minute developers will each run 120 different results.",
            kind="report",
            channel=CHART_RESEARCHER,
        )
        refreshed = build_cards()
        write_cards(refreshed)
        board.post(
            LESSON_WRITER,
            f"The cards now include {len(measured_cards(refreshed))} scripts with a measured change.",
            kind="log",
            channel=LESSON_WRITER,
        )
        if not _sleep(board, pause, 60):
            return


def _created_waiting() -> Path | None:
    if not LEARNING.is_dir():
        return None
    found = sorted(path for path in LEARNING.glob(f"{_CREATED}*.pine") if path.is_file())
    return found[0] if found else None


def _note_for(store: Path, path: Path) -> dict:
    file = store / f"{path.stem.replace(' ', '_')}.json"
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _changes(record: dict) -> list[dict]:
    rows = []
    seen = set()
    for trial in _trials(record):
        change = str(trial.get("change") or "").strip()
        if not change or change in {"Original", "Starting settings"} or change in seen:
            continue
        if trial.get("error") or trial.get("fatal"):
            continue
        seen.add(change)
        rows.append({
            "change": change,
            "impact": str(trial.get("impact") or "").strip(),
            "net_profit": trial.get("net_profit"),
            "max_drawdown": trial.get("max_drawdown"),
            "trades": trial.get("trades"),
        })
    return rows


def _trials(record: dict) -> list[dict]:
    found = []
    instruments = record.get("instruments") if isinstance(record.get("instruments"), dict) else {}
    for item in instruments.values():
        if not isinstance(item, dict):
            continue
        charts = item.get("charts") if isinstance(item.get("charts"), dict) else {}
        if charts:
            for chart in charts.values():
                if isinstance(chart, dict):
                    found.extend(row for row in chart.get("trials") or [] if isinstance(row, dict))
        else:
            found.extend(row for row in item.get("trials") or [] if isinstance(row, dict))
    if not found:
        found.extend(row for row in record.get("trials") or [] if isinstance(row, dict))
    return found


def _ask(board, agent: str, activity: str, prompt: str) -> str:
    from futuresfund.crew import get_crew
    from futuresfund.llm import complete

    def work():
        return complete(prompt, agent)

    reply = get_crew(board).run(agent, activity, work)
    return reply or ""


def _baseline(board, source: str, frames: dict) -> list[dict]:
    from futuresfund.pineforge_engine import run_script

    rows = []
    cancel = None if board is None else board.cancel
    for name in RESEARCH_CHARTS:
        if cancel is not None and cancel.is_set():
            break
        bars = frames.get(name) or []
        if len(bars) < 80:
            rows.append({"timeframe": name, "error": f"The {name} chart is not loaded.", "fatal": True, "trades": 0})
            continue
        board.post(
            CREATION_TESTER,
            f"Running the new script once on the {name} chart.",
            kind="log",
            channel=CREATION_TESTER,
        )
        measured = run_script(source, bars, name, None, cancel, "ES", "new script", CREATION_ENGINE) or {}
        measured["timeframe"] = name
        rows.append(measured)
    return rows


def _baseline_summary(rows: list[dict]) -> str:
    parts = []
    for row in rows:
        parts.append(
            f"{row.get('timeframe')} profit {row.get('net_profit')}, "
            f"drawdown {row.get('max_drawdown')}, trades {row.get('trades')}"
        )
    return "; ".join(parts)


def _title_from(source: str) -> str:
    match = re.search(r'strategy\s*\(\s*"([^"]+)"', source or "")
    if match:
        return match.group(1)
    return "strategy"
