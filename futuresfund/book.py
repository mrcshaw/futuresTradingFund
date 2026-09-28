"""Prop-account book. Positions change when CrossTrade accepts an order, not when a desk is still thinking."""

from __future__ import annotations

import json
from datetime import datetime

from pathlib import Path

from futuresfund.config import BOOK_PATH
from futuresfund.contracts import POINT_VALUE, root_of


def load() -> dict:
    if not BOOK_PATH.is_file():
        return _fresh()
    try:
        data = json.loads(BOOK_PATH.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return _fresh()
    if not isinstance(data, dict):
        return _fresh()
    data.setdefault("positions", {})
    data.setdefault("orders", [])
    data.setdefault("chat", [])
    data.setdefault("strategy", None)
    data.setdefault("intervals", [])
    data.setdefault("notices", [])
    data.setdefault("prop_updates", {})
    data.setdefault("headquarters_rules", None)
    return data


def save(book: dict) -> None:
    BOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOOK_PATH.write_text(json.dumps(book, indent=2), encoding="utf-8")


def record_fill(book: dict, signal: dict, side: str, price: float | None) -> dict:
    key = _key(signal["account"], signal["instrument"])
    current = book["positions"].get(key) or {}
    held = signal.get("position")
    if held is None:
        held = float(current.get("contracts") or 0)
    qty = int(signal["qty"])
    if _flatten(signal, side, held):
        contracts = qty if side == "BUY" else -qty
    else:
        contracts = held + (qty if side == "BUY" else -qty)
    contracts = int(contracts) if float(contracts).is_integer() else contracts
    mark = price if price is not None else signal.get("price")
    position = {
        "account": signal["account"],
        "instrument": signal["instrument"],
        "yahoo": signal["yahoo"],
        "contracts": contracts,
        "average_price": mark if contracts else None,
        "last_price": mark,
        "updated": _now(),
    }
    if contracts == 0:
        book["positions"].pop(key, None)
    else:
        book["positions"][key] = position
    return position


def record_interval(book: dict, row: dict) -> dict:
    book.setdefault("intervals", []).append(row)
    book["intervals"] = book["intervals"][-500:]
    save(book)
    return row


def apply_target(book: dict, signal: dict, target: int, price: float | None) -> None:
    key = f"{signal['account']}|{signal['instrument']}"
    old = book["positions"].get(key)
    _realize(book, old, int(target), price, signal.get("instrument") or "")
    if target == 0:
        book["positions"].pop(key, None)
    else:
        book["positions"][key] = {
            "account": signal["account"],
            "instrument": signal["instrument"],
            "yahoo": signal.get("yahoo") or "",
            "contracts": target,
            "average_price": price,
            "last_price": price,
            "updated": _now(),
        }
    _refresh_paper(book, signal.get("account"), signal.get("instrument"))


def record_order(book: dict, signal: dict, rating: str, result: dict) -> dict:
    order = {
        "time": _now(),
        "account": signal["account"],
        "instrument": signal["instrument"],
        "qty": signal["qty"],
        "rating": rating,
        "side": result.get("side"),
        "price": signal.get("price"),
        "bar_time": signal.get("bar_time"),
        "sent": bool(result.get("sent")),
        "dry_run": bool(result.get("dry_run")),
        "reason": result.get("reason") or "",
    }
    book["orders"].append(order)
    book["orders"] = book["orders"][-40:]
    if result.get("sent") and result.get("side") and not result.get("managed"):
        record_fill(book, signal, result["side"], signal.get("price"))
    save(book)
    return order


def snapshot() -> dict:
    book = load()
    positions = []
    for position in book["positions"].values():
        row = dict(position)
        row["pnl"] = _pnl(row)
        positions.append(row)
    marks = _marks(positions)
    for row in positions:
        live = marks.get(row["yahoo"])
        if live is not None:
            row["last_price"] = live
            row["pnl"] = _pnl(row)
    from futuresfund.strategy import lead_script

    return {
        "positions": positions,
        "orders": list(reversed(book["orders"][-20:])),
        "strategy": _public_strategy(book.get("strategy")),
        "lead": lead_script(),
        "intervals": list(reversed(book.get("intervals", [])[-15:])),
        "interval_count": len(book.get("intervals") or []),
        "headquarters_rules": book.get("headquarters_rules"),
        "prop_updates": book.get("prop_updates") or {},
        "paper": paper_status(book),
        "active_strategies": active_strategies(book),
        "leaders": _leaders(),
        "running": list(book.get("running") or []),
        "account_rules": _account_rules(book),
        "next_meeting": "8:00am ET Monday through Friday, and 5:00pm ET Sunday through Friday. Saturday has no meeting.",
    }


def _public_strategy(strategy):
    if not isinstance(strategy, dict):
        return strategy
    shown = dict(strategy)
    shown["strategies"] = [
        {key: value for key, value in item.items() if key != "pine"}
        for item in strategy.get("strategies") or []
    ]
    return shown


def _marks(positions: list[dict]) -> dict:
    symbols = sorted({row["yahoo"] for row in positions if row.get("yahoo")})
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}
    marks = {}
    for symbol in symbols:
        try:
            history = yf.Ticker(symbol).history(period="1d")
            if history is not None and not history.empty:
                marks[symbol] = float(history["Close"].iloc[-1])
        except Exception:
            continue
    return marks


