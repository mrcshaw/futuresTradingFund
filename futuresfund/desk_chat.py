"""What a person can ask the desks in chat, and what the Portfolio Manager can do from there.

Questions about the application are answered from the desk's own records, not guessed by the model.
Tasks are done here and confirmed in plain words. Nothing in this file places an order.
"""

from __future__ import annotations

import re
from pathlib import Path

PORTFOLIO_MANAGER = "Portfolio Manager"
ROOT = Path(__file__).resolve().parent
LEARNING = ROOT / "learningStrategies"
LEARNED = ROOT / "learnedStrategies"

_TITLE = re.compile(r"""(?:strategy|indicator)\s*\(\s*(?:title\s*=\s*)?["']([^"'\n]+)["']""")


# Pasted scripts.


def pine_script(message: str) -> str | None:
    """The Pine source inside a message, or None when the message is only words."""
    text = message or ""
    start = text.find("//@version")
    if start < 0:
        if "strategy(" in text and ("strategy.entry" in text or "strategy.close" in text or "strategy.exit" in text):
            start = text.find("strategy(")
            start = text.rfind("\n", 0, start) + 1
        else:
            return None
    return text[start:].strip() or None


def script_title(source: str) -> str:
    found = _TITLE.search(source or "")
    return found.group(1).strip() if found else ""


