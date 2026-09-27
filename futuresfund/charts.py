"""ES chart exports. The live book stays on the 15-minute file. Research also reads 2-minute and 5-minute files."""

from __future__ import annotations

from pathlib import Path

from futuresfund.config import ROOT
from futuresfund.csv_bars import parse_tradingview_csv

# The token is the comma-space prefix in the TradingView file name, so 15 does not match 5 or 2.
_FRAMES = (("15m", ", 15_"), ("5m", ", 5_"), ("2m", ", 2_"))


def chart_paths() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in ROOT.glob("CME_MINI_DL_ES1*.csv"):
        for frame, token in _FRAMES:
            if token in path.name:
                found[frame] = path
                break
    return found


def load_chart(timeframe: str) -> list[dict]:
    path = chart_paths().get(timeframe)
    if path is None:
        return []
    return parse_tradingview_csv(path.read_text(encoding="utf-8-sig", errors="replace"))
