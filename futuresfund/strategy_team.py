"""The strategy creation team.

The card keeper reads measured test changes. The strategy developer writes
one new Pine script from those cards. The script checker rejects a script
that has no entry or exit. The creation tester runs the new script on engine 4 for up to 200 attempts
and stops when it is profitable. The script is saved in createdStrategies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from futuresfund.learn import LEARNING, NOTES

CREATED = LEARNING.parent / "createdStrategies"

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
    folder = folder or CREATED
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


SEEN_PATH = NOTES / "creation_sources.json"


def finished_chart_studies(learning: Path | None = None, notes: Path | None = None) -> list[str]:
    """Scripts whose 2-minute, 5-minute, and 15-minute studies are finished on one instrument."""
    from futuresfund.learn import RESEARCH_CHARTS, _chart_finished, research_instruments

    folder = learning or LEARNING
    store = notes or NOTES
    found = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.pine")):
        if not path.is_file() or path.name.startswith(_CREATED):
            continue
        record = _note_for(store, path)
        for root in research_instruments():
            if all(_chart_finished(record, root, timeframe) for timeframe in RESEARCH_CHARTS):
                found.append(path.name)
                break
    return found


def run_creation(board, pause) -> None:
    """Write one script after the three chart developers finish, then test it on engine 4."""
    from futuresfund.learn import (
        ACCOUNT_SIZE,
        ATTEMPT_LIMIT,
        _load_frames,
        _pine_params,
        _engine_execute,
        _search,
        _sleep,
        _wait,
        _with_contract_point,
        pine_facts,
    )

    while not board.cancel.is_set():
        if not _wait(board, pause):
            return
        cards = build_cards()
        write_cards(cards)
        source_name = _next_source()
        if source_name is None:
            board.post(
                STRATEGY_DEVELOPER,
                "Waiting until the 2-minute, 5-minute, and 15-minute developers finish a backtest.",
                kind="log",
                channel=STRATEGY_DEVELOPER,
            )
            if not _sleep(board, pause, 30):
                return
            continue
        ready = measured_cards(cards)
        board.post(
            CARD_KEEPER,
            f"The 2-minute, 5-minute, and 15-minute studies of {source_name} are finished. {len(ready)} cards have a measured change.",
            kind="report",
            channel=CARD_KEEPER,
        )
        if not ready:
            _remember_source(source_name)
            if not _sleep(board, pause, 30):
                return
            continue
        reply = _ask(
            board,
            STRATEGY_DEVELOPER,
            f"Writing one Pine script after {source_name}",
            prompt_for(ready),
        )
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
            "The script has a version line, an entry, and an exit. Engine 4 will run up to 200 attempts.",
            kind="log",
            channel=SCRIPT_CHECKER,
        )
        title = _title_from(source)
        name = _store_created(title, source)
        _remember_source(source_name)
        created = CREATED / name
        facts = pine_facts(source) if source else {"title": title, "inputs": [], "named": {}}
        frames = _load_frames("ES")
        board.post(
            CREATION_TESTER,
            f"Engine 4 is backtesting {name} on the 5-minute ES chart until it is profitable, up to {ATTEMPT_LIMIT} attempts.",
            kind="report",
            channel=CREATION_TESTER,
        )
        execute = _engine_execute(
            source, frames, facts, board, "ES", CREATION_TESTER, "5m", CREATION_ENGINE,
        )
        trials = _search(
            _with_contract_point(_pine_params(facts), "ES"),
            execute,
            ACCOUNT_SIZE,
            ATTEMPT_LIMIT,
            board,
            created,
            pause,
            facts,
            extras=False,
            developer=CREATION_TESTER,
            researcher=STRATEGY_DEVELOPER,
            until_profitable=True,
        )
        if board.cancel.is_set():
            return
        best = max(trials, key=lambda row: float(row.get("net_profit") or -10**12)) if trials else {}
        if any(_row_is_profitable(row) for row in trials):
            board.post(
                CREATION_TESTER,
                f"{name} is profitable on engine 4. Profit {best.get('net_profit')}, drawdown {best.get('max_drawdown')}, trades {best.get('trades')}. It stays in createdStrategies.",
                kind="report",
                channel=CREATION_TESTER,
            )
        else:
            board.post(
                CREATION_TESTER,
                f"{name} used {len(trials)} of {ATTEMPT_LIMIT} attempts and was not profitable. Profit {best.get('net_profit')}, trades {best.get('trades')}. It stays in createdStrategies.",
                kind="report",
                channel=CREATION_TESTER,
            )
        board.post(
            LESSON_WRITER,
            f"Engine 4 finished {name}. The next script waits for the next completed 2-minute, 5-minute, and 15-minute backtest.",
            kind="log",
            channel=LESSON_WRITER,
        )


def _row_is_profitable(row: dict) -> bool:
    from futuresfund.learn import _row_profitable

    return _row_profitable(row)


def _next_source() -> str | None:
    seen = _seen_sources()
    for name in finished_chart_studies():
        if name not in seen:
            return name
    return None


def _seen_sources() -> set[str]:
    if not SEEN_PATH.is_file():
        return set()
    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    found = data.get("seen") if isinstance(data, dict) else None
    return {str(item) for item in found} if isinstance(found, list) else set()


def _remember_source(name: str) -> None:
    seen = _seen_sources()
    seen.add(name)
    NOTES.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps({"seen": sorted(seen)}, indent=2), encoding="utf-8")


def _store_created(title: str, source: str) -> str:
    """Save a new script beside the learning folder. The chart studies do not pick it up."""
    CREATED.mkdir(parents=True, exist_ok=True)
    name = script_name(title, CREATED)
    text = source if source.endswith("\n") else source + "\n"
    (CREATED / name).write_text(text, encoding="utf-8")
    return name


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


def _title_from(source: str) -> str:
    match = re.search(r'strategy\s*\(\s*"([^"]+)"', source or "")
    if match:
        return match.group(1)
    return "strategy"
