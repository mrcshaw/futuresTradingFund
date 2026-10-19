"""Rules for every research run.

A research run starts with the daily loss limit and the daily profit cap off.
Those inputs then change like any other input: the search can turn them on and
move the amounts, and a change that raises profit stays for the following runs.
The take profit stays larger than the stop: a stop of 100 keeps the take
profit above 100. A larger target is still tried. A stop entered as a percent
of the account is not larger than 3 percent, and the take profit is not larger
than 4 percent. A leftover dollar stop is not larger than $1,500 and a leftover
dollar take profit is not larger than $2,000.
"""

from __future__ import annotations

_STOP_DOLLARS = ("stop_dollars", "stop_dollar", "sl_dollars", "stop_loss", "fixed_stop")
_STOP_PCT = ("stop_pct",)
_STOP_POINTS = ("stop_pts", "stop_points", "sl_points")
_TARGET_DOLLARS = ("tp_dollars", "take_profit", "tp_dollar", "target_dollars")
_TARGET_PCT = ("tp_pct",)
_TARGET_POINTS = ("take_profit_points", "tp_pts", "tp_points")
EXIT_DOLLAR_STEP = 50.0
# The stop is measured on these dollars. The take profit uses the same steps and continues to $2,000.
STOP_LADDER = (250.0, 500.0, 750.0, 1000.0, 1250.0, 1500.0)
TARGET_LADDER = (250.0, 500.0, 750.0, 1000.0, 1250.0, 1500.0, 1750.0, 2000.0)
STOP_CHECKPOINTS = STOP_LADDER


def is_daily_limit(name: str) -> bool:
    """A daily loss limit or a daily profit cap. A max-trades-per-day input is not one."""
    compact = (name or "").lower().replace("_", "")
    if "max" in compact and "trade" in compact:
        return False
    if compact in {"usedailylimits", "usedailycap", "dailylosslimit", "dailylosslim", "dailyprofitcap"}:
        return True
    if "daily" not in compact:
        return False
    return compact.startswith("usedaily") or "loss" in compact or "profitcap" in compact


def reward_pair(params: dict) -> tuple[str, str] | None:
    """The stop and the take profit that share a unit, dollars with dollars or points with points."""
    for stops, targets in (
        (_STOP_PCT, _TARGET_PCT),
        (_STOP_DOLLARS, _TARGET_DOLLARS),
        (_STOP_POINTS, _TARGET_POINTS),
    ):
        stop = next((name for name in stops if _number(params.get(name))), None)
        target = next((name for name in targets if _number(params.get(name))), None)
        if stop and target:
            return stop, target
    return None


def prepare_research(params: dict) -> dict:
    """The settings a research run starts from: no daily cap, and the take profit at the ratio floor."""
    updated = dict(params or {})
    for name, value in list(updated.items()):
        if not is_daily_limit(name):
            continue
        if isinstance(value, bool):
            updated[name] = False
        elif isinstance(value, float):
            updated[name] = 0.0
        elif isinstance(value, int):
            updated[name] = 0
    return hold_reward(updated)


def hold_reward(params: dict | None) -> dict | None:
    """Raise the take profit until it is larger than the stop, and keep both inside the dollar caps."""
    if not params:
        return params
    updated = _cap_dollars(dict(params))
    pair = reward_pair(updated)
    if pair is None:
        return updated
    stop_name, target_name = pair
    stop = float(updated[stop_name])
    target = float(updated[target_name])
    if stop <= 0 or target > stop:
        return updated
    step = exit_step(target_name)
    if step is None:
        step = 1.0 if isinstance(updated[target_name], int) and not isinstance(updated[target_name], bool) else 0.01
    nxt = next_rung(target_name, stop)
    if nxt is None:
        nxt = stop + step
    updated[target_name] = _typed(updated[target_name], nxt)
    return updated


def _exit_kind(name: str) -> str | None:
    """Stop or take profit. A trail or a point distance is neither."""
    compact = (name or "").lower().replace("_", "")
    if "trail" in compact or "point" in compact or compact.endswith("pts") or "mode" in compact:
        return None
    if "takeprofit" in compact or "target" in compact or compact.startswith("tp"):
        return "target"
    # "sl" only as its own end. "loss" plus "limit" also contains those two letters.
    if "stop" in compact or compact.startswith("sl") or compact.endswith("sl"):
        return "stop"
    return None


