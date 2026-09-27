"""Search simple futures rules on exported bars and keep the one the backtest can defend."""

from __future__ import annotations

import json

from futuresfund.config import BARS_PATH
from futuresfund.contracts import POINT_VALUE, root_of
from futuresfund.pines import render

_COST = 1.0


def load_bars() -> list[dict]:
    if not BARS_PATH.is_file():
        return []
    try:
        data = json.loads(BARS_PATH.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_bars(bars: list[dict]) -> None:
    BARS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BARS_PATH.write_text(json.dumps(bars[-20000:]), encoding="utf-8")


def append_bar(bars: list[dict], bar: dict) -> list[dict]:
    """Add the alert's bar to the continuous series. The same timestamp updates that bar."""
    bars = list(bars)
    bars.append(bar)
    bars.sort(key=lambda item: item["t"])
    unique = []
    for item in bars:
        if unique and unique[-1]["t"] == item["t"]:
            unique[-1] = item
        else:
            unique.append(item)
    return unique[-20000:]


def bar_facts(bars: list[dict]) -> str:
    """A short description of the file the agents are allowed to use."""
    closes = [bar["c"] for bar in bars]
    first, last = closes[0], closes[-1]
    change = (last - first) / first * 100 if first else 0.0
    window = closes[-20:] if len(closes) >= 20 else closes
    recent = (window[-1] - window[0]) / window[0] * 100 if window[0] else 0.0
    ranges = []
    for earlier, later in zip(bars, bars[1:]):
        ranges.append(max(later["h"] - later["l"], abs(later["h"] - earlier["c"]), abs(later["l"] - earlier["c"])))
    sample = ranges[-14:] or [0.0]
    atr = sum(sample) / len(sample)
    return (
        f"{len(bars)} bars from {bars[0]['t']} to {bars[-1]['t']}. "
        f"First close {first:.2f}, last close {last:.2f}, change {change:.1f}%. "
        f"High {max(bar['h'] for bar in bars):.2f}, low {min(bar['l'] for bar in bars):.2f}. "
        f"Last {len(window)} bars changed {recent:.1f}%. Recent ATR {atr:.2f}."
    )


def develop(bars: list[dict], instrument: str, timeframe: str, account: str, account_size: float, profit_target: float) -> dict:
    if not str(account).strip():
        raise ValueError("Name the prop account this strategy will trade.")
    if len(bars) < 80:
        raise ValueError("The file needs at least 80 bars before a strategy can be tested.")
    if account_size <= 0 or profit_target <= 0:
        raise ValueError("Account size and profit target must be greater than zero.")
    try:
        point = POINT_VALUE[root_of(instrument)]
    except (ValueError, KeyError) as exc:
        raise ValueError("That contract does not have a point value on this desk.") from exc
    qty = _size(bars, account_size, point)
    ranked = []
    for name, params in _candidates():
        desired = _desired(bars, name, params)
        if desired is None:
            continue
        result = _simulate(bars, desired, qty, point, account_size)
        result["name"] = name
        result["params"] = params
        ranked.append(result)
    if not ranked:
        raise ValueError("None of the rules could be tested on this file.")
    return _assemble(ranked, instrument, timeframe, account, account_size, profit_target, qty)


def evaluate_rule(bars: list[dict], instrument: str, account_size: float, profit_target: float, name: str, params: dict) -> dict | None:
    """Backtest one rule the agents proposed. Returns None when the bars are too short for it."""
    point = POINT_VALUE[root_of(instrument)]
    qty = _size(bars, account_size, point)
    desired = _desired(bars, name, params)
    if not desired:
        return None
    result = _simulate(bars, desired, qty, point, account_size, stop=_stop_amount(params))
    result["reached_target"] = result["net_profit"] >= profit_target
    title = _title(name, params)
    formula = _formula(name, params)
    stop = _stop_amount(params)
    if stop:
        title = f"{title}, stop ${stop:,.0f}"
        formula = formula + f" Flatten the trade when the open loss reaches ${stop:,.0f}, before the account trail is touched."
    return _row(name, params, result, title, formula, point, account_size, qty)


def compare_timeframes(frames: dict[str, list[dict]], instrument: str, account_size: float, profit_target: float, name: str, params: dict) -> dict | None:
    """Backtest one rule on each timeframe and keep the best one that clears the trail."""
    tested = []
    for timeframe, series in frames.items():
        row = evaluate_rule(series, instrument, account_size, profit_target, name, params)
        if row is None:
            continue
        row = dict(row)
        row["timeframe"] = timeframe
        tested.append(row)
    if not tested:
        return None
    proven = [row for row in tested if row["proven"]]
    pool = proven or tested
    pool.sort(key=lambda row: float(row["backtest"]["net_profit"]), reverse=True)
    best = dict(pool[0])
    best["timeframe"] = best["timeframe"]
    best["id"] = f"{best['id']}-{best['timeframe']}"
    best["title"] = f"{best['title']} on {best['timeframe']}"
    best["proven"] = bool(proven)
    best["frames"] = [
        {
            "timeframe": row["timeframe"],
            "net_profit": row["backtest"]["net_profit"],
            "max_drawdown": row["backtest"]["max_drawdown"],
            "trades": row["backtest"]["trades"],
            "proven": row["proven"],
        }
        for row in sorted(tested, key=lambda row: row["timeframe"])
    ]
    return best


def _row(name, params, result, title, formula, point, account_size, qty) -> dict:
    return {
        "id": _sid(name, params),
        "name": name,
        "title": title,
        "params": params,
        "rule": {"name": name, **params},
        "formula": formula,
        "pine": render(name, params, title, point, account_size),
        "proven": result["proven"],
        "active": False,
        "backtest": {key: result[key] for key in ("net_profit", "max_drawdown", "drawdown_limit", "trades", "win_rate", "reached_target", "bars", "ending_equity", "breached")},
        "qty": qty,
    }


def assemble_book(rows: list[dict], instrument: str, timeframe: str, account: str, account_size: float, profit_target: float) -> dict:
    """Keep every proposal. Arm only the best one whose drawdown stayed inside the account."""
    ranked = [dict(row) for row in rows]
    ranked.sort(key=lambda item: (item["proven"], item["backtest"]["reached_target"], item["backtest"]["net_profit"], -item["backtest"]["max_drawdown"]), reverse=True)
    catalog = []
    for item in ranked:
        item["reached_target"] = item["backtest"]["reached_target"]
        name = item["name"]
        params = item["params"]
        title = _title(name, params)
        catalog.append({
            "id": item["id"],
            "name": name,
            "title": title,
            "params": params,
            "rule": item["rule"],
            "formula": item["formula"],
            "pine": item["pine"],
            "proven": item["proven"],
            "active": False,
            "backtest": item["backtest"],
        })
    return _pack(catalog, instrument, timeframe, account, account_size, profit_target, int(rows[0]["qty"]) if rows else 1)


def _assemble(ranked: list[dict], instrument: str, timeframe: str, account: str, account_size: float, profit_target: float, qty: int) -> dict:
    ranked.sort(key=lambda item: (item["proven"], item["net_profit"] >= profit_target, item["net_profit"], -item["max_drawdown"]), reverse=True)
    point = POINT_VALUE[root_of(instrument)]
    catalog = []
    for item in ranked:
        item["reached_target"] = item["net_profit"] >= profit_target
        name = item["name"]
        params = item["params"]
        title = _title(name, params)
        catalog.append({
            "id": _sid(name, params),
            "name": name,
            "title": title,
            "params": params,
            "rule": {"name": name, **params},
            "formula": _formula(name, params),
            "pine": render(name, params, title, point, account_size),
            "proven": item["proven"],
            "active": False,
            "backtest": {key: item[key] for key in ("net_profit", "max_drawdown", "drawdown_limit", "trades", "win_rate", "reached_target", "bars", "ending_equity", "breached")},
        })
    return _pack(catalog, instrument, timeframe, account, account_size, profit_target, qty)


def _pack(catalog: list[dict], instrument: str, timeframe: str, account: str, account_size: float, profit_target: float, qty: int) -> dict:
    proven = [item for item in catalog if item["proven"]]
    for item in catalog:
        item["active"] = False
        item["recommended"] = False
    lead = proven[0] if proven else catalog[0]
    return {
        "contract": instrument,
        "timeframe": timeframe,
        "account": account,
        "account_size": account_size,
        "profit_target": profit_target,
        "qty": qty,
        "rule": lead["rule"],
        "formula": lead["formula"],
        "stance": "paused",
        "proven": bool(proven),
        "backtest": lead["backtest"],
        "strategies": catalog,
    }


def active_rules(book: dict | None) -> list[dict]:
    if not book:
        return []
    rows = [item for item in book.get("strategies") or [] if item.get("active") and item.get("proven")]
    if rows:
        return rows
    if book.get("rule") and book.get("stance") == "keep" and book.get("proven"):
        return [{"id": "legacy", "title": "Strategy", "rule": book["rule"], "proven": True, "active": True}]
    return []


def combine_signs(signs: list[int]) -> int | None:
    """Agreed side, flat when every active rule is flat, or None when they conflict."""
    if not signs:
        return 0
    nonzero = [sign for sign in signs if sign]
    if not nonzero:
        return 0
    if all(sign == nonzero[0] for sign in nonzero):
        return nonzero[0]
    return None


def merge_active(previous: list[dict], fresh: list[dict], live_pnl: float) -> list[dict]:
    """Keep a strategy active when it is still profitable. Drop a loser if the interval mark is down."""
    old = {item["id"]: item for item in previous}
    best = next((item["id"] for item in fresh if item["proven"]), None)
    kept = False
    for item in fresh:
        prior = old.get(item["id"])
        active = bool(prior and prior.get("active") and item["proven"])
        if live_pnl < 0 and active and item["id"] != best:
            active = False
        item["active"] = active
        kept = kept or active
    if not kept:
        for item in fresh:
            item["active"] = bool(item["proven"] and item["id"] == best)
    return fresh


def sync_lead(book: dict) -> dict:
    """Point the book at the first active strategy so meetings and the card have one summary."""
    actives = [item for item in book.get("strategies") or [] if item.get("active") and item.get("proven")]
    book["proven"] = bool(actives)
    book["stance"] = "keep" if actives else "paused"
    if actives:
        book["rule"] = actives[0]["rule"]
        book["formula"] = actives[0]["formula"]
        book["backtest"] = actives[0]["backtest"]
    return book


def desired_sign(bars: list[dict], rule: dict) -> int:
    name = rule.get("name")
    params = {key: value for key, value in rule.items() if key != "name"}
    series = _desired(bars, name, params)
    if not series:
        return 0
    return int(series[-1])


def tight_stop(rules: list[dict]) -> float | None:
    """The smallest dollar stop among the armed rules. A rule with no stop does not loosen the others."""
    amounts = []
    for item in rules:
        amount = _stop_amount(item.get("rule") or {})
        if amount:
            amounts.append(amount)
    return min(amounts) if amounts else None


def stop_hit(entry: float, contracts: int, bar: dict, dollars: float, point: float) -> bool:
    """True when this bar's worst price has lost the stop dollars from the entry."""
    if contracts == 0 or dollars <= 0 or point <= 0 or entry is None:
        return False
    qty = abs(int(contracts))
    if contracts > 0:
        worst = bar["l"] if bar.get("l") is not None else bar["c"]
        loss = (float(entry) - float(worst)) * point * qty
    else:
        worst = bar["h"] if bar.get("h") is not None else bar["c"]
        loss = (float(worst) - float(entry)) * point * qty
    return loss >= dollars


def absorb_alert(forming: dict | None, bar: dict, alert_frame: str, strategy_frame: str) -> tuple[list[dict], dict]:
    """Fold one alert into the strategy candle.

    A finer alert updates the candle that is still open. The candle is returned only when it closes.
    An alert on the strategy timeframe is that candle, already closed.
    """
    from futuresfund.timeframe import bar_minutes, bucket_open

    forming = dict(forming or {})
    bucket = bucket_open(bar["t"], strategy_frame)
    done: list[dict] = []
    if forming.get("bucket") and forming["bucket"] != bucket:
        done.append(_from_forming(forming))
        forming = {}
    if bar_minutes(alert_frame) >= bar_minutes(strategy_frame):
        done.append(_from_bar(bar, bucket))
        return done, {}
    if not forming:
        forming = {"bucket": bucket, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar.get("v") or 0}
    else:
        forming["h"] = max(float(forming["h"]), float(bar["h"]))
        forming["l"] = min(float(forming["l"]), float(bar["l"]))
        forming["c"] = bar["c"]
        forming["v"] = float(forming.get("v") or 0) + float(bar.get("v") or 0)
    return done, forming


def follow_rules(bars: list[dict], rules: list[dict], memory: dict | None) -> tuple[int | None, dict]:
    """The armed side after the latest strategy candle.

    A repeat of the same candle reuses the saved side. One new candle updates that side from the
    saved indicator state, so a live alert does not walk the whole history again.
    """
    if not bars or not rules:
        return 0, memory or {}
    ids = [str(item.get("id") or _rule_id(item.get("rule") or {})) for item in rules]
    memory = dict(memory or {})
    last = bars[-1]["t"]
    same = memory.get("ids") == ids
    if same and memory.get("through") == last and memory.get("count") == len(bars):
        return _saved_sign(memory.get("sign")), memory
    states = dict(memory.get("states") or {}) if same else {}
    warm = (
        same
        and memory.get("count") == len(bars) - 1
        and len(bars) >= 2
        and memory.get("through") == bars[-2]["t"]
        and all(rule_id in states for rule_id in ids)
    )
    start = len(bars) - 1 if warm else 0
    if not warm:
        states = {}
    for index in range(start, len(bars)):
        for item, rule_id in zip(rules, ids):
            states[rule_id] = _step_rule(states.get(rule_id), bars[index], item.get("rule") or {}, index)
    signs = [int(states[rule_id].get("position") or 0) for rule_id in ids]
    agreed = combine_signs(signs)
    saved = {"ids": ids, "through": last, "count": len(bars), "sign": "conflict" if agreed is None else agreed, "states": states}
    return agreed, saved


def _saved_sign(value) -> int | None:
    if value == "conflict":
        return None
    return int(value or 0)


def _from_forming(forming: dict) -> dict:
    return {
        "t": forming["bucket"],
        "o": forming["o"],
        "h": forming["h"],
        "l": forming["l"],
        "c": forming["c"],
        "v": forming.get("v") or 0,
    }


def _from_bar(bar: dict, bucket: str) -> dict:
    return {"t": bucket, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar.get("v") or 0}


def _step_rule(state: dict | None, bar: dict, rule: dict, index: int) -> dict:
    """One candle of one rule. The position matches the backtest rule on the bars seen so far."""
    state = dict(state or {})
    name = rule.get("name")
    params = {key: value for key, value in rule.items() if key != "name"}
    position = int(state.get("position") or 0)
    price = float(bar["c"])
    if name == "ema_cross":
        fast_n, slow_n = int(params["fast"]), int(params["slow"])
        fast, slow = _step_ema(state.get("fast"), price, fast_n), _step_ema(state.get("slow"), price, slow_n)
        if index >= slow_n:
            if fast > slow:
                position = 1
            elif fast < slow:
                position = -1
        return {"position": position, "fast": fast, "slow": slow}
    if name == "bollinger":
        length, dev = int(params["length"]), float(params["dev"])
        closes = list(state.get("closes") or [])
        closes.append(price)
        if len(closes) > length:
            closes = closes[-length:]
        if index >= length and len(closes) == length:
            mean = sum(closes) / length
            var = sum((item - mean) ** 2 for item in closes) / length
            band = dev * (var ** 0.5)
            if price < mean - band:
                position = 1
            elif price > mean + band:
                position = -1
            elif position == 1 and price > mean:
                position = 0
            elif position == -1 and price < mean:
                position = 0
        return {"position": position, "closes": closes}
    if name == "macd":
        fast_n, slow_n, sig_n = int(params["fast"]), int(params["slow"]), int(params["signal"])
        fast = _step_ema(state.get("fast"), price, fast_n)
        slow = _step_ema(state.get("slow"), price, slow_n)
        spread = fast - slow
        signal = _step_ema(state.get("signal"), spread, sig_n)
        if index >= slow_n + sig_n and fast_n < slow_n:
            if spread > signal:
                position = 1
            elif spread < signal:
                position = -1
        return {"position": position, "fast": fast, "slow": slow, "signal": signal}
    if name == "donchian":
        length = int(params["length"])
        prior = list(state.get("prior") or [])
        if index >= length and len(prior) >= length:
            window = prior[-length:]
            if price > max(item["h"] for item in window):
                position = 1
            elif price < min(item["l"] for item in window):
                position = -1
        prior.append({"h": float(bar["h"]), "l": float(bar["l"])})
        return {"position": position, "prior": prior[-length:]}
    if name == "supertrend":
        length, mult = int(params["length"]), float(params["mult"])
        atr, atr_state = _step_atr(state.get("atr_state") or {}, bar, length)
        if atr:
            mid = (float(bar["h"]) + float(bar["l"])) / 2
            if price > mid + mult * atr:
                position = 1
            elif price < mid - mult * atr:
                position = -1
        return {"position": position, "atr_state": atr_state}
    if name == "vwap_side":
        length = int(params["length"])
        recent = (list(state.get("recent") or []) + [_plain(bar)])[-length:]
        if index >= length:
            line = _window_vwma(recent, length)
            if price > line:
                position = 1
            elif price < line:
                position = -1
        return {"position": position, "recent": recent}
    if name == "poc_pullback":
        lookback, rows = int(params["lookback"]), int(params["rows"])
        keep = max(lookback, 20)
        recent = (list(state.get("recent") or []) + [_plain(bar)])[-keep:]
        if index >= lookback and len(recent) >= lookback:
            low_edge, high_edge = _poc_band(recent[-lookback:], rows)
            average = _window_vwma(recent, 20)
            if low_edge is not None:
                if price > high_edge and float(bar["l"]) <= high_edge and price > float(bar["o"]) and price > average:
                    position = 1
                elif price < low_edge and float(bar["h"]) >= low_edge and price < float(bar["o"]) and price < average:
                    position = -1
        return {"position": position, "recent": recent}
    if name == "rsi_revert":
        period = int(params["period"])
        value, rsi_state = _step_rsi(state.get("rsi") or {}, price, period)
        if index >= period and value is not None:
            if value < float(params["low"]):
                position = 1
            elif value > float(params["high"]):
                position = -1
            elif position == 1 and value > 50:
                position = 0
            elif position == -1 and value < 50:
                position = 0
        rsi_state["position"] = position
        return {"position": position, "rsi": {key: rsi_state[key] for key in rsi_state if key != "position"}}
    return {"position": position}


def _step_ema(previous, price: float, length: int) -> float:
    if previous is None:
        return price
    weight = 2 / (length + 1)
    return price * weight + float(previous) * (1 - weight)


def _step_atr(state: dict, bar: dict, length: int) -> tuple[float | None, dict]:
    previous = state.get("prev_close")
    if previous is None:
        true_range = float(bar["h"]) - float(bar["l"])
    else:
        true_range = max(float(bar["h"]) - float(bar["l"]), abs(float(bar["h"]) - float(previous)), abs(float(bar["l"]) - float(previous)))
    window = list(state.get("trs") or [])
    smoothed = state.get("atr")
    if smoothed is None:
        window.append(true_range)
        if len(window) == length:
            smoothed = sum(window) / length
            window = []
    else:
        smoothed = (float(smoothed) * (length - 1) + true_range) / length
    return (float(smoothed) if smoothed is not None else None), {"prev_close": float(bar["c"]), "trs": window, "atr": smoothed}


def _step_rsi(state: dict, price: float, length: int) -> tuple[float | None, dict]:
    """RSI at this close. The value matches the last point of the backtest RSI, which includes this close."""
    previous = state.get("prev")
    if previous is None:
        return 50.0, {"prev": price, "changes": 0, "seed_gain": 0.0, "seed_loss": 0.0, "avg_gain": None, "avg_loss": None, "value": 50.0}
    change = price - float(previous)
    gain = max(change, 0.0)
    loss = max(-change, 0.0)
    changes = int(state.get("changes") or 0)
    if changes < length:
        seed_gain = float(state.get("seed_gain") or 0) + gain
        seed_loss = float(state.get("seed_loss") or 0) + loss
        changes += 1
        avg_gain = seed_gain / length if changes == length else None
        avg_loss = seed_loss / length if changes == length else None
        value = 50.0
    else:
        seed_gain = float(state.get("seed_gain") or 0)
        seed_loss = float(state.get("seed_loss") or 0)
        avg_gain = (float(state.get("avg_gain")) * (length - 1) + gain) / length
        avg_loss = (float(state.get("avg_loss")) * (length - 1) + loss) / length
        changes += 1
        value = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return value, {
        "prev": price,
        "changes": changes,
        "seed_gain": seed_gain,
        "seed_loss": seed_loss,
        "avg_gain": avg_gain,
        "avg_loss": avg_loss,
        "value": value,
    }


def _plain(bar: dict) -> dict:
    return {"o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar.get("v") or 0}


def _window_vwma(bars: list[dict], length: int) -> float:
    window = bars[-length:]
    weight = sum((bar.get("v") or 1) for bar in window)
    return sum(bar["c"] * (bar.get("v") or 1) for bar in window) / weight


def trade_plan(desired_sign: int, held: float, qty: int) -> dict:
    """Turn the rule's side into place, hold, or close against the contracts already on."""
    target = int(desired_sign) * int(qty)
    held = int(held or 0)
    if target == held:
        return {"action": "hold", "side": None, "qty": 0, "flatten_first": False, "target": target}
    if target == 0:
        return {"action": "close", "side": "SELL" if held > 0 else "BUY", "qty": abs(held), "flatten_first": False, "target": 0}
    flip = (held > 0 and target < 0) or (held < 0 and target > 0)
    if held == 0 or flip:
        return {
            "action": "place",
            "side": "BUY" if target > 0 else "SELL",
            "qty": abs(target),
            "flatten_first": flip,
            "target": target,
        }
    delta = target - held
    return {
        "action": "place",
        "side": "BUY" if delta > 0 else "SELL",
        "qty": abs(delta),
        "flatten_first": False,
        "target": target,
    }


def choose_update(current: dict, candidate: dict, live_pnl: float) -> str:
    """Keep the armed rule, adjust its settings, or replace it when the new test is better."""
    if not candidate.get("proven"):
        return "pause"
    if _rule_id(current.get("rule")) == _rule_id(candidate.get("rule")):
        return "keep"
    current_net = float((current.get("backtest") or {}).get("net_profit") or 0)
    new_net = float((candidate.get("backtest") or {}).get("net_profit") or 0)
    if new_net > current_net or live_pnl < 0:
        same_family = (current.get("rule") or {}).get("name") == (candidate.get("rule") or {}).get("name")
        return "adjust" if same_family else "replace"
    return "keep"


def live_pnl(intervals: list[dict], instrument: str) -> float:
    try:
        point = POINT_VALUE[root_of(instrument)]
    except (ValueError, KeyError):
        point = 1
    total = 0.0
    rows = [row for row in intervals if row.get("price") is not None]
    for earlier, later in zip(rows, rows[1:]):
        total += (float(later["price"]) - float(earlier["price"])) * float(earlier.get("target") or 0) * point
    return round(total, 2)


def _candidates() -> list[tuple[str, dict]]:
    return [
        ("ema_cross", {"fast": 8, "slow": 34}),
        ("ema_cross", {"fast": 12, "slow": 50}),
        ("donchian", {"length": 20}),
        ("rsi_revert", {"period": 14, "low": 30, "high": 70}),
        ("poc_pullback", {"lookback": 40, "rows": 8}),
        ("poc_pullback", {"lookback": 80, "rows": 12}),
        ("vwap_side", {"length": 30}),
    ]


def _desired(bars: list[dict], name: str, params: dict) -> list[int] | None:
    closes = [bar["c"] for bar in bars]
    if name == "ema_cross":
        fast, slow = int(params["fast"]), int(params["slow"])
        if len(closes) <= slow:
            return None
        fast_line, slow_line = _ema(closes, fast), _ema(closes, slow)
        position = 0
        out = []
        for i in range(len(closes)):
            if i >= slow:
                if fast_line[i] > slow_line[i]:
                    position = 1
                elif fast_line[i] < slow_line[i]:
                    position = -1
            out.append(position)
        return out
    if name == "donchian":
        length = int(params["length"])
        if len(bars) <= length:
            return None
        position = 0
        out = []
        for i, bar in enumerate(bars):
            if i >= length:
                window = bars[i - length:i]
                if bar["c"] > max(item["h"] for item in window):
                    position = 1
                elif bar["c"] < min(item["l"] for item in window):
                    position = -1
            out.append(position)
        return out
    if name == "rsi_revert":
        period = int(params["period"])
        line = _rsi(closes, period)
        if not line:
            return None
        position = 0
        out = []
        for i, value in enumerate(line):
            if i >= period:
                if value < float(params["low"]):
                    position = 1
                elif value > float(params["high"]):
                    position = -1
                elif position == 1 and value > 50:
                    position = 0
                elif position == -1 and value < 50:
                    position = 0
            out.append(position)
        return out
    if name == "poc_pullback":
        lookback, rows = int(params["lookback"]), int(params["rows"])
        if len(bars) <= lookback:
            return None
        average = _vwma(bars, 20)
        position = 0
        out = []
        for i, bar in enumerate(bars):
            if i >= lookback:
                low_edge, high_edge = _poc_band(bars[i - lookback + 1:i + 1], rows)
                if low_edge is not None:
                    if bar["c"] > high_edge and bar["l"] <= high_edge and bar["c"] > bar["o"] and bar["c"] > average[i]:
                        position = 1
                    elif bar["c"] < low_edge and bar["h"] >= low_edge and bar["c"] < bar["o"] and bar["c"] < average[i]:
                        position = -1
            out.append(position)
        return out
    if name == "vwap_side":
        length = int(params["length"])
        if len(bars) <= length:
            return None
        line = _vwma(bars, length)
        position = 0
        out = []
        for i, bar in enumerate(bars):
            if i >= length:
                if bar["c"] > line[i]:
                    position = 1
                elif bar["c"] < line[i]:
                    position = -1
            out.append(position)
        return out
    if name == "bollinger":
        length, dev = int(params["length"]), float(params["dev"])
        if len(closes) <= length:
            return None
        position = 0
        out = []
        for i, price in enumerate(closes):
            if i >= length:
                window = closes[i - length + 1:i + 1]
                mean = sum(window) / length
                var = sum((item - mean) ** 2 for item in window) / length
                band = dev * (var ** 0.5)
                if price < mean - band:
                    position = 1
                elif price > mean + band:
                    position = -1
                elif position == 1 and price > mean:
                    position = 0
                elif position == -1 and price < mean:
                    position = 0
            out.append(position)
        return out
    if name == "macd":
        fast, slow, signal = int(params["fast"]), int(params["slow"]), int(params["signal"])
        if fast >= slow or len(closes) <= slow + signal:
            return None
        spread = [left - right for left, right in zip(_ema(closes, fast), _ema(closes, slow))]
        line = _ema(spread, signal)
        position = 0
        out = []
        for i in range(len(closes)):
            if i >= slow + signal:
                if spread[i] > line[i]:
                    position = 1
                elif spread[i] < line[i]:
                    position = -1
            out.append(position)
        return out
    if name == "supertrend":
        length, mult = int(params["length"]), float(params["mult"])
        atr = _atr(bars, length)
        if not atr:
            return None
        position = 0
        out = []
        for i, bar in enumerate(bars):
            if atr[i]:
                mid = (bar["h"] + bar["l"]) / 2
                if bar["c"] > mid + mult * atr[i]:
                    position = 1
                elif bar["c"] < mid - mult * atr[i]:
                    position = -1
            out.append(position)
        return out
    return None


def _stop_amount(params: dict) -> float | None:
    raw = params.get("stop") if isinstance(params, dict) else None
    if raw in (None, "", 0):
        return None
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _simulate(bars: list[dict], desired: list[int], qty: int, point: float, account: float, stop: float | None = None) -> dict:
    from futuresfund.prop_rules import LOCK_BUFFER, for_account, passes

    tier = for_account(account)
    allowance = float(tier["max_drawdown"])
    equity = account
    peak = account
    floor = account - allowance
    max_dd = 0.0
    position = 0
    trades = 0
    wins = 0
    open_pnl = 0.0
    breached = False
    closes = [bar["c"] for bar in bars]
    for i in range(len(closes) - 1):
        target = desired[i]
        if stop and target != 0 and position == 0 and equity - (peak - allowance) < stop:
            target = 0
        if target != position:
            if position != 0 and open_pnl > 0:
                wins += 1
            trades += 1
            equity -= _COST * qty * abs(target - position)
            position = target
            open_pnl = 0.0
        change = (closes[i + 1] - closes[i]) * position * point * qty
        stopped = False
        if position != 0 and stop and change < 0:
            room = max(0.0, stop + open_pnl)
            if -change > room:
                change = -room
                stopped = True
            cushion = peak - allowance + (_COST * qty) + 1
            if equity + change <= cushion:
                change = max(change, cushion - equity)
                stopped = True
        open_pnl += change
        equity += change
        if stopped:
            if open_pnl > 0:
                wins += 1
            trades += 1
            equity -= _COST * qty
            position = 0
            open_pnl = 0.0
        peak = max(peak, equity)
        if peak < account + allowance + LOCK_BUFFER:
            floor = peak - allowance
        else:
            floor = account + LOCK_BUFFER
        max_dd = max(max_dd, peak - equity)
        if equity <= floor:
            breached = True
        if equity <= 0:
            breached = True
            break
    if position != 0 and open_pnl > 0:
        wins += 1
    net = round(equity - account, 2)
    return {
        "net_profit": net,
        "max_drawdown": round(max_dd, 2),
        "trades": trades,
        "win_rate": round(wins / trades, 4) if trades else None,
        "reached_target": False,
        "proven": passes(account, net_profit=net, max_drawdown=max_dd, trades=trades, breached=breached, qty=qty),
        "drawdown_limit": allowance,
        "breached": breached,
        "trail_floor": round(floor, 2),
        "bars": len(bars),
        "ending_equity": round(equity, 2),
    }


def mark_target(result: dict, profit_target: float) -> dict:
    backtest = dict(result.get("backtest") or {})
    backtest["reached_target"] = float(backtest.get("net_profit") or 0) >= profit_target
    result["backtest"] = backtest
    return result


def _size(bars: list[dict], account: float, point: float) -> int:
    from futuresfund.config import max_qty

    ranges = []
    for earlier, later in zip(bars, bars[1:]):
        ranges.append(max(later["h"] - later["l"], abs(later["h"] - earlier["c"]), abs(later["l"] - earlier["c"])))
    atr = sum(ranges[-14:]) / max(1, min(14, len(ranges)))
    from futuresfund.prop_rules import for_account

    risk = account * 0.01
    raw = int(risk / max(atr * point, 0.01))
    cap = min(max_qty(), int(for_account(account)["max_contracts"]))
    return max(1, min(cap, raw or 1))


def _atr(bars: list[dict], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    previous = None
    window: list[float] = []
    smoothed = None
    for i, bar in enumerate(bars):
        if previous is None:
            tr = bar["h"] - bar["l"]
        else:
            tr = max(bar["h"] - bar["l"], abs(bar["h"] - previous), abs(bar["l"] - previous))
        previous = bar["c"]
        window.append(tr)
        if smoothed is None:
            if len(window) == length:
                smoothed = sum(window) / length
                out[i] = smoothed
        else:
            smoothed = (smoothed * (length - 1) + tr) / length
            out[i] = smoothed
    return out


def _ema(values: list[float], length: int) -> list[float]:
    weight = 2 / (length + 1)
    line = []
    acc = values[0]
    for value in values:
        acc = value * weight + acc * (1 - weight)
        line.append(acc)
    return line


def _rsi(values: list[float], length: int) -> list[float]:
    if len(values) <= length:
        return []
    gains = []
    losses = []
    for earlier, later in zip(values, values[1:]):
        change = later - earlier
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    line = [50.0] * length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        if avg_loss == 0:
            line.append(100.0)
        else:
            line.append(100 - (100 / (1 + avg_gain / avg_loss)))
    line.append(line[-1])
    return line


def _formula(name: str, params: dict) -> str:
    if name == "ema_cross":
        return (
            f"Long when the {params['fast']}-bar average is above the {params['slow']}-bar average. "
            f"Short when it is below. Hold that side until the averages cross."
        )
    if name == "donchian":
        return (
            f"Long when price breaks the prior {params['length']}-bar high. "
            f"Short when it breaks the prior {params['length']}-bar low. Hold until the opposite break."
        )
    if name == "poc_pullback":
        return (
            f"Point-of-contact pullback over {params['lookback']} bars and {params['rows']} price rows. "
            "Long when price tags the contact band from above and closes back up through the volume average. "
            "Short when it tags the band from below and closes back down."
        )
    if name == "vwap_side":
        return (
            f"Long while price is above the {params['length']}-bar volume-weighted price. "
            "Short while it is below."
        )
    if name == "bollinger":
        return (
            f"Long when price closes below the {params['length']}-bar band minus {params['dev']} deviations. "
            f"Short when it closes above the upper band. Exit when price returns to the average."
        )
    if name == "macd":
        return (
            f"Long when the {params['fast']}/{params['slow']} MACD is above its {params['signal']}-bar signal. "
            "Short when it is below."
        )
    if name == "supertrend":
        return (
            f"Long when price is above the {params['length']}-bar ATR band by {params['mult']} times. "
            "Short when it is below that band."
        )
    return (
        f"Long when the {params['period']}-bar RSI is under {params['low']}. "
        f"Short when it is over {params['high']}. Exit a long above 50 and a short below 50."
    )


def _title(name: str, params: dict) -> str:
    if name == "ema_cross":
        return f"Average cross {params['fast']}/{params['slow']}"
    if name == "donchian":
        return f"Range break {params['length']}"
    if name == "poc_pullback":
        return f"Contact pullback {params['lookback']}"
    if name == "vwap_side":
        return f"Volume price side {params['length']}"
    if name == "bollinger":
        return f"Bollinger {params['length']} x{params['dev']}"
    if name == "macd":
        return f"MACD {params['fast']}/{params['slow']}/{params['signal']}"
    if name == "supertrend":
        return f"Supertrend {params['length']} x{params['mult']}"
    return f"RSI revert {params['period']}"


def _sid(name: str, params: dict) -> str:
    tail = "-".join(f"{key}{params[key]}" for key in sorted(params))
    return f"{name}-{tail}"


def _vwma(bars: list[dict], length: int) -> list[float]:
    line = []
    for i in range(len(bars)):
        window = bars[max(0, i - length + 1):i + 1]
        weight = sum((bar.get("v") or 1) for bar in window)
        line.append(sum(bar["c"] * (bar.get("v") or 1) for bar in window) / weight)
    return line


def _poc_band(window: list[dict], rows: int) -> tuple[float | None, float | None]:
    high = max(bar["h"] for bar in window)
    low = min(bar["l"] for bar in window)
    span = high - low
    if span <= 0 or rows < 1:
        return None, None
    step = span / rows
    buckets = [0.0] * rows
    for bar in window:
        weight = bar.get("v") or 1
        start = max(0, min(rows - 1, int((bar["l"] - low) / step)))
        stop = max(0, min(rows - 1, int((bar["h"] - low) / step)))
        share = weight / (stop - start + 1)
        for row in range(start, stop + 1):
            buckets[row] += share
    best = max(range(rows), key=lambda row: buckets[row])
    return low + best * step, low + (best + 1) * step


def _rule_id(rule: dict | None) -> str:
    if not rule:
        return ""
    parts = [str(rule.get("name"))]
    for key in sorted(rule):
        if key != "name":
            parts.append(f"{key}={rule[key]}")
    return "|".join(parts)
