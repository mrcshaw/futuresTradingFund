"""A researcher's redesign is a new script. The script it came from stays as it was.

It goes in the learning folder under its own name and title, so it gets its own search, its own
stages, and its own notes, and it is judged only after that search has tuned it. The ledger is
researchNotes/redesigns/ledger.json. It keeps the plan behind each one, so the next redesign of
that script does not repeat it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

LIMIT = 3  # redesigns of one original script, counting redesigns of its redesigns
_LOCK = threading.Lock()
_MARK = re.compile(r"_redesign\d+$", re.IGNORECASE)
_TITLE = re.compile(r'(\bstrategy\(\s*(?:title\s*=\s*)?)"([^"\n]*)"')
_TITLE_MARK = re.compile(r"\s+Redesign \d+$")


def _ledger() -> Path:
    from futuresfund import learn

    return Path(learn.NOTES) / "redesigns" / "ledger.json"


def _load() -> dict:
    try:
        data = json.loads(_ledger().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _ledger()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def is_redesign(path: Path) -> bool:
    return bool(_MARK.search(Path(path).stem))


def original_of(path: Path) -> str:
    """The name of the script a whole line of redesigns came from."""
    return _MARK.sub("", Path(path).stem)


def room(path: Path) -> bool:
    """True while this script's original has a redesign left."""
    with _LOCK:
        return len(_load().get(original_of(path)) or []) < LIMIT


def earlier_lines(path: Path) -> list[str]:
    """What was already tried, for the researcher."""
    with _LOCK:
        rows = _load().get(original_of(path)) or []
    return [f"{row.get('file')} already tried this as a redesign, so do not repeat it: {row.get('because')}" for row in rows]


def _titled(source: str, parent: str, original: str, number: int) -> str:
    """The new script's own title, so a table or a chart can never mistake it for the original."""
    found = _TITLE.search(parent or "")
    base = _TITLE_MARK.sub("", found.group(2)) if found and found.group(2).strip() else original
    return _TITLE.sub(lambda match: f'{match.group(1)}"{base} Redesign {number}"', source, count=1)


def file_redesign(path: Path, parent: str, source: str, because: str, plan: str = "") -> Path | None:
    """Write the redesign as a new script and enter it on the ledger. None when the original has no redesign left."""
    from futuresfund import learn
    from futuresfund.attempt_reason import keep_version

    original = original_of(path)
    with _LOCK:
        data = _load()
        rows = list(data.get(original) or [])
        if len(rows) >= LIMIT:
            return None
        number = len(rows) + 1
        target = Path(learn.LEARNING) / f"{original}_redesign{number}.pine"
        text = _titled(source, parent, original, number)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        rows.append({
            "file": target.name,
            "from": Path(path).name,
            "from_sha": keep_version(target, parent),
            "source_sha": keep_version(target, text),
            "because": because,
            "plan": plan,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        data[original] = rows
        _write(data)
    return target