def mark_paper(book: dict, signal: dict) -> dict:
    """Move every open trade in this contract to the latest price."""
    price = signal.get("price")
    instrument = signal.get("instrument")
    if price is not None:
        for position in (book.get("positions") or {}).values():
            if instrument and position.get("instrument") != instrument:
                continue
            position["last_price"] = price
            position["updated"] = _now()
    _refresh_paper(book, signal.get("account"), signal.get("instrument"))
    save(book)
    return paper_status(book)


def set_running(strategy_id: str, account: str, enabled: bool) -> list[dict]:
    """Arm or stop one of the three headquarters strategies. Two can run, even on one account."""
    from futuresfund.strategy import consistent_leaders, pine_text

    book = load()
    rows = [dict(row) for row in book.get("running") or [] if isinstance(row, dict)]
    strategy_id = str(strategy_id or "").strip()
    account = str(account or "").strip()
    if not enabled:
        book["running"] = [row for row in rows if row.get("id") != strategy_id]
        _sync_running_scripts(book)
        save(book)
        return book["running"]
    if not account:
        raise ValueError("Type the account this strategy should trade.")
    leader = next((item for item in consistent_leaders(3) if item["id"] == strategy_id), None)
    if leader is None:
        raise ValueError("That strategy is not one of the three on headquarters.")
    others = [row for row in rows if row.get("id") != strategy_id]
    if len(others) >= 2:
        raise ValueError("Two strategies are already running. Stop one before starting another.")
    others.append({
        "id": leader["id"],
        "title": leader["title"],
        "file": leader.get("file") or "",
        "account": account,
        "timeframe": leader.get("timeframe") or "5m",
        "net_profit": leader.get("net_profit"),
        "max_drawdown": leader.get("max_drawdown"),
        "trades": leader.get("trades"),
        "charts": leader.get("charts") or "",
    })
    book["running"] = others
    _sync_running_scripts(book, pine_text)
    save(book)
    return others


def _sync_running_scripts(book: dict, pine_text=None) -> None:
    """The Active strategy tab shows the strategies that are running, each with its account."""
    running = [row for row in book.get("running") or [] if isinstance(row, dict)]
    running_ids = {row.get("id") for row in running}
    kept = []
    for row in book.get("active_strategies") or []:
        if not isinstance(row, dict):
            continue
        if row.get("source") == "headquarters" and row.get("id") not in running_ids:
            continue
        kept.append(row)
    for row in running:
        text = ""
        if pine_text is not None:
            text = pine_text(str(row.get("file") or ""))
        card = {
            "id": row.get("id"),
            "account": row.get("account") or "paper",
            "title": row.get("title") or "Strategy",
            "text": text,
            "locked": True,
            "source": "headquarters",
        }
        existing = next((item for item in kept if item.get("id") == row.get("id")), None)
        if existing:
            if not text:
                card["text"] = existing.get("text") or ""
            existing.update(card)
        else:
            kept.append(card)
    book["active_strategies"] = kept


