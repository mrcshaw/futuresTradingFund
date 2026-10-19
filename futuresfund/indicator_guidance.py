"""The research method the user edits.

The chart researcher, the strategy developers, and the Under the hood page read
futuresfund/researchNotes/how_to_research.md. That file is the guideline.
"""

from __future__ import annotations

from pathlib import Path

GUIDE_PATH = Path(__file__).resolve().parent / "researchNotes" / "how_to_research.md"

_FALLBACK = (
    "Read the script before changing a number. Say what this script is trying to do, "
    "then change the condition in this script that is keeping bars from entering. "
    "A lesson from a different script is not a reason to change this one. "
    "A result under 2 trades a week is far under the floor of 4. "
    "The stop and the take profit do not create the entry."
)


def price_guidance() -> str:
    """The guideline file, read each time so an edit is what the desks see next."""
    try:
        text = GUIDE_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""
    body = text.strip()
    return body or _FALLBACK


def save_guidance(text: str) -> str:
    """Write the research guide. The next agent decision reads this text."""
    body = str(text or "").replace("\r\n", "\n").strip()
    if not body:
        raise ValueError("The research guide is empty.")
    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = GUIDE_PATH.with_suffix(".md.tmp")
    temporary.write_text(body + "\n", encoding="utf-8")
    temporary.replace(GUIDE_PATH)
    return price_guidance()
