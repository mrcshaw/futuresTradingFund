"""Learning-folder files the worker loads, one at a time."""

from __future__ import annotations

import shutil
import time
from pathlib import Path


LEARNING = Path(__file__).resolve().parents[2] / "futuresfund" / "learningStrategies"
_SUFFIXES = {".pine", ".txt"}
_ROOTS = ("MNQ", "MES", "GOLD", "NQ", "ES", "GC")


def learning_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    found = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in _SUFFIXES]
    return sorted(found, key=lambda path: path.name.lower())


def instrument_of(name: str) -> str:
    text = name.upper()
    for root in _ROOTS:
        if root in text:
            return root
    return "ES"


def place_file(path: Path, passed: bool) -> Path:
    """Move a finished learning file beside the learning folder."""
    folder = path.parent.parent / ("completed strategies" if passed else "did not produce profits")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    if target.exists():
        target = folder / f"{path.stem}-{time.time_ns()}{path.suffix}"
    shutil.move(str(path), str(target))
    return target
