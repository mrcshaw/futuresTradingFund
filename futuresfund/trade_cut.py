"""Cutting the trades of a script that trades too much. This is the chart researcher's work in a study.

A run with more than objective.TRADE_SAVE_MAX trades, or on a proving stage a pace over
objective.TRADES_YEAR_MAX a year, is kept in the trials but can never pass or be filed. The search then
cuts trades, one step per attempt, measuring each step:

1. the script's own daily loss limit, daily profit cap and most-trades-a-day inputs are turned on
   (their own values, or about 2x the stop for a loss limit, the profit limit below for a profit cap,
   and 3 trades a day);
2. the cooldown is doubled. When the doubled value is past the input's maxval, the maxval is widened
   in the copy the engine runs, so the clamps do not snap it back and the filed script carries it.
   A script with no cooldown input gets the desk cooldown;
3. the desk trade limits (strategy_team.ensure_trade_limits) are written into the run copy and the
   desk daily loss limit (about 2x the stop) and desk daily profit limit (the weekly target, never
   under one take profit) are set;
4. the desk's most trades a day is set to 3 (600 a year is about 2.3 a day).

After those, the steps tighten in turn: the cooldown doubles again, the most trades a day drops by
one, the daily loss limit halves toward the stop and the daily profit limit halves. A run under the
cap ends the cut, and the values-only search goes on from those settings. 0 always means off, so
tightening a limit that is off sets it to its starting value instead of halving it.
"""

from __future__ import annotations

import math

from futuresfund.research_rules import is_daily_limit

COOLDOWN_SEED = 10       # bars, the first cooldown when the script or the desk had none
TRADES_A_DAY = 3         # the first most-trades-a-day; 600 a year is about 2.3 a session
DOLLAR_STEP = 50
_FIRST_STEPS = ("own_limits", "cooldown", "daily_limits", "max_trades_day")
_TIGHTEN_STEPS = ("cooldown", "max_trades_day", "daily_loss", "daily_profit")
_STOP_NAMES = ("stop_dollars", "stop_dollar", "sl_dollars", "stop_loss", "fixed_stop")
_TARGET_NAMES = ("tp_dollars", "take_profit", "tp_dollar", "target_dollars")


def new_state() -> dict:
    """A chart's cut: whether it is on, how many cut attempts were still over the cap, and the next step."""
    return {"active": False, "used": 0, "step": 0, "steps": [], "anchor": None, "count": 0}


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dollars_up(value: float) -> int:
    return int(max(DOLLAR_STEP, math.ceil(float(value) / DOLLAR_STEP) * DOLLAR_STEP))


def _dollars_down(value: float) -> int:
    return int(max(DOLLAR_STEP, math.floor(float(value) / DOLLAR_STEP) * DOLLAR_STEP))


def _same_type(old, value):
    if isinstance(old, int) and not isinstance(old, bool):
        return int(round(float(value)))
    return float(value)


def stop_of(params: dict, row: dict | None = None) -> float:
    """The dollar stop the run used: the stop input, the engine's measured stop, or $250."""
    for name in _STOP_NAMES:
        value = _number((params or {}).get(name))
        if value and value > 0:
            return value
    measured = _number((row or {}).get("stop_dollars"))
    if measured and measured > 0:
        return measured
    return 250.0


def profit_limit(params: dict, weekly_profit: float) -> int:
    """The first daily profit limit: the weekly target, never under one take profit."""
    target = 0.0
    for name in _TARGET_NAMES:
        value = _number((params or {}).get(name))
        if value and value > 0:
            target = value
            break
    return _dollars_up(max(float(weekly_profit or 0), target, DOLLAR_STEP))


def _is_max_trades(name: str) -> bool:
    lowered = (name or "").lower()
    return "max" in lowered and "trade" in lowered and not lowered.startswith("desk_")


def _cooldown_name(params: dict, script: str) -> str:
    from futuresfund.strategy_team import own_cooldown

    named = own_cooldown(script)
    if named and named in params:
        return named
    for name in params:
        lowered = name.lower()
        if ("cooldown" in lowered or "cool_down" in lowered) and not lowered.startswith("desk_") and _number(params.get(name)) is not None:
            return name
    return ""