def exit_ladder(name: str, account_size: float = 50_000.0) -> list[float]:
    """The dollars this input is measured on. A percent input is those dollars divided by the account."""
    kind = _exit_kind(name)
    if kind is None:
        return []
    dollars = TARGET_LADDER if kind == "target" else STOP_LADDER
    compact = (name or "").lower().replace("_", "")
    if compact.endswith("pct") or "percent" in compact:
        account = float(account_size) if account_size else 50_000.0
        if account <= 0:
            return []
        return [round(amount / account * 100.0, 4) for amount in dollars]
    return list(dollars)


def next_rung(name: str, current, account_size: float = 50_000.0):
    """The next ladder value above current. None when current is already at the top."""
    try:
        value = float(current)
    except (TypeError, ValueError):
        return None
    for rung in exit_ladder(name, account_size):
        if value + 1e-9 < rung:
            return rung
    return None


def stop_checkpoints(name: str, account_size: float = 50_000.0) -> list[float]:
    """Dollar stops the search measures: $250, $500, $750, $1,000, $1,250, and $1,500."""
    if _exit_kind(name) != "stop":
        return []
    return exit_ladder(name, account_size)


def opening_stop_value(name: str, value, account_size: float = 50_000.0):
    """A $1,000 template stop starts at $250. A $1,500 template target starts at $500."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    compact = (name or "").lower().replace("_", "")
    if "trail" in compact:
        return value
    target = "takeprofit" in compact or "target" in compact or compact.startswith("tp")
    percent = compact.endswith("pct") or "percent" in compact
    number = float(value)
    if target and not percent and number >= 1500:
        nxt = 500.0
    elif target and percent and number >= 3:
        nxt = 1.0
    elif not target and number >= (2.0 if percent else 1000.0) and stop_checkpoints(name, account_size):
        nxt = stop_checkpoints(name, account_size)[0]
    else:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and nxt >= 1:
        return int(round(nxt))
    return nxt


def exit_step(name: str, account_size: float = 50_000.0) -> float | None:
    """One $50 step for a stop or take profit. A percent input steps by $50 of the account."""
    compact = (name or "").lower().replace("_", "")
    if "trail" in compact:
        return None
    if not any(token in compact for token in ("stop", "takeprofit", "target")) and not compact.startswith("tp") and not compact.startswith("sl"):
        return None
    if "point" in compact or compact.endswith("pts"):
        return None
    if compact.endswith("pct") or "percent" in compact:
        account = float(account_size) if account_size else 50_000.0
        if account <= 0:
            return None
        return round(EXIT_DOLLAR_STEP / account * 100.0, 4)
    return EXIT_DOLLAR_STEP


def exit_cap(name: str) -> float | None:
    """The largest stop or take profit the search may try."""
    from futuresfund.risk_style import STOP_CAP, STOP_PCT_CAP, TP_CAP, TP_PCT_CAP

    compact = (name or "").lower().replace("_", "")
    if exit_step(name) is None:
        return None
    percent = compact.endswith("pct") or "percent" in compact
    target = "takeprofit" in compact or "target" in compact or compact.startswith("tp")
    if percent:
        return TP_PCT_CAP if target else STOP_PCT_CAP
    return TP_CAP if target else STOP_CAP


def _cap_dollars(params: dict) -> dict:
    """A percent stop stays at or under 3 percent of the account. A percent take profit stays at or under 4 percent.

    A leftover dollar stop stays at or under $1,500. A leftover dollar take profit stays at or under $2,000.
    """
    from futuresfund.risk_style import STOP_CAP, STOP_PCT_CAP, TP_CAP, TP_PCT_CAP

    updated = dict(params)
    for name in _STOP_PCT:
        value = updated.get(name)
        if _number(value) and float(value) > STOP_PCT_CAP:
            updated[name] = _typed(value, STOP_PCT_CAP)
    for name in _TARGET_PCT:
        value = updated.get(name)
        if _number(value) and float(value) > TP_PCT_CAP:
            updated[name] = _typed(value, TP_PCT_CAP)
    for name in _STOP_DOLLARS:
        value = updated.get(name)
        if _number(value) and float(value) > STOP_CAP:
            updated[name] = _typed(value, STOP_CAP)
    for name in _TARGET_DOLLARS:
        value = updated.get(name)
        if _number(value) and float(value) > TP_CAP:
            updated[name] = _typed(value, TP_CAP)
    return updated


def daily_restore(name: str, value, seeds: dict | None) -> int | float | None:
    """The script's own daily limit, used as the first step up from off."""
    if not is_daily_limit(name) or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if float(value) != 0:
        return None
    seed = (seeds or {}).get(name)
    if isinstance(seed, bool) or not isinstance(seed, (int, float)) or float(seed) <= 0:
        return None
    if isinstance(value, int):
        return int(round(float(seed)))
    return float(seed)


