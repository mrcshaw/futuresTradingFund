"""Read a Pine script closely enough to brief the chief executive. Numbers come from the file or the engine."""

from __future__ import annotations

import re

_STRATEGY = re.compile(r"strategy\s*\((.*?)\)", re.DOTALL)
_INPUT = re.compile(
    r"(?P<name>[A-Za-z_]\w*)\s*=\s*input\.(?P<kind>int|float|bool|string)\s*\((?P<body>.*?)\)",
    re.DOTALL,
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def pine_facts(text: str) -> dict:
    """Facts that are written in the script. Missing items stay absent."""
    body = text or ""
    header = _STRATEGY.search(body)
    header_text = header.group(1) if header else ""
    title = _quoted(header_text) or "Untitled strategy"
    inputs = []
    for match in _INPUT.finditer(body):
        raw = match.group("body")
        inputs.append({
            "name": match.group("name"),
            "kind": match.group("kind"),
            "default": _first_value(match.group("kind"), raw),
            "label": _quoted(raw) or match.group("name"),
        })
    named = {item["name"]: item["default"] for item in inputs}
    compact = re.sub(r"\s+", "", body)
    return {
        "title": title,
        "inputs": inputs,
        "named": named,
        "long": bool(re.search(r"\blong\b", body, re.I)),
        "short": bool(re.search(r"\bshort\b", body, re.I)),
        "on_close": "process_orders_on_close=true" in compact,
        "every_tick": "calc_on_every_tick=true" in compact,
        "commission": _header_number(header_text, "commission_value"),
        "slippage": _header_number(header_text, "slippage"),
        "qty": _header_number(header_text, "default_qty_value"),
        "pyramiding": _header_number(header_text, "pyramiding"),
        "point_value": _first_number(named, ("pv", "point_value", "pointvalue")),
        "stop": _first_number(named, ("stop_dollars", "stop_dollar", "sl_dollars", "stop_loss", "fixed_stop")),
        "target": _first_number(named, ("tp_dollars", "take_profit", "tp_dollar", "target_dollars", "profit_target")),
        "stop_points": _first_number(named, ("stop_pts", "stop_points", "sl_points")),
        "atr": any("atr" in item["name"].lower() for item in inputs) or "ta.atr" in body,
        "trail": bool(re.search(r"trail", body, re.I)),
        "breakeven": bool(re.search(r"break\s*even|breakeven", body, re.I)),
        "session": _session(named, body),
        "session_flat": "session_ended" in body or "Session Close" in body or "close_all" in body,
        "unused": _unused(body, inputs),
        "pivot": bool(re.search(r"pivot|ta\.pivothigh|ta\.pivotlow", body, re.I)),
    }


def ceo_report(facts: dict, best: dict | None, quant_note: str, indicator_note: str, attempts: int) -> str:
    """The portfolio manager's report. Engine figures are copied. The script supplies the rules."""
    best = best or {}
    measured = best.get("measured") or {}
    profit = measured.get("net_profit", "not recorded")
    drawdown = measured.get("max_drawdown", "not recorded")
    trades = measured.get("trades", "not recorded")
    win = measured.get("win_rate", "not recorded")
    cleared = "yes" if best.get("passed") else "no"
    lines = [
        "FUTURES FUND",
        "Research report for the Chief Executive Officer",
        "",
        f"Strategy: {facts.get('title') or 'Untitled strategy'}",
        "Prepared by the Portfolio Manager from the Quantitative Researcher and the Indicator Researcher.",
        f"Engine attempts used: {attempts} of 200.",
        f"Best measured result: profit {profit}, drawdown {drawdown}, trades {trades}, win rate {win}.",
        f"Clears the $2,000 trailing drawdown with a profit: {cleared}.",
        f"Parameters of that version: {_params(best.get('params') or {})}",
        "",
        "Quantitative Researcher",
        quant_note.strip() or "No adjustment note was recorded.",
        "",
        "Indicator Researcher",
        indicator_note.strip() or "No indicator note was recorded.",
        "",
        "1. The bet",
        _bet(facts),
        "",
        "2. The stop that is actually active",
        _stop(facts, best.get("params") or {}),
        "",
        "3. The target",
        _target(facts, best.get("params") or {}),
        "",
        "4. The trailing stop and breakeven",
        _trail(facts),
        "",
        "5. Time of day",
        _session_section(facts),
        "",
        "6. Costs and size that change the result",
        _costs(facts),
        "",
        "7. What the backtest cannot prove",
        _cannot(facts),
        "",
        "8. Where this kind of rule set usually makes and loses money",
        _where(facts, best),
    ]
    return "\n".join(lines)


def _bet(facts: dict) -> str:
    sides = []
    if facts.get("long"):
        sides.append("long")
    if facts.get("short"):
        sides.append("short")
    side = " and ".join(sides) if sides else "a side the script does not name"
    kind = _behavior(facts)
    fill = "at the signal bar's close" if facts.get("on_close") else "on the next bar's open, because the script does not set process_orders_on_close"
    if facts.get("every_tick"):
        fill = "intrabar, because calc_on_every_tick is on"
    must = "The coded conditions that build the entry signal must all be true, including any session filter and cooldown written in the file."
    return (
        f"This version tries to catch {kind}. It can trade {side}. {must} "
        f"The fill is {fill}."
    )


def _behavior(facts: dict) -> str:
    title = str(facts.get("title") or "").lower()
    names = " ".join(item["name"].lower() for item in facts.get("inputs") or [])
    blob = f"{title} {names}"
    if "mean" in blob or "revert" in blob or "rsi" in blob:
        return "a mean-reversion move, a stretch back toward an average"
    if "break" in blob or "orb" in blob or "bos" in blob:
        return "a breakout"
    if "pullback" in blob or "dip" in blob:
        return "a dip in a trend"
    if "trail" in blob or "trend" in blob or "ema" in blob:
        return "a trend turn or a continuation after a trend filter"
    return "the behavior named in the script title"


def _stop(facts: dict, params: dict) -> str:
    stop = _overlay(params, facts, ("stop_dollars", "stop_dollar", "sl_dollars", "stop_loss", "fixed_stop"), facts.get("stop"))
    points = facts.get("stop_points")
    point = facts.get("point_value")
    kind = "a fixed dollar stop" if stop is not None else ("an ATR stop" if facts.get("atr") else "a stop the script does not name as dollars, points, ATR, structure, time, or a session exit")
    if facts.get("session_flat") and stop is None:
        kind = "a forced session exit, with no separate dollar stop named"
    dollars = f"${stop:,.0f}" if isinstance(stop, (int, float)) else "not a fixed dollar amount in this version"
    if isinstance(stop, (int, float)) and isinstance(point, (int, float)) and point:
        points = stop / point
    point_text = f"{points:g} points" if isinstance(points, (int, float)) else "not stated as a point distance"
    reward, ratio, needed = _reward(facts, params, stop)
    return (
        f"This version uses {kind}. Initial risk is {point_text}"
        + (f", {dollars}." if isinstance(stop, (int, float)) else ".")
        + f" {reward} The reward-to-risk ratio is {ratio}. The win rate required before commission and slippage is about {needed}. "
        "If the file codes more than one stop, the figures above are the inputs on the version the engine kept."
    )


def _target(facts: dict, params: dict) -> str:
    target = _overlay(params, facts, ("tp_dollars", "take_profit", "tp_dollar", "target_dollars", "profit_target"), facts.get("target"))
    if isinstance(target, (int, float)):
        return (
            f"Winners are capped at ${target:,.0f}. This version does not describe a split. "
            "The position exits together when the target, the stop, or a session flat is hit."
        )
    if facts.get("trail"):
        return "No fixed profit cap is named. A trail is coded, so a winner can stay open until that trail or a session flat takes it. The position is not described as split."
    return "No fixed target is named. A winner is left open until an opposite signal, a stop, or a session flat. The position exits together."


def _trail(facts: dict) -> str:
    if not facts.get("trail") and not facts.get("breakeven"):
        return (
            "The trailing stop is off in this version, and breakeven is off. "
            "The backtest of this version says nothing about a trail. "
            "A trail that is not in the code cannot lock profit, cut winners, or replace a profit cap."
        )
    trail = "on" if facts.get("trail") else "off"
    even = "on" if facts.get("breakeven") else "off"
    tick = "every tick" if facts.get("every_tick") else "on the bar close, so a bar-close trail gives back a full bar before it exits"
    return (
        f"The trailing stop is {trail} in this version. Breakeven is {even}. "
        f"The file does not state a separate arming price beyond the trail input it contains. "
        f"Updates are {tick}. A trail that is on sits under any fixed target the file also codes, and it can only be judged from that code, not from a version where the trail was switched off. "
        "Design judgment: where a trail is on, it locks profit only after price has moved in favor of the trade, and it can cut a winner before a fixed cap if both are present."
    )


def _session_section(facts: dict) -> str:
    session = facts.get("session") or {}
    if not session:
        windows = (
            "No session clock is coded, so 9:30, the first hour, midday, 15:00–16:00, and the 18:00 reopen all use the same rules. "
            "Nothing in the file forces a flat at those times."
        )
    else:
        windows = (
            f"The coded session is {session['start']} to {session['end']} Eastern Time. "
            f"9:30 is {session['open']}. The first hour is {session['first']}. Midday is {session['mid']}. "
            f"The cash close, 15:00–16:00, is {session['close']}. The 18:00 reopen is {session['evening']}. "
            + ("Positions are forced flat when the session ends. " if facts.get("session_flat") else "The file does not show a forced flat at the session end. ")
        )
    return (
        windows
        + "Where 9:30 uses the same rules as the rest of the day, that is stated above. "
        "The open is the least reliable part of a backtest when orders fill on the bar close and stops are not checked every tick, because one bar can hit both the stop and the target."
    )


def _costs(facts: dict) -> str:
    point = facts.get("point_value")
    point_text = f"${point:g} per point" if isinstance(point, (int, float)) else "not named in the header or inputs"
    qty = facts.get("qty")
    commission = facts.get("commission")
    slippage = facts.get("slippage")
    pyramiding = facts.get("pyramiding")
    cost = "Round-trip cost in points was not computed because point value or commission is missing from the file."
    if isinstance(point, (int, float)) and point and isinstance(commission, (int, float)):
        dollars = commission * 2
        slip = ""
        if isinstance(slippage, (int, float)):
            slip_dollars = slippage * 0.25 * point
            dollars += slip_dollars
            slip = f" Slippage is coded as {slippage:g} tick, taken as ${slip_dollars:g} at a 0.25 point tick."
        cost = f"Round-trip commission is ${commission * 2:g}.{slip} That is {dollars / point:g} points, which is the cost to set next to the stop and the target."
    return (
        f"Point value is {point_text}. Contracts are {qty if qty is not None else 'the strategy default'}. "
        f"Commission is {('$' + format(commission, 'g') + ' a side') if isinstance(commission, (int, float)) else 'not in the header'}. "
        f"Slippage is {slippage if slippage is not None else 'not in the header'}. "
        f"Pyramiding is {pyramiding if pyramiding is not None else 'not in the header'}. "
        "Cooldown after an exit is whatever cooldown input the file names; if it has none, the script does not wait. "
        + cost
    )


def _cannot(facts: dict) -> str:
    fill = "Orders in this version fill on the close. " if facts.get("on_close") else "The script does not set fills on the close. "
    tick = "Stops are not calculated every tick. " if not facts.get("every_tick") else "calc_on_every_tick is on. "
    pivot = "Pivots in this file confirm only after later bars. " if facts.get("pivot") else ""
    unused = facts.get("unused") or []
    unused_text = (
        "These inputs are calculated or declared and do not appear again in the entry: " + ", ".join(unused) + ". "
        if unused else "No declared input was found unused in the entry. "
    )
    return fill + tick + pivot + unused_text + "A level that needs future bars is not a live signal."


def _where(facts: dict, best: dict) -> str:
    capped = "Winners are capped, so a large trend pays only the target. " if facts.get("target") else "Winners are not capped by a fixed target in the file. "
    losers = "Losers take the full coded stop when a stop is hit. "
    flat = "Trades still open at the session end are cut by the session flat. " if facts.get("session_flat") else "The file does not cut trades with a session flat. "
    opened = facts.get("session") or {}
    whipsaw = (
        "The open is inside the session, so the open can whipsaw a bar-close fill. "
        if opened.get("open") == "traded with the same rules" or not opened
        else "The open is outside the coded session, so the open itself is stood aside. "
    )
    settled = (
        f"Settled from the code and this engine pass: profit {((best.get('measured') or {}).get('net_profit', 'not recorded'))}, "
        f"drawdown {((best.get('measured') or {}).get('max_drawdown', 'not recorded'))}, "
        f"trades {((best.get('measured') or {}).get('trades', 'not recorded'))}, "
        f"trail cleared: {'yes' if best.get('passed') else 'no'}."
    )
    return (
        capped + losers + flat + whipsaw + settled
        + " Still to be tested on a trade list: result by hour, especially 9:30–10:00, and result with the trail on versus off."
    )


def _session(named: dict, body: str) -> dict | None:
    start_h = named.get("sess_start_hr", named.get("session_start_hour"))
    end_h = named.get("sess_end_hr", named.get("session_end_hour"))
    if not isinstance(start_h, (int, float)) or not isinstance(end_h, (int, float)):
        if "use_session" not in body and "session" not in body.lower():
            return None
        return None
    start_m = named.get("sess_start_mn", 0) or 0
    end_m = named.get("sess_end_mn", 0) or 0
    start = int(start_h) * 60 + int(start_m)
    end = int(end_h) * 60 + int(end_m)

    def covers(minute: int) -> str:
        inside = (start <= minute < end) if start <= end else (minute >= start or minute < end)
        return "traded with the same rules" if inside else "stood aside"

    return {
        "start": f"{int(start_h):02d}:{int(start_m):02d}",
        "end": f"{int(end_h):02d}:{int(end_m):02d}",
        "open": covers(9 * 60 + 30),
        "first": covers(10 * 60),
        "mid": covers(12 * 60),
        "close": covers(15 * 60 + 30),
        "evening": covers(18 * 60),
    }


def _reward(facts: dict, params: dict, stop):
    target = _overlay(params, facts, ("tp_dollars", "take_profit", "tp_dollar", "target_dollars", "profit_target"), facts.get("target"))
    if not isinstance(stop, (int, float)) or not isinstance(target, (int, float)) or stop <= 0:
        return "No fixed reward is coded beside the stop.", "not defined", "not defined from a fixed stop and target"
    ratio = target / stop
    needed = stop / (stop + target) * 100
    return f"The coded reward is ${target:,.0f}.", f"{ratio:.2f}", f"{needed:.1f} percent"


def _overlay(params: dict, facts: dict, keys: tuple, fallback):
    named = facts.get("named") or {}
    for key in keys:
        if key in params and isinstance(params[key], (int, float)):
            return params[key]
        if key in named and isinstance(named[key], (int, float)):
            return named[key]
    return fallback


def _unused(body: str, inputs: list[dict]) -> list[str]:
    unused = []
    for item in inputs:
        name = item["name"]
        if len(re.findall(r"\b" + re.escape(name) + r"\b", body)) <= 1:
            unused.append(name)
    return unused


def _quoted(text: str) -> str:
    match = re.search(r'"([^"]+)"', text or "")
    return match.group(1).strip() if match else ""


def _first_value(kind: str, body: str):
    piece = (body or "").split(",")[0].strip()
    if kind == "bool":
        return piece.lower() == "true"
    if kind == "string":
        return _quoted(piece)
    match = _NUMBER.search(piece)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if kind == "int" else number


def _header_number(header: str, key: str):
    match = re.search(rf"{key}\s*=\s*(-?\d+(?:\.\d+)?)", header or "")
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def _first_number(named: dict, keys: tuple):
    for key in keys:
        value = named.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _params(params: dict) -> str:
    if not params:
        return "the original inputs"
    parts = [f"{key}={value}" for key, value in sorted(params.items())]
    return ", ".join(parts[:12])