def _money(value) -> str:
    return f"${float(value):,.0f}"


def _own_limits(params: dict, seeds: dict, stop: float, profit: int) -> tuple[dict, list[str]]:
    changes: dict = {}
    said: list[str] = []
    for name, value in params.items():
        if name.lower().startswith("desk_"):
            continue
        if is_daily_limit(name):
            if isinstance(value, bool):
                if not value:
                    changes[name] = True
                    said.append(f"turned on {name}")
                continue
            number = _number(value)
            if number is None or number > 0:
                continue
            seed = _number((seeds or {}).get(name))
            lowered = name.lower()
            if seed and seed > 0:
                amount = seed
            elif "profit" in lowered:
                amount = profit
            else:
                amount = _dollars_up(2 * stop)
            changes[name] = _same_type(value, amount)
            kind = "daily profit cap" if "profit" in lowered else "daily loss limit"
            said.append(f"turned on its own {kind} ({name}) at {_money(amount)}")
        elif _is_max_trades(name):
            if isinstance(value, bool):
                if not value:
                    changes[name] = True
                    said.append(f"turned on {name}")
                continue
            number = _number(value)
            if number is None:
                continue
            seed = _number((seeds or {}).get(name))
            goal = seed if seed and 0 < seed <= TRADES_A_DAY else TRADES_A_DAY
            if number <= 0 or number > goal:
                changes[name] = _same_type(value, goal)
                said.append(f"set its own {name} to {goal:g}")
    return changes, said


def _bounds_high(bounds: dict, name: str):
    return (bounds or {}).get(name, (None, None))[1]