def slug(title: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")
    return words[:80] or "pasted_strategy"


def file_script(board, message: str, by: str = PORTFOLIO_MANAGER) -> str:
    """Save a pasted Pine strategy into the learning folder so the chart developers pick it up."""
    source = pine_script(message)
    if not source:
        return "I did not find a Pine script in that message. Paste the whole script, starting at //@version."
    if "strategy(" not in source:
        return (
            "That script is an indicator, not a strategy. The learning folder takes Pine strategies, "
            "the ones that call strategy() and strategy.entry(). Add entries and exits and paste it again."
        )
    title = script_title(source) or "Pasted strategy"
    name = slug(title)
    LEARNING.mkdir(parents=True, exist_ok=True)
    existing = _existing_names()
    path = LEARNING / f"{name}.pine"
    number = 2
    while path.stem.lower() in existing or path.exists():
        path = LEARNING / f"{name}_{number}.pine"
        number += 1
    path.write_text(source.rstrip() + "\n", encoding="utf-8")
    try:
        from futuresfund.indicators import record_script

        record_script(path.stem, source, by)
    except Exception:
        pass
    waiting = len(list(LEARNING.glob("*.pine")))
    state = _research_state(board)
    if state == "running":
        pickup = "The researchers are running, so the next free chart developer takes it from the queue."
    else:
        pickup = "The researchers are not running. Press Start on headquarters and the chart developers will study it."
    note = (
        f"Filed {title} as learningStrategies/{path.name}. {waiting} Pine scripts are in the learning folder. {pickup} "
        "Each script is tested on the 2-minute, 5-minute, and 15-minute charts on every research instrument."
    )
    from futuresfund.roster import CHART_DESKS

    for desk in CHART_DESKS:
        board.post(desk["developer"], f"{by} filed {path.name} in the learning folder.", kind="log", channel=desk["developer"])
    return note


def _existing_names() -> set[str]:
    names = set()
    for folder in (LEARNING, LEARNED):
        if folder.is_dir():
            names.update(path.stem.lower() for path in folder.rglob("*.pine"))
    return names


# Facts about the application.


def executing_lines() -> list[str]:
    """Which cards are executing, who runs them, and what the active list holds."""
    from futuresfund.book import active_strategies, load
    from futuresfund.live_trading import executing_view, trading_mode

    book = load()
    lines = [f"Trading mode for new entries: {trading_mode(book)}."]
    running = executing_view(book)
    if running:
        for row in running:
            held = row.get("position")
            trade = (
                f"{held['side']} {held['qty']} from {held['entry']}, last {held['last']}, open P&L {held['pnl']} ({held['mode']})"
                if held else "flat right now"
            )
            lines.append(
                f"{row['trader']} is executing {row['title']} on the {row['timeframe']} {row['contract']} chart "
                f"for account {row['account']} since {row['started']}: {trade}. "
                f"{row['trades']} closed trades, realized {row['realized']}."
            )
    else:
        lines.append("No active strategy is executing. Nothing is being traded from the active list.")
    cards = active_strategies(book)
    if cards:
        names = "; ".join(
            f"{row.get('title') or 'Untitled'} ({row.get('instrument') or 'no contract'}, {row.get('timeframe') or '5m'}, account {row.get('account') or 'paper'})"
            for row in cards
        )
        lines.append(f"Cards on the active strategy page: {len(cards)}. {names}.")
    else:
        lines.append("The active strategy page has no cards.")
    return lines


def research_lines(board) -> list[str]:
    """What the researchers are doing and what waits in the learning folder."""
    from futuresfund.learn import _read_notes, pending_for_chart, queue
    from futuresfund.roster import CHART_DESKS

    state = _research_state(board)
    lines = [f"Research is {state}."]
    if state == "running":
        tasks = board.snapshot().get("agent_tasks") or {}
        for desk in CHART_DESKS:
            task = tasks.get(desk["developer"])
            if task:
                lines.append(f"{desk['developer']}: {task}")
    scripts = queue()
    lines.append(f"Learning folder: {len(scripts)} Pine scripts waiting or in progress.")
    for path in scripts[:12]:
        notes = _read_notes(path)
        left = [desk["label"] for desk in CHART_DESKS if pending_for_chart(notes, desk["timeframe"])]
        lines.append(f"  {path.name}: charts left {', '.join(left) if left else 'none'}.")
    if len(scripts) > 12:
        lines.append(f"  and {len(scripts) - 12} more.")
    learned = len(list(LEARNED.rglob("*.pine"))) if LEARNED.is_dir() else 0
    lines.append(f"Learned folder: {learned} finished scripts.")
    return lines


def mail_lines(*, probe: bool = False) -> list[str]:
    from futuresfund.mailer import mail_status

    return mail_status(probe=probe)


def creation_lines() -> list[str]:
    """What the strategy creation team learns from and what it has produced."""
    from futuresfund.exemplars import exemplars, target_text
    from futuresfund.strategy_team import creation_lessons, lesson_text

    from futuresfund.objective import describe as objective_text

    lines = [objective_text(), target_text()]
    top = exemplars(5)
    if top:
        lines.append(
            "Each chart has its own strategy developer. They write new scripts from scratch the whole time research is running. Engine 4 develops each one. A new strategy in the library is learned before the next script, including a profitable one-year run whose drawdown is inside $35,000. They also read the exemplars, the finished studies that hit the profit target inside that one-year drawdown with the lowest drawdown: "
            + "; ".join(
                f"{row['title']} (drawdown {row['best']['drawdown']:,.0f}, profit {row['best']['profit']:,.0f}, {row['best']['trades']} trades on {row['best']['timeframe']}; "
                f"green on {row['safe_green']} of {row['tested']} charts)"
                for row in top
            ) + "."
        )
    else:
        lines.append("No finished study is an exemplar yet. The strategy developers still write new scripts from scratch, and engine 4 develops them. A new library strategy is learned when one is filed.")
    rows = creation_lessons(5)
    if rows:
        lines.append(f"Created scripts recorded: {len(creation_lessons(0))}. The latest:")
        lines.extend("  " + lesson_text(row) for row in rows)
    else:
        lines.append("No created script has been recorded yet.")
    return lines


def objective_lines() -> list[str]:
    """What every desk optimizes for, and where it applies."""
    from futuresfund.objective import describe

    return [
        describe(),
        "It applies everywhere a result is ranked: the chart researcher's next setting and the best result of each study, the three "
        "headquarters leaders and the headquarters script, the strategy library shelves, the 8:00am stand-in, and the cards, exemplars, "
        "and promotion gate of the strategy creation team.",
    ]


def error_lines() -> list[str]:
    """The error journal: what was recorded since the last report and when the last report went out."""
    from futuresfund.errors import summary_lines

    return summary_lines()


def facts_for(board, message: str) -> str:
    """Facts the model needs for this question, chosen from the words in it."""
    asked = (message or "").lower()
    sections: list[str] = []
    if _mentions(asked, _OBJECTIVE_WORDS):
        sections.append("Research objective:\n" + "\n".join(objective_lines()))
    if _mentions(asked, _ERROR_WORDS):
        sections.append("Workflow errors:\n" + "\n".join(error_lines()))
    if _mentions(asked, _MAIL_WORDS):
        sections.append("Report email:\n" + "\n".join(mail_lines()))
    if _mentions(asked, _EXECUTION_WORDS):
        sections.append("Execution:\n" + "\n".join(executing_lines()))
    if _mentions(asked, _RESEARCH_WORDS):
        sections.append("Research:\n" + "\n".join(research_lines(board)))
    if _mentions(asked, _CREATION_WORDS):
        sections.append("Strategy creation:\n" + "\n".join(creation_lines()))
    sections.append("What the Portfolio Manager can do from chat:\n" + "\n".join(abilities()))
    return "\n\n".join(sections)


def abilities() -> list[str]:
    from futuresfund.objective import goal_line

    return [
        "Answer what is executing, what is on the active strategy list, and the trading mode.",
        f"State the research objective: {goal_line()} The take profit is larger than the stop, and no trade is down more than $1,500.",
        "Say what the strategy creation team is trained on and how each created script did.",
        "Say whether reports are being emailed, to whom, and when the last one went out; send the fund report on request.",
        "Give the web address and the TradingView alert address. A new address is opened at 5:30pm New York, during the session close, and emailed through the desk mail service.",
        "Send an email on request through the desk mail service.",
        "Say what errors the workflow has raised; have the Systems Administrator compile the error report and email it on request.",
        "File a pasted Pine strategy into the learning folder so the chart developers study it.",
        "Start or stop the researchers.",
        "Start or stop executing a card on the active strategy page by its name.",
        "Reset the paper account after a liquidation. The paper positions, paper trades, and paper realized P&L are cleared. Live accounts stay as they are.",
        "Switching paper and live, adding cards, and placing orders are done on the page, not in chat.",
    ]


# Deterministic answers and tasks.


def answer(board, name: str, message: str) -> str | None:
    """A direct answer or a done task. None means the model should answer with the facts."""
    text = (message or "").strip()
    asked = text.lower()
    if not text:
        return None
    if pine_script(text):
        if name != PORTFOLIO_MANAGER:
            return f"Ask the Portfolio Manager to file that script. I am the {name}."
        return file_script(board, text, name)
    wants_filing = _mentions(asked, ("add this script", "add the script", "file this script", "add this strategy", "file this strategy")) or (
        _mentions(asked, ("learningstrategies", "learning folder", "learning strategies"))
        and _mentions(asked, ("add", "file", "put", "save", "drop"))
        and _mentions(asked, ("script", "strategy", "pine"))
    )
    if wants_filing:
        return (
            "Paste the whole Pine strategy into the message, starting at //@version, and I will file it in the learning folder. "
            "The chart developers take it from there."
        )
    if name == PORTFOLIO_MANAGER:
        task = _task(board, asked)
        if task is not None:
            return task
        if _backtest_report_asked(asked):
            from futuresfund.acceptance_gaps import backtest_report_text

            return backtest_report_text()
    from futuresfund.engine_watch import answer_engines, engine_issue_asked

    if engine_issue_asked(asked):
        return answer_engines(board, text, apply=name == "Systems Administrator")
    if _mentions(asked, _OBJECTIVE_WORDS) and _is_question(asked):
        return "\n".join(objective_lines())
    if _mentions(asked, _ERROR_WORDS) and _is_question(asked):
        return "\n".join(error_lines())
    if _mentions(asked, _MAIL_WORDS) and _is_question(asked):
        return _mail_answer(board)
    if _mentions(asked, _PHONE_WORDS) and _is_question(asked):
        return _phone_answer()
    if _mentions(asked, _EXECUTION_WORDS) and _is_question(asked) and not _mentions(asked, ("library", "learned", "lesson")):
        return "\n".join(executing_lines())
    if _mentions(asked, _QUEUE_WORDS) and _is_question(asked):
        return "\n".join(research_lines(board))
    if _mentions(asked, _CREATION_WORDS) and _is_question(asked):
        return "\n".join(creation_lines())
    if _mentions(asked, ("what can you do", "what do you do", "help me", "your abilities", "what tasks")):
        return "From this chat I can: " + " ".join(abilities())
    return None


def _task(board, asked: str) -> str | None:
    if _wants(asked, ("send", "email", "mail", "resend", "deliver", "compile")) and _mentions(asked, _ERROR_WORDS) and not _is_question(asked):
        return _send_error_report_now(board)
    if _mentions(asked, ("address", "url", "webhook", "phone", "link")) and (
        _mentions(asked, ("new", "expired", "renew", "refresh", "another"))
        or _wants(asked, ("send", "email", "mail", "resend"))
    ):
        return _deliver_address(board, asked)
    if _wants(asked, ("send", "email", "mail", "resend", "deliver")) and _mentions(asked, ("report", "summary")) and not _is_question(asked):
        return _send_report_now(board)
    if _wants(asked, ("send an email", "email this", "email me", "send this")) and not _is_question(asked):
        return _send_note(board, asked)
    research_words = _mentions(asked, _RESEARCH_WORDS)
    if _wants(asked, ("start", "begin", "resume", "run")) and research_words:
        return _start_research()
    if _wants(asked, ("stop", "halt", "pause")) and research_words:
        return _stop_research()
    if _wants(asked, ("start", "execute", "run", "turn on")) and _mentions(asked, ("execut", "card", "active", "strategy", "trading")):
        return _execute_card(board, asked, start=True)
    if _wants(asked, ("stop", "halt", "turn off")) and _mentions(asked, ("execut", "card", "active", "strategy", "trading")):
        return _execute_card(board, asked, start=False)
    if not _is_question(asked) and (
        _wants(asked, ("reset the paper", "reset paper", "paper account reset", "reset my paper", "reset the account"))
        or _wants(asked, ("been liquidated", "was liquidated", "account liquidated"))
    ):
        from futuresfund.desk_services import reset_paper_account

        return reset_paper_account()
    if _mentions(asked, ("switch to live", "go live", "turn on live", "live mode", "switch to paper", "paper mode")) and not _is_question(asked):
        return "Paper and live are switched with the Paper/Live buttons on the active strategy page, not from chat. That keeps the choice deliberate."
    return None


def _mail_answer(board) -> str:
    lines = mail_lines(probe=True)
    state = _research_state(board)
    if state == "running":
        from futuresfund.roster import CHART_DESKS

        tasks = (board.snapshot().get("agent_tasks") or {})
        working = [f"{desk['developer']}: {tasks[desk['developer']]}" for desk in CHART_DESKS if tasks.get(desk["developer"])]
        lines.append(
            "Research is running. A report goes out only when a developer finishes all attempts for one script on one chart, "
            "so there is no email until a chart finishes."
        )
        lines.extend(working)
    else:
        lines.append(
            f"Research is {state}. No script finishes while the researchers are stopped, so no research report is produced. "
            "Press Start on headquarters to continue."
        )
    return "\n".join(lines)


def _phone_answer() -> str:
    from futuresfund.tunnel import current_url, service_lines

    if not current_url():
        return "The phone address is not up yet. Ask for a new one and the portfolio manager will open it and email it."
    return "\n".join(service_lines())


def _deliver_address(board, asked: str) -> str:
    """Open a new address when this one is due, and email it through the desk mail service."""
    from futuresfund.mailer import send_report
    from futuresfund.tunnel import current_url, issue_due, renew, service_lines

    make_new = _mentions(asked, ("new", "expired", "renew", "refresh", "another")) or issue_due() or not current_url()
    try:
        if make_new:
            renew(board)
            lead = "I opened a new address and emailed it."
        else:
            result = send_report("\n".join(service_lines()), "Futures desk address")
            lead = f"I emailed the address to {result.get('to')}." if result.get("sent") else (result.get("reason") or "The address was not emailed.")
    except Exception as exc:
        return f"The address was not opened: {exc}"
    return lead + "\n" + "\n".join(service_lines())


def _send_note(board, asked: str) -> str:
    """The portfolio manager sends the words in the request through the desk mail service."""
    from futuresfund.mailer import send_report

    body = asked.strip()
    if len(body) < 8:
        return "Say what the email should say, and I will send it."
    try:
        result = send_report(body, "Futures desk note")
    except Exception as exc:
        return f"The email was not sent: {exc}"
    if result.get("sent"):
        return f"I emailed that note to {result.get('to')}."
    return result.get("reason") or "The email was not sent."


def _send_error_report_now(board) -> str:
    """The Systems Administrator compiles the workflow error report and the Portfolio Manager emails it."""
    from futuresfund import errors

    try:
        result = errors.deliver(board, force=True)
    except Exception as exc:
        return f"The error report was not sent: {exc}"
    count = result.get("errors", 0)
    if result.get("sent"):
        return f"Done. The Systems Administrator compiled the workflow error report ({count} errors) and I emailed it to {result.get('to')}. The full report is on the Systems Administrator's channel."
    return f"The Systems Administrator compiled the workflow error report ({count} errors), but it was not emailed: {result.get('reason')} The full report is on the Systems Administrator's channel."


def _send_report_now(board) -> str:
    from futuresfund.mailer import deliver_report

    try:
        written = deliver_report()
    except Exception as exc:
        return f"The report was not sent: {exc}"
    board.post(PORTFOLIO_MANAGER, written, kind="report", channel=PORTFOLIO_MANAGER)
    tail = written.strip().splitlines()[-1] if written.strip() else "The report was written."
    return f"Done. {tail} The full report is on my channel."


def _start_research() -> str:
    from futuresfund import server

    try:
        result = server.start_run() or {}
    except Exception as exc:
        return f"The researchers did not start: {exc}"
    if result.get("started"):
        return "Started the researchers. The chart developers are taking the Pine scripts from the learning folder."
    return "The researchers are already running."


def _stop_research() -> str:
    from futuresfund import server

    try:
        server.stop_run()
    except Exception as exc:
        return f"The researchers did not stop: {exc}"
    return "Stop requested. The chart developers finish the attempt they are on and stop. The strategy was not changed and no order was sent."


def _execute_card(board, asked: str, *, start: bool) -> str:
    from futuresfund import live_trading
    from futuresfund.book import active_strategies, load

    book = load()
    cards = active_strategies(book)
    if not cards:
        return "The active strategy page has no cards. Add a card there first."
    executing = {row["id"]: row for row in live_trading.executing_view(book)}
    card = _match_card(asked, cards)
    if card is None:
        if not start and len(executing) == 1:
            card = next(row for row in cards if str(row.get("id")) in executing)
        elif start and len(cards) == 1:
            card = cards[0]
        else:
            names = "; ".join(str(row.get("title") or "Untitled") for row in cards)
            return f"Which card? The active list has: {names}."
    try:
        if start:
            slot = live_trading.start(board, str(card["id"]))
            return (
                f"{slot['trader']} is executing {slot['title']} on the {slot['timeframe']} {slot['contract']} chart "
                f"for account {slot['account']} in {live_trading.trading_mode()} mode."
            )
        slot = live_trading.stop(board, str(card["id"]))
        return f"{slot['trader']} stopped executing {slot['title']}. {slot.get('trades') or 0} closed trades, realized {round(float(slot.get('realized') or 0), 2)}."
    except ValueError as exc:
        return str(exc)


def _match_card(asked: str, cards: list[dict]) -> dict | None:
    best, score = None, 0
    for row in cards:
        title = str(row.get("title") or "").lower()
        words = [word for word in re.findall(r"[a-z0-9]+", title) if len(word) > 2]
        if not words:
            continue
        if title and title in asked:
            return row
        hits = sum(1 for word in words if word in asked)
        if hits > score and hits >= max(1, len(words) // 2):
            best, score = row, hits
    return best


# Words.

_OBJECTIVE_WORDS = (
    "objective", "optimiz", "drawdown first", "lowest drawdown", "minimize drawdown", "minimise drawdown", "second priority",
    "profit target", "the target", "acceptable", "ranked by", "rank by", "what are the researchers looking for",
    "what do the researchers look for", "how is best chosen",
)
_ERROR_WORDS = ("error", "errors", "exception", "traceback", "went wrong", "gone wrong", "failures", "failed runs", "error journal", "error report")
_MAIL_WORDS = ("email", "e-mail", "mail", "smtp", "gmail", "inbox", "report")
_PHONE_WORDS = ("phone", "web address", "web url", "public address", "desk address", "on my phone", "webhook")
_EXECUTION_WORDS = (
    "active strateg", "executing", "execution", "currently running", "are we running", "are you running",
    "what is running", "what's running", "is running", "being traded", "trading right now", "floor trader",
    "quantitative trader", "paper or live", "live or paper", "trading mode",
)
_RESEARCH_WORDS = ("research", "learning", "learn folder", "study", "studying", "developer", "backtest", "queue", "attempt")
_CREATION_WORDS = (
    "strategy developer", "strategy creat", "creation team", "created strateg", "created script", "creation tester",
    "exemplar", "card keeper", "lesson writer", "script checker", "team created", "has created", "new strategies",
)
_QUEUE_WORDS = (
    "learning folder", "learningstrategies", "learning queue", "the queue", "scripts waiting", "waiting in", "developers doing",
    "researchers doing", "research running", "researchers running", "research status", "is research", "are the researchers",
)


def _backtest_report_asked(asked: str) -> bool:
    """The Portfolio Manager's table of why finished backtests miss the acceptance boxes."""
    if not _mentions(asked, ("backtest", "backtests")):
        return False
    return _mentions(asked, ("acceptance", "passing", "pass", "fail", "miss", "criteria", "why", "report", "table", "reason"))


def _mentions(asked: str, words: tuple[str, ...]) -> bool:
    return any(word in asked for word in words)


def _wants(asked: str, verbs: tuple[str, ...]) -> bool:
    """An instruction, not a question about one."""
    if _is_question(asked):
        return False
    return any(re.search(rf"(^|[\s,.])(please\s+)?{re.escape(verb)}\b", asked) for verb in verbs)


def _is_question(asked: str) -> bool:
    stripped = asked.strip()
    if stripped.endswith("?"):
        return True
    return bool(re.match(r"^(why|what|when|where|who|which|how|is|are|do|does|did|can you tell|could you tell|have|has)\b", stripped))


def _research_state(board) -> str:
    try:
        return str(board.snapshot().get("research") or "idle")
    except Exception:
        return "idle"