def focus_keys(before: dict, after: dict) -> list[str]:
    """The setting the researcher meant to move. A take profit that only keeps the ratio follows the stop."""
    keys = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    pair = reward_pair(after)
    if pair and set(keys) == set(pair):
        stop_name, target_name = pair
        try:
            new_stop = float(after[stop_name])
            new_target = float(after[target_name])
            old_target = float(before.get(target_name))
        except (TypeError, ValueError):
            return keys
        if new_target > new_stop and old_target <= new_stop:
            return [stop_name]
    return keys


# One contract is flattened at the session close, so the target has to be a move that session can print.
SESSION_POINTS = 250


def risk_problems(source: str) -> list[str]:
    """A dollar stop below a tick, or a target one session cannot reach.

    The search may still move a stop that is at least one tick. A default under its own minimum,
    or a take profit of hundreds of points on one contract, is sent back to be reasoned out.
    """
    inputs = _float_inputs(source)
    point = _point_value(inputs)
    tick_dollars = None if point is None else point * 0.25
    problems = []
    for name in _STOP_DOLLARS:
        found = inputs.get(name)
        if found is None:
            continue
        default, low, _high = found
        if low is not None and default + 1e-9 < float(low):
            problems.append(
                f"{name} is {default}, below its own minimum of {low}. The default has to sit inside minval and maxval."
            )
        if tick_dollars is not None and default + 1e-9 < tick_dollars:
            problems.append(
                f"{name} is ${default}, smaller than one tick (${tick_dollars:g} at this point value). "
                "The search can still move the stop. Each stop has to be at least one tick."
            )
        elif tick_dollars is None and default < 1:
            problems.append(
                f"{name} is ${default}. A dollar stop has to be at least one tick, not a fraction of a cent. The search can still move it."
            )
    for name in _TARGET_DOLLARS:
        found = inputs.get(name)
        if found is None or point is None or point <= 0:
            continue
        default, _low, _high = found
        points = default / point
        if points > SESSION_POINTS:
            problems.append(
                f"{name} is ${default:g}, {points:.0f} points on one contract. "
                "The session closes the trade, so the take profit has to be a move that session can reach."
            )
    return problems


def _float_inputs(source: str) -> dict[str, tuple[float, float | None, float | None]]:
    import re

    found = {}
    pattern = re.compile(
        r"([A-Za-z_]\w*)\s*=\s*input\.(?:float|int)\s*\(\s*(-?\d+(?:\.\d+)?)\s*,([^)\n]*)\)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(source or ""):
        rest = match.group(3)
        found[match.group(1)] = (float(match.group(2)), _bound(rest, "minval"), _bound(rest, "maxval"))
    return found


def _bound(body: str, key: str) -> float | None:
    import re

    match = re.search(rf"\b{key}\s*=\s*(-?\d+(?:\.\d+)?)", body or "")
    if not match:
        return None
    return float(match.group(1))


def _point_value(inputs: dict) -> float | None:
    for name in ("pv", "point_value", "pointvalue"):
        found = inputs.get(name)
        if found is not None and found[0] > 0:
            return found[0]
    return None


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _typed(previous, floor: float):
    if isinstance(previous, int) and not isinstance(previous, bool):
        return int(round(floor))
    return round(float(floor), 4)


def _key(params: dict) -> str:
    import json

    return json.dumps(params, sort_keys=True, default=str)
