"""
Night Shift Delta Volume Breakout — same logic as profitable day version.
Session 18:00–8:00 ET. Pivot confirmation, disarm_only_inside_poc.
"""

from strategies.delta_volume_breakout_strategy import DeltaVolumeBreakoutStrategy


class DeltaVolumeNightStrategy(DeltaVolumeBreakoutStrategy):
    """Night session 18:00–8:00 ET. Inherits pivot confirmation, disarm_only_inside_poc."""

    title = "Night Shift Delta Volume Breakout"

    sess_start_hr = 18
    sess_start_mn = 0
    sess_end_hr = 8
    sess_end_mn = 0

    stop_dollars = 600.0
    tp_dollars = 1100.0
