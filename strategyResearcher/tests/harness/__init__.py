"""Scripted proposals and days. No model, no PineTS, no chart files."""

from research_department.meeting import Proposal
from research_department.stages import Day


PINE = """//@version=5
strategy("5-Candle Breakout", overlay=true)
watch_bars = input.int(4, "Max Bars")
doji_threshold = input.float(0.25, "Doji")
"""


def proposal(variable="watch_bars", value=6, direction="up", consistent=True, win=True, justification=None) -> Proposal:
    text = justification
    if text is None:
        text = f"Raise {variable} for a more consistent $500 day and a higher win percentage."
    return Proposal(variable, value, direction, consistent, win, text)


def three(consistent=True, win=True) -> list[Proposal]:
    return [
        proposal("watch_bars", 6, "up", consistent, win),
        proposal("doji_threshold", 0.1, "down", consistent, win),
        proposal("ema_min_angle", 5, "up", consistent, win),
    ]


def days(count, profit=500, worst_loss=250, wins=1, trades=2) -> list[Day]:
    return [Day(profit, worst_loss, wins, trades) for _ in range(count)]