def _leaders() -> list[dict]:
    from futuresfund.strategy import consistent_leaders

    return consistent_leaders(3)


def active_strategies(book: dict | None = None) -> list[dict]:
    """Strategies the floor trader follows. Paper is one of the accounts."""
    book = book if book is not None else load()
    rows = book.get("active_strategies")
    if isinstance(rows, list):
        return rows
    text = ""
    path = Path(__file__).resolve().parent / "learnedStrategies" / "overnight_drift_capture.pine"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    book["active_strategies"] = [{
        "id": "overnight-drift",
        "account": "paper",
        "title": "Overnight Drift Capture [Optimized]",
        "text": text,
        "locked": False,
    }]
    save(book)
    return book["active_strategies"]


def save_active_strategies(rows: list[dict]) -> list[dict]:
    book = load()
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kept.append({
            "id": str(row.get("id") or _now()),
            "account": str(row.get("account") or "paper"),
            "title": str(row.get("title") or "Strategy"),
            "text": str(row.get("text") or ""),
            "locked": bool(row.get("locked")),
        })
    book["active_strategies"] = kept
    _arm_levels(book)
    save(book)
    return kept


def strategy_exit(text: str, entry: float | None, contracts: int, high: float | None, low: float | None) -> dict | None:
    """Stop and take profit from the strategy text, applied to the open trade."""
    if entry is None or not contracts:
        return None
    labeled = _labeled_inputs(text or "")
    take = _pick_level(labeled, ("take profit",))
    stop = _pick_level(labeled, ("max stop",)) or _pick_level(labeled, ("stop",))
    point = _pick_level(labeled, ("point value",)) or 50.0
    if take is None and stop is None:
        return None
    stop_px = None
    target_px = None
    if stop is not None and point:
        distance = float(stop) / float(point)
        stop_px = float(entry) - distance if contracts > 0 else float(entry) + distance
    if take is not None and point:
        distance = float(take) / float(point)
        target_px = float(entry) + distance if contracts > 0 else float(entry) - distance
    hit = None
    if contracts > 0:
        if stop_px is not None and low is not None and float(low) <= stop_px:
            hit = "stop"
        elif target_px is not None and high is not None and float(high) >= target_px:
            hit = "target"
    else:
        if stop_px is not None and high is not None and float(high) >= stop_px:
            hit = "stop"
        elif target_px is not None and low is not None and float(low) <= target_px:
            hit = "target"
    return {"stop": stop_px, "take_profit": target_px, "hit": hit}


def _labeled_inputs(text: str) -> list[tuple[str, float]]:
    import re

    found = []
    for match in re.finditer(r"input\.float\(\s*([0-9.]+)\s*,\s*\"([^\"]+)\"", text):
        found.append((match.group(2).lower(), float(match.group(1))))
    return found


def _pick_level(labeled: list[tuple[str, float]], needles: tuple[str, ...]) -> float | None:
    for label, value in labeled:
        if any(needle in label for needle in needles):
            return value
    return None


def _arm_levels(book: dict) -> None:
    """Write the submitted strategy's stop and take profit onto the open paper trade."""
    rows = [row for row in book.get("active_strategies") or [] if row.get("locked") and row.get("text")]
    if not rows:
        return
    text = rows[0]["text"]
    for position in (book.get("positions") or {}).values():
        try:
            contracts = int(position.get("contracts") or 0)
        except (TypeError, ValueError):
            continue
        levels = strategy_exit(text, position.get("average_price"), contracts, None, None)
        if not levels:
            continue
        position["stop"] = levels["stop"]
        position["take_profit"] = levels["take_profit"]


