"""Futures roots. The desks analyze the Yahoo continuous contract. CrossTrade gets the instrument name from the alert."""

from __future__ import annotations

import re

# Longer roots first so MES matches before ES, and MNQ before NQ.
ROOTS = (
    "MNQ", "MES", "MYM", "M2K", "MCL", "MGC", "MBT", "SIL",
    "NQ", "ES", "YM", "RTY", "CL", "NG", "QG", "GC", "SI", "HG",
    "ZB", "ZN", "ZF", "ZC", "ZS", "ZW",
    "6E", "6J", "6B", "6A", "6C",
    "BTC", "ETH",
)

# Minimum price increment. One tick of slippage on ES is 0.25 points, which is $12.50.
TICK_SIZE = {
    "ES": 0.25, "MES": 0.25, "NQ": 0.25, "MNQ": 0.25, "YM": 1, "MYM": 1,
    "RTY": 0.1, "M2K": 0.1, "CL": 0.01, "MCL": 0.01, "GC": 0.1, "MGC": 0.1,
}

# Dollar value of one point, for the book display only.
POINT_VALUE = {
    "ES": 50, "MES": 5, "NQ": 20, "MNQ": 2, "YM": 5, "MYM": 0.5,
    "RTY": 50, "M2K": 5, "CL": 1000, "MCL": 10, "NG": 10000, "QG": 2500,
    "GC": 100, "MGC": 10, "SI": 5000, "SIL": 1000, "HG": 25000,
    "ZB": 1000, "ZN": 1000, "ZF": 1000,
    "ZC": 50, "ZS": 50, "ZW": 50,
    "6E": 125000, "6J": 12500000, "6B": 62500, "6A": 100000, "6C": 100000,
    "BTC": 5, "MBT": 0.1, "ETH": 50,
}

_SAFE_INSTRUMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .!/_\-]{0,40}$")


def root_of(instrument: str) -> str:
    text = (instrument or "").upper().strip()
    if ":" in text:
        text = text.split(":")[-1]
    text = text.replace("=F", "").replace("1!", "").strip()
    token = text.split()[0] if text else ""
    for root in ROOTS:
        if token.startswith(root):
            return root
    raise ValueError(f"{instrument!r} is not a futures root this desk knows.")


def yahoo_symbol(instrument: str) -> str:
    return f"{root_of(instrument)}=F"


def tick_size(instrument: str) -> float:
    root = root_of(instrument)
    try:
        return TICK_SIZE[root]
    except KeyError as exc:
        raise ValueError(f"{root} has no tick size on this desk.") from exc


def check_instrument(instrument: str) -> str:
    cleaned = " ".join((instrument or "").strip().split())
    if not _SAFE_INSTRUMENT.fullmatch(cleaned):
        raise ValueError("Instrument contains characters CrossTrade would misread.")
    root_of(cleaned)
    return cleaned


def crosstrade_name(instrument: str) -> str:
    """A continuous name when the caller typed only the root."""
    cleaned = check_instrument(instrument)
    if cleaned.upper() in ROOTS:
        return f"{cleaned.upper()}1!"
    return cleaned