def plan_step(params: dict, script: str, bounds: dict, state: dict, *, seeds: dict | None = None, row: dict | None = None,
              weekly_profit: float = 0.0, can_rewrite: bool = True) -> tuple[dict, str, str] | None:
    """The next cut: (settings, the script text to run, what the researcher did in words), or None when nothing is left.

    state is new_state(); its "step" moves on each call. The script text changes only when the desk trade
    limits are written in or a cooldown maxval is widened. can_rewrite is False for a search with no
    script copy to change, and then only the script's own inputs can move.
    """
    from futuresfund.strategy_team import TRADE_LIMIT_INPUTS, ensure_trade_limits, has_trade_limits, widen_maxval

    base = dict(params or {})
    text = script or ""
    stop = stop_of(base, row)
    profit = profit_limit(base, weekly_profit)
    steps = list(_FIRST_STEPS) + list(_TIGHTEN_STEPS) * 6
    while state.get("step", 0) < len(steps):
        index = int(state.get("step", 0))
        step = steps[index]
        state["step"] = index + 1
        plan = dict(base)
        script_out = text
        said: list[str] = []

        def install() -> bool:
            nonlocal script_out
            if not can_rewrite:
                return False
            if not has_trade_limits(script_out):
                script_out = ensure_trade_limits(script_out)
                if not has_trade_limits(script_out):
                    return False
                said.append("wrote the desk trade limits into the copy this chart runs")
            for name in TRADE_LIMIT_INPUTS:
                if name in script_out.split("// ===== END DESK TRADE LIMITS =====", 1)[0]:
                    plan.setdefault(name, 0)
            return True

        if step == "own_limits":
            changes, words = _own_limits(base, seeds or {}, stop, profit)
            if not changes:
                continue
            plan.update(changes)
            said.extend(words)
        elif step == "cooldown":
            name = _cooldown_name(base, text)
            if not name and install():
                name = "desk_cooldown_bars" if "desk_cooldown_bars" in plan else ""
            if not name:
                continue
            value = _number(plan.get(name)) or 0.0
            nxt = value * 2 if value > 0 else float(COOLDOWN_SEED)
            plan[name] = _same_type(plan.get(name, 0), nxt)
            high = _bounds_high(bounds, name)
            if high is not None and float(plan[name]) > float(high):
                if not can_rewrite:
                    if value >= float(high):
                        continue
                    plan[name] = _same_type(plan.get(name, 0), high)
                else:
                    widened = widen_maxval(script_out, name, float(plan[name]))
                    if widened == script_out:
                        if value >= float(high):
                            continue
                        plan[name] = _same_type(plan.get(name, 0), high)
                    else:
                        script_out = widened
                        said.append(f"widened the {name} maximum past {float(high):g} in the copy this chart runs")
            if value > 0:
                said.append(f"raised the cooldown {name} from {value:g} to {float(plan[name]):g} bars")
            else:
                said.append(f"set the cooldown {name} to {float(plan[name]):g} bars")
        elif step == "daily_limits":
            if not install():
                continue
            if (_number(plan.get("desk_daily_loss")) or 0) > 0 and (_number(plan.get("desk_daily_profit")) or 0) > 0:
                continue
            loss = _dollars_up(2 * stop)
            plan["desk_daily_loss"] = loss
            plan["desk_daily_profit"] = profit
            said.append(f"turned on a {_money(loss)} daily loss limit and a {_money(profit)} daily profit limit")
        elif step == "max_trades_day":
            own = next((name for name in base if _is_max_trades(name) and _number(base.get(name)) is not None), "")
            if not install():
                if not own:
                    continue
                value = _number(plan.get(own)) or 0
                if 0 < value <= 1:
                    continue
                plan[own] = _same_type(plan[own], TRADES_A_DAY if value <= 0 or value > TRADES_A_DAY else value - 1)
                said.append(f"set {own} to {float(plan[own]):g} trades a day")
            else:
                value = _number(plan.get("desk_max_trades_day")) or 0
                if value > 0:
                    if value <= 1:
                        continue
                    plan["desk_max_trades_day"] = int(value) - 1
                else:
                    plan["desk_max_trades_day"] = TRADES_A_DAY
                said.append(f"set the most trades a day to {plan['desk_max_trades_day']}")
        elif step == "daily_loss":
            if not install():
                continue
            value = _number(plan.get("desk_daily_loss")) or 0
            floor = _dollars_up(stop)
            if value <= 0:
                plan["desk_daily_loss"] = _dollars_up(2 * stop)
            elif value <= floor:
                continue
            else:
                plan["desk_daily_loss"] = max(floor, _dollars_down(value / 2))
            said.append(f"set the daily loss limit to {_money(plan['desk_daily_loss'])}")
        elif step == "daily_profit":
            if not install():
                continue
            value = _number(plan.get("desk_daily_profit")) or 0
            if value <= 0:
                plan["desk_daily_profit"] = profit
            elif value <= DOLLAR_STEP:
                continue
            else:
                plan["desk_daily_profit"] = _dollars_down(value / 2)
            said.append(f"set the daily profit limit to {_money(plan['desk_daily_profit'])}")
        if plan == base and script_out == text:
            continue
        sentence = _join(said)
        state.setdefault("steps", []).append(sentence)
        return plan, script_out, sentence
    return None


def _join(parts: list[str]) -> str:
    parts = [part for part in parts if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def tighten_value(name: str, value, seed=None):
    """One stricter value for a trade limit, for learn._tighten_plan. 0 is off: tightening it sets a seed, not a half.

    A cooldown doubles, a most-trades-a-day drops by one, a daily loss or profit limit halves (never under $50).
    Returns None when the input cannot get stricter.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    lowered = (name or "").lower()
    number = float(value)
    seeded = _number(seed)
    if "cooldown" in lowered or "cool_down" in lowered:
        nxt = number * 2 if number > 0 else float(seeded if seeded and seeded > 0 else COOLDOWN_SEED)
    elif "max" in lowered and "trade" in lowered:
        if number <= 0:
            nxt = float(seeded if seeded and seeded > 0 else TRADES_A_DAY)
        elif number <= 1:
            return None
        else:
            nxt = number - 1 if number <= 2 * TRADES_A_DAY else math.floor(number / 2)
    elif "daily" in lowered and ("loss" in lowered or "profit" in lowered):
        if number <= 0:
            if not seeded or seeded <= 0:
                return None
            nxt = seeded
        elif number <= DOLLAR_STEP:
            return None
        else:
            nxt = _dollars_down(number / 2)
    else:
        return None
    out = _same_type(value, nxt)
    return None if out == value else out