def save_account_rules(row: dict) -> dict:
    """Store the account the firm is trading and the rules the floor trader must watch."""
    try:
        size = float(row.get("size"))
        profit_target = float(row.get("profit_target"))
        drawdown = float(row.get("max_drawdown"))
        contracts = int(row.get("max_contracts"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Account size, profit target, drawdown, and contracts must be numbers.") from exc
    if size <= 0 or profit_target <= 0 or drawdown <= 0 or contracts < 1:
        raise ValueError("Account size, profit target, drawdown, and contracts must be greater than zero.")
    saved = {
        "account": str(row.get("account") or "").strip(),
        "size": size,
        "profit_target": profit_target,
        "max_drawdown": drawdown,
        "trailing": bool(row.get("trailing", True)),
        "max_contracts": contracts,
    }
    book = load()
    book["account_rules"] = saved
    save(book)
    from futuresfund.prop_rules import rules_sentence

    book = load()
    book["headquarters_rules"] = {"text": rules_sentence(size), "source": "account"}
    strategy = book.get("strategy")
    if isinstance(strategy, dict):
        strategy["account_size"] = size
        strategy["profit_target"] = profit_target
        book["strategy"] = strategy
    save(book)
    return saved


def _account_rules(book: dict) -> dict:
    from futuresfund.prop_rules import active_limits

    size = None
    strategy = book.get("strategy")
    if isinstance(strategy, dict) and strategy.get("account_size"):
        size = strategy.get("account_size")
    return active_limits(size)


def firm_record(view: dict | None = None) -> str:
    """What the portfolio manager can read about the firm. No secrets and no invented results."""
    view = view if view is not None else snapshot()
    paper = view.get("paper") or {}
    strategy = view.get("strategy") if isinstance(view.get("strategy"), dict) else {}
    lines = [
        "Futures desk record. Chat does not place an order.",
        _rules_line(view),
        "Headquarters shows paper P&L, three consistent strategies, and the lead script. Click a strategy name to copy its Pine script.",
        "Strategy library is its own tab. Profitable Pine scripts are filed by contract. ES and gold are separate. Each row is the most profitable timeframe of that engine run. Click a row to copy the script.",
        "Two strategies can run at once, including on the same account. An order alert must name the strategy. The account in that message is where the order goes.",
        "A price alert with id desk-bar records the candle and does not send an order.",
        "The stop is defined in the Pine strategy. TradingView sends the buy or sell, including the stop exit. The floor trader turns that alert into the CrossTrade webhook. CrossTrade places the trade.",
        "Research starts only when Start is pressed. It does not start on its own.",
        str(view.get("next_meeting") or ""),
    ]
    from futuresfund.config import public_settings

    settings = public_settings()
    if settings.get("dry_run"):
        lines.append("Dry run is on. The floor trader builds the order and does not send it.")
    else:
        lines.append("Dry run is off. An order that matches a running strategy is sent.")
    names = ", ".join(settings.get("accounts") or []) or "none"
    lines.append(f"Configured account names: {names}. A running strategy can use any account named in its alert, including paper.")
    lines.append(
        f"Paper equity {paper.get('equity')}. Active P&L {paper.get('active_pnl')}. "
        f"Drawdown {paper.get('drawdown')}. Peak {paper.get('peak')}."
    )
    positions = view.get("positions") or []
    if positions:
        for row in positions:
            lines.append(
                f"Open position: {row.get('account')} {row.get('instrument')} "
                f"{row.get('contracts')} from {row.get('average_price')}, last {row.get('last_price')}, P&L {row.get('pnl')}."
            )
    else:
        lines.append("No position is open.")
    running = view.get("running") or []
    if running:
        for row in running:
            lines.append(f"Running strategy: {row.get('title')} on {row.get('account')}, timeframe {row.get('timeframe')}.")
    else:
        lines.append("No strategy is running.")
    for row in view.get("leaders") or []:
        lines.append(
            f"Headquarters strategy: {row.get('title')}, {row.get('timeframe')}, "
            f"profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}. {row.get('charts')}."
        )
    lead = view.get("lead") or {}
    if lead.get("title"):
        lines.append(
            f"Lead script on headquarters: {lead.get('title')}, {lead.get('timeframe')}, "
            f"profit {lead.get('net_profit')}, drawdown {lead.get('max_drawdown')}, trades {lead.get('trades')}."
        )
    if strategy.get("contract") or strategy.get("timeframe"):
        lines.append(f"Book contract {strategy.get('contract') or 'unset'}, timeframe {strategy.get('timeframe') or 'unset'}.")
    from futuresfund.library import search_library

    for shelf in ("ES1!", "GC1!"):
        found = search_library(shelf)
        label = "ES" if shelf == "ES1!" else "Gold"
        rows = found.get("strategies") or []
        lines.append(f"Library {label}: {len(rows)} profitable scripts.")
        for row in rows[:8]:
            lines.append(
                f"Library {label} {row.get('title')}: {row.get('timeframe')}, "
                f"profit {row.get('net_profit')}, drawdown {row.get('max_drawdown')}, trades {row.get('trades')}."
            )
    try:
        from futuresfund.research import load_bars

        lines.append(f"Stored chart bars: {len(load_bars())}.")
    except Exception:
        pass
    rules = (view.get("headquarters_rules") or {}).get("text")
    if rules:
        lines.append(f"Prop rules recorded at the meeting: {str(rules)[:500]}")
    updates = view.get("prop_updates") or {}
    if updates:
        lines.append(f"Prop updates on the book: {updates}.")
    orders = view.get("orders") or []
    if orders:
        lines.append("Recent orders:")
        for order in orders[:5]:
            lines.append(
                f"{order.get('time')} {order.get('account')} {order.get('instrument')} "
                f"{order.get('side') or order.get('rating')}: {str(order.get('reason') or '')[:160]}"
            )
    return "\n".join(line for line in lines if line)


def _rules_line(view: dict) -> str:
    rules = view.get("account_rules") or {}
    if not rules:
        return ""
    kind = "trailing drawdown" if rules.get("trailing", True) else "drawdown"
    name = rules.get("account") or "The account"
    return (
        f"Account rules: {name}, size {rules.get('size')}, profit target {rules.get('profit_target')}, "
        f"{kind} {rules.get('max_drawdown')}, max contracts {rules.get('max_contracts')}."
    )


def open_trade_brief(view: dict) -> str:
    """Stop and take profit for the open paper trade. Missing prices are not invented."""
    positions = view.get("positions") or []
    paper = view.get("paper") or {}
    if not positions:
        return "No trade is open on the paper book."
    lines = []
    for row in positions:
        side = "long" if float(row.get("contracts") or 0) > 0 else "short"
        lines.append(
            f"The open paper trade is {side} {abs(float(row.get('contracts') or 0)):g} "
            f"{row.get('instrument')} from {row.get('average_price')}. "
            f"Last price {row.get('last_price')}. Active P&L {row.get('pnl')}."
        )
    lines.append(
        f"Current drawdown is {paper.get('drawdown')}. Equity is {paper.get('equity')}."
    )
    stop = None
    target = None
    for row in positions:
        stop = row.get("stop") if row.get("stop") is not None else stop
        target = row.get("take_profit") if row.get("take_profit") is not None else target
    if stop is None and target is None:
        lines.append(
            "This fill did not include a stop price or a take-profit price, so those live levels are not on the book."
        )
    else:
        lines.append(f"Stop on the book is {stop}. Take profit on the book is {target}.")
    levels = _script_levels((view.get("lead") or {}).get("pine") or "")
    if levels:
        title = (view.get("lead") or {}).get("title") or "The headquarters script"
        lines.append(f"{title} is written with {levels}. That is the script, not a live stop price.")
    return " ".join(lines)


def _script_levels(pine: str) -> str:
    if not pine:
        return ""
    import re

    found = []
    for match in re.finditer(r"input\.float\(\s*([0-9.]+)\s*,\s*\"([^\"]+)\"", pine):
        label = match.group(2)
        lowered = label.lower()
        if "stop" in lowered or "take profit" in lowered:
            found.append(f"{label} {match.group(1)}")
    return ", ".join(found)


def reset_paper() -> dict:
    """Clear the paper result. Those fills were not from a strategy the floor was following."""
    book = load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    size = float((strategy or {}).get("account_size") or 50000)
    book["paper"] = {"realized": 0.0, "peak": size}
    book["positions"] = {}
    if isinstance(strategy, dict):
        strategy["working"] = None
        strategy["positions_working"] = None
        book["strategy"] = strategy
    save(book)
    return paper_status(book)


def paper_status(book: dict | None = None) -> dict:
    """Open P&L and drawdown from the paper account. Nothing is sent to CrossTrade."""
    book = book if book is not None else load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    size = float((strategy or {}).get("account_size") or 50000)
    paper = book.get("paper") if isinstance(book.get("paper"), dict) else {}
    realized = float(paper.get("realized") or 0)
    active = 0.0
    for position in (book.get("positions") or {}).values():
        pnl = _pnl(position)
        if pnl is not None:
            active += pnl
    equity = size + realized + active
    peak = max(float(paper.get("peak") or size), equity)
    return {
        "mode": "paper",
        "active_pnl": round(active, 2),
        "drawdown": round(max(0.0, peak - equity), 2),
        "equity": round(equity, 2),
        "peak": round(peak, 2),
    }


def _realize(book: dict, old: dict | None, target: int, price: float | None, instrument: str) -> None:
    if not old or price is None or old.get("average_price") is None:
        return
    try:
        held = int(old.get("contracts") or 0)
    except (TypeError, ValueError):
        return
    if held == 0:
        return
    closed = held if target == 0 or (held > 0) != (target > 0) else held - target
    if closed == 0 or (held > 0 and closed < 0) or (held < 0 and closed > 0):
        return
    try:
        value = POINT_VALUE[root_of(instrument or old.get("instrument") or "")]
    except (ValueError, KeyError):
        return
    gained = (float(price) - float(old["average_price"])) * closed * value
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    size = float((strategy or {}).get("account_size") or 50000)
    paper = book.setdefault("paper", {"realized": 0.0, "peak": size})
    paper["realized"] = round(float(paper.get("realized") or 0) + gained, 2)


def _refresh_paper(book: dict, account: str | None, instrument: str | None) -> None:
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    size = float((strategy or {}).get("account_size") or 50000)
    paper = book.setdefault("paper", {"realized": 0.0, "peak": size})
    status = paper_status(book)
    paper["peak"] = status["peak"]
    paper["realized"] = float(paper.get("realized") or 0)


def _pnl(row: dict):
    avg = row.get("average_price")
    last = row.get("last_price")
    contracts = row.get("contracts") or 0
    if avg is None or last is None or not contracts:
        return None
    try:
        value = POINT_VALUE[root_of(row["instrument"])]
    except (ValueError, KeyError):
        return None
    return round((float(last) - float(avg)) * float(contracts) * value, 2)


def _flatten(signal: dict, side: str, held: float) -> bool:
    if signal.get("flatten_first") is True:
        return True
    if signal.get("flatten_first") is False:
        return False
    if not held:
        return False
    return (held > 0 and side == "SELL") or (held < 0 and side == "BUY")


def _key(account: str, instrument: str) -> str:
    return f"{account}|{instrument}"


def _fresh() -> dict:
    return {"positions": {}, "orders": [], "chat": [], "strategy": None, "intervals": [], "notices": [], "prop_updates": {}, "headquarters_rules": None, "last_morning": "", "last_afternoon": ""}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
