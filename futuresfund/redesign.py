"""A researcher's redesign replaces the script it answers, in place.

No new script is filed. The redesign is measured at the same settings as the script it replaces
and kept only when it does better; otherwise the script is put back (learn._install_checked).
The ledger is researchNotes/redesigns/ledger.json. It keeps the plan behind each one, so the next
redesign of that script does not repeat it. Rows from before 2026-11-05 name the separate
_redesignN scripts the desk used to file; they still count as ideas tried, not against LIMIT.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

LIMIT = 3  # in-place redesigns of one script
_LOCK = threading.Lock()
_MARK = re.compile(r"_redesign\d+$", re.IGNORECASE)
_TITLE = re.compile(r'(\bstrategy\(\s*(?:title\s*=\s*)?)"([^"\n]*)"')


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
    """True while this script has an in-place redesign left."""
    with _LOCK:
        rows = _load().get(original_of(path)) or []
    return sum(1 for row in rows if row.get("in_place")) < LIMIT


def keep_title(source: str, parent: str) -> str:
    """The redesign keeps the title of the script it replaces, so tables and charts still name it."""
    found = _TITLE.search(parent or "")
    if not found or not found.group(2).strip():
        return source
    return _TITLE.sub(lambda match: f'{match.group(1)}"{found.group(2)}"', source, count=1)


def note_redesign(path: Path, because: str, plan: str = "") -> bool:
    """Enter an in-place redesign on the ledger. False when this script has none left."""
    original = original_of(path)
    with _LOCK:
        data = _load()
        rows = list(data.get(original) or [])
        if sum(1 for row in rows if row.get("in_place")) >= LIMIT:
            return False
        rows.append({
            "file": Path(path).name,
            "in_place": True,
            "because": because,
            "plan": plan,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        data[original] = rows
        _write(data)
    return True


def earlier_lines(path: Path) -> list[str]:
    """What was already tried, for the researcher."""
    with _LOCK:
        rows = _load().get(original_of(path)) or []
    return [
        f"{row.get('file')} already tried this as a redesign, so do not repeat it: {row.get('because')} {row.get('plan') or ''}".strip()
        for row in rows
    ]
