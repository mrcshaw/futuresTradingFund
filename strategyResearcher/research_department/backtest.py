"""One PineTS run for one engine. The futures desk queue is not involved."""

from __future__ import annotations

import sys
from pathlib import Path

from research_department.charts import cut_sessions, load_sessions, symbol_of
from research_department.measure import measure
from research_department.stages import Day


FUND = Path(__file__).resolve().parents[2]
if str(FUND) not in sys.path:
    sys.path.insert(0, str(FUND))

from futuresfund.pinets_engine import PineTSError, run_pinets


def run_window(source: str, instrument: str, timeframe: str, count: int, *, sample: bool = False) -> list[Day]:
    bars, dates = (cut_sessions if sample else load_sessions)(instrument, count, timeframe)
    trades = run_pinets(source, bars, timeframe, symbol_of(instrument))
    return measure(trades, dates)


__all__ = ["PineTSError", "run_window"]
