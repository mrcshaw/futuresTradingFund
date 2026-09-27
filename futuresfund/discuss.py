"""The desks propose a rule, the engine backtests it on the bar file, and they revise from the numbers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from futuresfund.board import Board
from futuresfund.charts import chart_paths
from futuresfund.csv_bars import parse_tradingview_csv
from futuresfund.prop_rules import describe, for_account, gate
from futuresfund.research import assemble_book, bar_facts, evaluate_rule, load_bars, save_bars
from futuresfund.roster import ROSTER
from futuresfund.strategy import get_strategy, store_strategy

_SPECS = {
    "ema_cross": {"fast": (5, 30), "slow": (20, 120)},
    "donchian": {"length": (10, 60)},
    "rsi_revert": {"period": (8, 21), "low": (20, 40), "high": (60, 80)},
    "poc_pullback": {"lookback": (30, 100), "rows": (6, 16)},
    "vwap_side": {"length": (10, 60)},
    "bollinger": {"length": (10, 40), "dev": (2, 3)},
    "macd": {"fast": (8, 16), "slow": (20, 40), "signal": (5, 12)},
    "supertrend": {"length": (7, 21), "mult": (2, 4)},
}


def load_chart_file() -> int:
    """Seed the engine from the 15-minute ES export. Leave a series that alerts have already extended."""
    existing = load_bars()
    if existing:
        return 0
    path = chart_paths().get("15m")
    if path is None:
        return 0
    bars = parse_tradingview_csv(path.read_text(encoding="utf-8-sig", errors="replace"))
    save_bars(bars)
    return len(bars)


def parse_proposal(text: str) -> tuple[str, dict]:
    """Read the designer's JSON. Rejects anything the engine cannot backtest."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("The strategy proposal was not JSON.")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("The strategy proposal JSON could not be read.") from exc
    name = str(payload.get("name") or "").strip()
    spec = _SPECS.get(name)
    if not spec:
        raise ValueError(
            "The strategy name must be ema_cross, donchian, rsi_revert, poc_pullback, "
            "vwap_side, bollinger, macd, or supertrend."
        )
    raw = payload.get("params") or {}
    if not isinstance(raw, dict):
        raise ValueError("params must be an object.")
    params = {}
    for key, (low, high) in spec.items():
        if key not in raw:
            raise ValueError(f"The proposal needs {key}.")
        try:
            number = int(float(raw[key]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a whole number.") from exc
        if number < low or number > high:
            raise ValueError(f"{key} must be between {low} and {high}.")
        params[key] = number
    if name == "ema_cross" and params["fast"] >= params["slow"]:
        raise ValueError("The fast average must be shorter than the slow average.")
    if name == "rsi_revert" and params["low"] >= params["high"]:
        raise ValueError("The RSI low must be below the RSI high.")
    if name == "macd" and params["fast"] >= params["slow"]:
        raise ValueError("The MACD fast length must be shorter than the slow length.")
    if raw.get("stop") not in (None, ""):
        try:
            stop = int(float(raw["stop"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("stop must be a whole number of dollars.") from exc
        if stop < 50 or stop > 2000:
            raise ValueError("stop must be between 50 and 2000 dollars.")
        params["stop"] = stop
    return name, params


def run_discussion(board: Board, when: str = "Startup") -> None:
    """Let the desks build a strategy from the bar file, one backtest at a time."""
    if board.busy:
        board.post("System", "A strategy discussion is already running.", kind="system", channel="headquarters")
        return
    load_chart_file()
    bars = load_bars()
    if len(bars) < 80:
        board.post("System", "The 15-minute ES file is not loaded, so the agents cannot backtest a strategy.", kind="error", channel="headquarters")
        return
    current = get_strategy() or {}
    account = str(current.get("account") or "APEX4415870000042")
    contract = str(current.get("contract") or "ES1!")
    timeframe = str(current.get("timeframe") or "15m")
    account_size = float(current.get("account_size") or 50000)
    profit_target = float(current.get("profit_target") or 3000)
    previous = int(current.get("bar_count") or 0)
    added = max(0, len(bars) - previous) if previous else 0
    board.begin_run(contract, when, ["researcher", "quant_trader"])
    board.post(
        "System",
        f"{when}: the discussion uses {len(bars)} continuous bars"
        + (f", including {added} added by alerts since the last meeting." if added else "."),
        kind="system",
        channel="headquarters",
    )
    board.post("Risk Manager", describe(account_size), kind="report", channel="Risk Manager")
    _compare_saved(board, current, contract)
    try:
        facts = bar_facts(bars)
        transcript = [f"Bar file: {facts}" + (f" Alerts added {added} bars since the last meeting." if added else "")]
        market = _speak(
            board,
            "Quantitative Researcher",
            "You are the quantitative researcher for ES futures. Think through these bars and say what a systematic rule should use. "
            "Entry and exit must be different conditions. Use only these facts. Do not invent a profit or a drawdown.\n" + facts,
        )
        transcript.append(f"Quantitative Researcher: {market}")
        board.post("Quantitative Researcher", market, kind="speech", channel="R&D")
        indicator = _speak(
            board,
            "Indicator Researcher",
            "You are the indicator researcher for ES futures. Think about one public indicator and the settings the trader should test. "
            "Name the entry and a different exit. Use only these facts. Do not invent a profit or a drawdown.\n" + facts,
        )
        transcript.append(f"Indicator Researcher: {indicator}")
        board.post("Indicator Researcher", indicator, kind="speech", channel="R&D")
        proposals: list[dict] = []
        tested: set[str] = set()
        round_number = 0
        while len(proposals) < 3:
            round_number += 1
            if board.cancel.is_set():
                board.finish_run("stopped")
                board.post("System", "Stop requested. The strategy was not changed.", kind="system", channel="headquarters")
                return
            proposal_text = _speak(board, "Quantitative Trader", _designer_prompt(transcript, round_number))
            board.post("Quantitative Trader", proposal_text, kind="speech", channel="R&D")
            board.post(
                "Quantitative Trader",
                "Developer, implement this rule and run it on the engine.",
                kind="speech",
                channel="Quantitative Developer",
            )
            name, params = _read_proposal(board, proposal_text)
            if name and _sid(name, params) in tested:
                proposal_text = _speak(
                    board,
                    "Quantitative Trader",
                    "That rule was already backtested. Choose a different name or different parameters. Reply with JSON only.",
                )
                name, params = _read_proposal(board, proposal_text)
            if name:
                tested.add(_sid(name, params))
            row, note = _developer_backtest(board, bars, contract, account_size, profit_target, name, params, round_number)
            transcript.append(note)
            analyst = _speak(board, "Trading Analyst", _analyst_prompt(transcript))
            researcher_note = _speak(board, "Quantitative Researcher", _review_prompt(transcript))
            indicator_note = _speak(board, "Indicator Researcher", _indicator_prompt(transcript))
            transcript.append(f"Trading Analyst: {analyst}")
            transcript.append(f"Quantitative Researcher: {researcher_note}")
            transcript.append(f"Indicator Researcher: {indicator_note}")
            risk = _speak(board, "Risk Manager", _risk_prompt(transcript, row))
            transcript.append(f"Risk Manager: {risk}")
            if _risk_approves(risk, row):
                proposals = [item for item in proposals if item["id"] != row["id"]]
                proposals.append(row)
                row["accepted"] = True
                board.post("Risk Manager", f"Approved {row['title']}. {len(proposals)} of 3 for the 8:00am meeting.", kind="report", channel="Risk Manager")
                _stand_in(board, proposals, contract, timeframe, account, account_size, profit_target, current)
            else:
                title = row["title"] if row else (name or "the proposal")
                board.post("Risk Manager", f"Denied {title}. The loop continues until three rules pass.", kind="report", channel="Risk Manager")
        if board.cancel.is_set():
            board.finish_run("stopped")
            board.post("System", "Stop requested. The strategy was not changed.", kind="system", channel="headquarters")
            return
        _stand_in(board, proposals, contract, timeframe, account, account_size, profit_target, current)
        board.post(
            "Quantitative Researcher",
            "Three rules passed risk. They wait on the book until the next meeting. "
            "That is 8:00am Monday through Friday or 5:00pm Sunday through Friday. Saturday has no meeting.",
            kind="report",
            channel="headquarters",
        )
        board.finish_run("done")
    except Exception as exc:
        board.finish_run("error", str(exc))
        board.post("Portfolio Manager", f"The strategy discussion stopped: {exc}. The previous book was left as it was.", kind="error", channel="Portfolio Manager")


def choose_standin(approved: list[dict], already_active: bool) -> dict | None:
    """Until the 8:00am meeting, the most profitable rule that passed risk stands in when none is active."""
    if already_active or not approved:
        return None
    return max(approved, key=lambda row: float((row.get("backtest") or {}).get("net_profit") or 0))


def _stand_in(board: Board, approved: list[dict], contract: str, timeframe: str, account: str, account_size: float, profit_target: float, current: dict) -> None:
    if not approved:
        return
    prior = get_strategy() or {}
    already = any(item.get("active") for item in prior.get("strategies") or [])
    book = assemble_book(approved, contract, timeframe, account, account_size, profit_target)
    pick = choose_standin(approved, already)
    active_ids = {item.get("id") for item in prior.get("strategies") or [] if item.get("active")}
    for item in book["strategies"]:
        item["accepted"] = True
        item["active"] = item.get("id") in active_ids or (pick is not None and item.get("id") == pick.get("id"))
        gate(item, account_size)
    book["yahoo"] = current.get("yahoo") or "ES=F"
    book["bar_count"] = len(load_bars())
    from futuresfund.research import sync_lead
    sync_lead(book)
    store_strategy(book)
    if pick is not None:
        board.post(
            "Quantitative Researcher",
            f"No strategy was active. Until the 8:00am meeting, {pick['title']} is active. "
            f"It is the most profitable rule that passed risk, profit {pick['backtest']['net_profit']}, "
            f"drawdown {pick['backtest']['max_drawdown']}.",
            kind="report",
            channel="Quantitative Researcher",
        )


def hold_meeting(board: Board, when: str = "8:00am") -> None:
    """The portfolio manager leads the vote. This runs at 8:00am, not during the research loop."""
    if board.busy:
        board.post("System", "A discussion is already running. The 8:00am meeting waits.", kind="system", channel="headquarters")
        return
    rules = ""
    if when == "5:00pm":
        from futuresfund.account_mail import brief_prop_accounts

        rules = brief_prop_accounts(board)
    current = get_strategy() or {}
    eligible = [item for item in current.get("strategies") or [] if item.get("proven") and item.get("accepted")]
    board.begin_run(str(current.get("contract") or "ES1!"), when, ["portfolio"])
    if len(eligible) < 3:
        board.post(
            "Portfolio Manager",
            f"The {when} meeting has {len(eligible)} of 3 rules that passed risk. The vote waits until three are ready.",
            kind="report",
            channel="Portfolio Manager",
        )
        _mail_report(board)
        board.finish_run("done")
        return
    try:
        transcript = [
            f"{item['title']}: profit {item['backtest']['net_profit']}, drawdown {item['backtest']['max_drawdown']}, trades {item['backtest']['trades']}"
            for item in eligible
        ]
        _speak(
            board,
            "Portfolio Manager",
            f"You are leading the {when} meeting. Summarize the three rules that passed risk. Do not invent numbers.\n"
            + "\n".join(transcript),
        )
        tally = _vote(board, eligible, transcript)
        verdict = _speak(board, "Portfolio Manager", _manager_prompt(transcript, eligible, tally, rules))
        book = dict(current)
        book["strategies"] = list(current.get("strategies") or [])
        _apply_verdict(book, verdict)
        for item in book["strategies"]:
            if item.get("proven") and not item.get("recommended"):
                item["held"] = True
                item["perceived_profit"] = item["backtest"]["net_profit"]
            elif item.get("recommended"):
                item["held"] = False
        book["last_decision"] = "discussion"
        book["last_meeting"] = when
        from futuresfund.research import sync_lead
        sync_lead(book)
        store_strategy(book)
        chosen = next((item for item in book["strategies"] if item.get("recommended")), None)
        board.post(
            "Portfolio Manager",
            f"The {when} vote closed. Headquarters shows {chosen['title'] if chosen else 'the current stand-in'}.",
            kind="report",
            channel="headquarters",
        )
        _floor_analysis(board, chosen or choose_standin(eligible, False))
        _mail_report(board)
        board.finish_run("done")
    except Exception as exc:
        board.finish_run("error", str(exc))
        board.post("Portfolio Manager", f"The {when} meeting stopped: {exc}.", kind="error", channel="Portfolio Manager")


def _mail_report(board: Board) -> None:
    from futuresfund.mailer import deliver_report

    board.post("Portfolio Manager", deliver_report(), kind="report", channel="Portfolio Manager")


def _developer_backtest(board: Board, bars, contract, account_size, profit_target, name, params, round_number: int) -> tuple[dict | None, str]:
    """The developer implements the rule and runs it. The researchers do not post the engine result."""
    board.set_status("Quantitative Developer", "working")
    board.set_activity("Quantitative Developer", "Implementing the rule and running it on the engine")
    try:
        row = evaluate_rule(bars, contract, account_size, profit_target, name, params) if name else None
    finally:
        board.set_status("Quantitative Developer", "done")
    if row is None:
        note = f"Round {round_number}: the developer could not implement {name or 'the proposal'} on these bars."
    else:
        backtest = row["backtest"]
        limit = for_account(account_size)["max_drawdown"]
        note = (
            f"Engine test of {row['title']}: profit {backtest['net_profit']}, "
            f"drawdown {backtest['max_drawdown']}, trades {backtest['trades']}. "
            f"{'Clears the trailing drawdown.' if row['proven'] else f'Drawdown is above the ${limit:,.0f} trailing limit.'}"
        )
    board.post("Quantitative Developer", note, kind="report", channel="Quantitative Developer")
    board.post("Quantitative Developer", note, kind="report", channel="R&D")
    if row is not None:
        from futuresfund.lab import record_trial

        record_trial(row, note)
    return row, note


def _floor_analysis(board: Board, chosen: dict | None) -> None:
    if chosen is None:
        prompt = (
            "You are the floor trader. Risk did not approve a strategy, so headquarters has no script. "
            "Say what you will watch on the next bar and that you will not send an order."
        )
    else:
        backtest = chosen.get("backtest") or {}
        prompt = (
            "You are the floor trader. Read the strategy the meeting put on headquarters. "
            "Say how you would treat the next bar: place, hold, or flat. Do not send an order. Nothing is live. "
            f"{chosen.get('title')}: profit {backtest.get('net_profit')}, "
            f"drawdown {backtest.get('max_drawdown')}, trades {backtest.get('trades')}. {chosen.get('formula')}"
        )
    _speak(board, "Floor Trader", prompt)


def _sid(name: str, params: dict) -> str:
    tail = "-".join(f"{key}{params[key]}" for key in sorted(params))
    return f"{name}-{tail}"


def _read_proposal(board: Board, text: str) -> tuple[str, dict]:
    try:
        return parse_proposal(text)
    except ValueError as exc:
        return parse_proposal(_speak(
            board,
            "Quantitative Trader",
            f"That proposal could not be tested: {exc}. Reply with one JSON object only.",
        ))


def _apply_verdict(book: dict, verdict: str) -> None:
    match = re.search(r"\{.*\}", verdict or "", re.DOTALL)
    if not match:
        return
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return
    chosen = str(payload.get("arm") or "").strip()
    for item in book["strategies"]:
        item["recommended"] = bool(
            chosen not in {"", "none", "null"} and item["id"] == chosen and item.get("proven")
        )


def _designer_prompt(transcript: list[str], round_number: int) -> str:
    heard = "\n".join(transcript[-8:])
    return (
        "You are the quantitative trader. Propose one systematic futures rule the engine can backtest. "
        "Reply with JSON only, shaped as "
        '{"name":"ema_cross","params":{"fast":12,"slow":50}}. '
        "Allowed names: ema_cross (fast 5-30, slow 20-120, fast shorter than slow), "
        "donchian (length 10-60), rsi_revert (period 8-21, low 20-40, high 60-80), "
        "poc_pullback (lookback 30-100, rows 6-16), vwap_side (length 10-60), "
        "bollinger (length 10-40, dev 2-3), macd (fast 8-16, slow 20-40, signal 5-12), "
        "supertrend (length 7-21, mult 2-4). "
        f"This is proposal {round_number}. The loop continues until three rules pass risk. "
        "The vote is at 8:00am Monday through Friday or 5:00pm Sunday through Friday. Saturday has no meeting. "
        "Include a stop in dollars, between 50 and 2000, or the trailing drawdown will fail. "
        'Example: {"name":"bollinger","params":{"length":14,"dev":2,"stop":250}}. '
        "Use the latest backtest and the analyst and risk remarks. "
        "If a backtest lost money or the drawdown was too deep, you must change the name or the parameters. "
        "Do not send a rule that was already tested.\n"
        f"{heard}"
    )


def _analyst_prompt(transcript: list[str]) -> str:
    heard = "\n".join(transcript[-6:])
    return (
        "You are the trading analyst. Think through whether this backtest is fit for the prop account. "
        "Use the profit, drawdown, and trade count. Do not invent numbers. "
        "A rule with one trade is not a strategy. Entry and exit must be different conditions.\n"
        f"{heard}"
    )


def _indicator_prompt(transcript: list[str]) -> str:
    heard = "\n".join(transcript[-6:])
    return (
        "You are the indicator researcher. Think about whether these settings are worth another test. "
        "Say what you would change on the next proposal. Use only the numbers above. Do not invent numbers.\n"
        f"{heard}"
    )


def _review_prompt(transcript: list[str]) -> str:
    heard = "\n".join(transcript[-6:])
    return (
        "You are the quantitative researcher. Read the engine result and say whether this rule should be proposed at the meeting. "
        "Use only the profit, drawdown, and trade count above. Do not invent numbers.\n"
        f"{heard}"
    )


def _risk_prompt(transcript: list[str], row: dict | None) -> str:
    heard = "\n".join(transcript[-6:])
    facts = "The engine has no result." if not row else (
        f"Engine result: profit {row['backtest']['net_profit']}, "
        f"drawdown {row['backtest']['max_drawdown']}, trades {row['backtest']['trades']}, "
        f"proven {row.get('proven')}."
    )
    return (
        "You are the risk manager. This review is before the meeting. "
        "Approve the rule only if profit is positive and the drawdown is inside the trailing limit. "
        "Otherwise deny it. Use only the engine numbers. Do not invent numbers. "
        "End with the single word APPROVE or DENY.\n"
        f"{facts}\n{heard}"
    )


def _risk_approves(text: str, row: dict | None) -> bool:
    """The engine trail is the fact. A model approval cannot pass a rule the backtest failed."""
    if not row or not row.get("proven"):
        return False
    words = (text or "").upper().replace(".", " ").split()
    if "DENY" in words:
        return False
    return "APPROVE" in words


def _ballot_prompt(name: str, lines: list[str]) -> str:
    return (
        f"You are {name}. These rules already cleared the prop trailing floor. "
        "Pick one. Score it from 1 to 5. Name one pro or one con in a single sentence. "
        'Reply with JSON only: {"pick":"the id","score":4,"note":"one sentence"}.\n'
        + "\n".join(lines)
    )


def _manager_prompt(transcript: list[str], strategies: list[dict], tally: dict[str, int], rules: str = "") -> str:
    lines = [
        f"{item['id']}: profit {item['backtest']['net_profit']}, drawdown {item['backtest']['max_drawdown']}, votes {tally.get(item['id'], 0)}"
        for item in strategies
    ]
    heard = "\n".join(transcript[-12:])
    return (
        "You are the portfolio manager. Weigh the vote and the profit against the drawdown. "
        "Recommend the one rule that best fits the prop account. Recommend none if the list is empty. "
        "A recommendation is not live. The user turns it on. The rules you do not pick stay saved for the next meeting. "
        'End with JSON: {"arm":"the id"} or {"arm":"none"}.\n'
        + "\n".join(lines)
        + "\n"
        + heard
        + (("\nMention these prop-account rules. Do not change the numbers.\n" + rules) if rules else "")
    )


def _compare_saved(board: Board, current: dict, contract: str) -> None:
    from futuresfund.book import load
    from futuresfund.research import live_pnl

    saved = [
        item for item in (current.get("strategies") or [])
        if item.get("held") or item.get("active") or item.get("recommended")
    ]
    if not saved:
        return
    intervals = load().get("intervals") or []
    live = live_pnl(intervals, contract) if intervals else None
    for item in saved:
        perceived = item.get("perceived_profit", (item.get("backtest") or {}).get("net_profit"))
        if item.get("active"):
            board.post(
                "Portfolio Manager",
                f"{item['title']} was live. Perceived profit {perceived}. Realized interval P&L {live}.",
                kind="report",
                channel="Portfolio Manager",
            )
        elif item.get("held"):
            board.post(
                "Trading Analyst",
                f"{item['title']} was saved and not traded. Perceived profit {perceived}. No realized result sits beside it.",
                kind="report",
                channel="Trading Analyst",
            )


def _vote(board: Board, eligible: list[dict], transcript: list[str]) -> dict[str, int]:
    lines = [
        f"{item['id']}: {item['title']}, profit {item['backtest']['net_profit']}, drawdown {item['backtest']['max_drawdown']}, trades {item['backtest'].get('trades')}"
        for item in eligible
    ]
    tally = {item["id"]: 0 for item in eligible}
    known = set(tally)
    for agent in ROSTER:
        if agent["name"] == "Portfolio Manager" or agent.get("votes") is False or board.cancel.is_set():
            continue
        raw = _speak(board, agent["name"], _ballot_prompt(agent["name"], lines))
        pick, score = _parse_ballot(raw, known)
        if pick:
            tally[pick] += score
        transcript.append(f"{agent['name']} voted {pick or 'none'} with score {score}.")
    ordered = ", ".join(f"{key} {value}" for key, value in sorted(tally.items(), key=lambda pair: -pair[1]))
    board.post("Portfolio Manager", f"Vote totals: {ordered}.", kind="report", channel="Portfolio Manager")
    return tally


def _parse_ballot(text: str, known: set[str]) -> tuple[str, int]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return "", 0
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "", 0
    pick = str(payload.get("pick") or "").strip()
    try:
        score = int(payload.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    score = max(1, min(5, score)) if pick in known else 0
    return (pick if pick in known else ""), score


def _speak(board: Board, name: str, prompt: str) -> str:
    if board.cancel.is_set():
        return ""
    board.set_status(name, "working")
    try:
        text = _model(prompt)
    finally:
        board.set_status(name, "done")
    if text:
        board.post(name, text, kind="speech", channel=name)
    return text


def _model(prompt: str) -> str:
    from futuresfund.llm import complete

    return complete(prompt)
