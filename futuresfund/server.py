"""Futures desk. Meetings and the bar engine set the strategy. Interval alerts place the orders."""

from __future__ import annotations

import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from futuresfund.alerts import parse_alert
from futuresfund.board import Board
from futuresfund.book import snapshot as book_snapshot
from futuresfund.config import load_env, public_settings, webhook_token
from futuresfund.contracts import crosstrade_name, yahoo_symbol
from futuresfund.discuss import run_discussion
from futuresfund.research import save_bars, sync_lead
from futuresfund.csv_bars import parse_tradingview_csv
from futuresfund.strategy import get_strategy, normalize_timeframe, store_strategy
from futuresfund.session import handle_interval, scheduler, strategy_meeting

load_env()

STATIC = Path(__file__).resolve().parent / "static"
board = Board()
app = FastAPI(title="Futures Trading Desk")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

board.post(
    "System",
    "Futures desk is online. Upload a TradingView bar file, with the account size and profit target. "
    "The desk tests several different rules on that file. Each profitable rule stays on the book, and you choose which ones are active. "
    "Ingestion adds each TradingView alert to the chart. Two strategies can run at once, each on the account assigned to it. "
    "The trading analyst confirms the fill with NinjaTrader. "
    "Research continues until three rules pass risk. The vote is 8:00am Monday through Friday and 5:00pm Sunday through Friday.",
    kind="system",
    channel="headquarters",
)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/roster")
def roster():
    from futuresfund.roster import ROSTER
    return {"agents": ROSTER}


@app.get("/api/state")
def state():
    return board.snapshot()


@app.get("/api/lab")
def lab_report():
    from futuresfund.lab import load_report, running
    from futuresfund.learn import research_slate

    report = load_report()
    report["running"] = running()
    report["studying"] = board.research in {"running", "stopping"}
    report["slate"] = research_slate()
    report["trials"] = report.get("trials", [])[-40:]
    from futuresfund.backtrader_engine import engine_status

    report["engines"] = engine_status()
    return report


@app.post("/api/lab/run")
def lab_run():
    from futuresfund.lab import running, start_search
    if running():
        raise HTTPException(status_code=409, detail="The developer is already running the engine.")
    if not start_search(board):
        raise HTTPException(status_code=409, detail="The developer is already running the engine.")
    return {"ok": True}


class ActiveStrategies(BaseModel):
    strategies: list[dict]


@app.post("/api/active-strategies")
def save_strategies(body: ActiveStrategies):
    from futuresfund.book import save_active_strategies

    return {"strategies": save_active_strategies(body.strategies)}


class RunningStrategy(BaseModel):
    id: str
    account: str = "paper"
    enabled: bool = True


@app.post("/api/running")
def run_strategy(body: RunningStrategy):
    from futuresfund.book import set_running

    try:
        rows = set_running(body.id, body.account, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"running": rows}


class AccountRules(BaseModel):
    account: str = ""
    size: float
    profit_target: float
    max_drawdown: float
    trailing: bool = True
    max_contracts: int


@app.post("/api/account-rules")
def account_rules(body: AccountRules):
    from futuresfund.book import save_account_rules
    from futuresfund.prop_rules import rules_sentence

    try:
        saved = save_account_rules({
            "account": body.account,
            "size": body.size,
            "profit_target": body.profit_target,
            "max_drawdown": body.max_drawdown,
            "trailing": body.trailing,
            "max_contracts": body.max_contracts,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sentence = rules_sentence(saved["size"])
    board.post("Floor Trader", f"Account rules saved. {sentence}", kind="report", channel="Floor Trader")
    board.post("Compliance & Operations", sentence, kind="report", channel="Compliance & Operations")
    return {"rules": saved}


@app.get("/api/chart")
def chart(contract: str = "ES1!", timeframe: str = "5m", limit: int = 160):
    from futuresfund.chart_view import chart_payload

    try:
        return chart_payload(contract, timeframe, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/research-guide")
def research_guide():
    from futuresfund.learn import research_guidelines

    return {"text": research_guidelines()}


@app.get("/api/library")
def library(contract: str = "ES1!", q: str = ""):
    from futuresfund.library import search_library

    return search_library(contract, q)


@app.get("/api/library/{strategy_id}")
def library_one(strategy_id: str, contract: str = ""):
    from futuresfund.library import library_script

    row = library_script(strategy_id, contract or None)
    if row is None or not row.get("pine"):
        raise HTTPException(status_code=404, detail="That strategy script is not in the library.")
    return row


@app.get("/api/leaders/{strategy_id}")
def leader(strategy_id: str):
    from futuresfund.strategy import leader_script

    row = leader_script(strategy_id)
    if row is None or not row.get("pine"):
        raise HTTPException(status_code=404, detail="That strategy script is not on headquarters.")
    return row


@app.get("/api/book")
def book():
    payload = book_snapshot()
    payload["settings"] = public_settings()
    return payload


@app.get("/api/settings")
def settings():
    return public_settings()


@app.post("/api/research")
async def research(
    file: UploadFile = File(...),
    contract: str = Form(...),
    timeframe: str = Form("5m"),
    account: str = Form(...),
    account_size: float = Form(...),
    profit_target: float = Form(...),
):
    if board.busy:
        raise HTTPException(status_code=409, detail="The desks are already in a meeting.")
    raw = await file.read()
    if len(raw) > 8_000_000:
        raise HTTPException(status_code=400, detail="The bar file is larger than 8 MB.")
    try:
        frame = normalize_timeframe(timeframe)
        instrument = crosstrade_name(contract)
        bars = parse_tradingview_csv(raw.decode("utf-8-sig", errors="replace"))
        if len(bars) < 80:
            raise ValueError("The file needs at least 80 bars before a strategy can be tested.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_bars(bars)
    current = get_strategy() or {}
    current.update({
        "contract": instrument,
        "timeframe": frame,
        "account": account.strip(),
        "account_size": account_size,
        "profit_target": profit_target,
        "yahoo": yahoo_symbol(instrument),
    })
    store_strategy(current)

    def _discuss():
        run_discussion(board, "Upload")

    threading.Thread(target=_discuss, daemon=True, name="futures-discussion").start()
    return {"ok": True, "started": True, "bars": len(bars)}


@app.post("/api/runs/stop")
def stop_run():
    board.post("System", "Stop requested. The strategy was not changed and no order was sent.", kind="system", channel="headquarters")
    board.mark_stopped()
    return {"ok": True}


_learning_thread: threading.Thread | None = None


@app.post("/api/runs/start")
def start_run():
    global _learning_thread
    if _learning_thread and _learning_thread.is_alive():
        return {"ok": True, "started": False}
    board.cancel.clear()
    board.post("System", "Start requested. The researchers will run the Pine scripts.", kind="system", channel="headquarters")

    def _go():
        from futuresfund.learn import run_learning
        run_learning(board)

    _learning_thread = threading.Thread(target=_go, daemon=True, name="futures-learning")
    _learning_thread.start()
    return {"ok": True, "started": True}


@app.post("/hooks/tradingview/{token}")
async def tradingview(token: str, request: Request):
    expected = webhook_token()
    if not expected:
        raise HTTPException(status_code=503, detail="Set TV_WEBHOOK_TOKEN before TradingView can call this desk.")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Unknown webhook.")
    raw = (await request.body()).decode("utf-8", errors="replace")
    preview = " ".join(raw.split())
    if len(preview) > 280:
        preview = preview[:277].rstrip() + "..."
    board.log("Ingestion", "Reading a TradingView alert")
    board.post("Ingestion", f"TradingView alert arrived.\n{preview or '(empty body)'}", kind="report", channel="Ingestion")
    try:
        signal = parse_alert(raw)
    except ValueError as exc:
        board.log("Ingestion", f"The alert was not recorded: {exc}")
        board.post("Ingestion", f"The alert was not recorded: {exc}", kind="error", channel="Ingestion")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from futuresfund.alerts import BAR_FEED_ID

    if board.busy and signal.get("id") != BAR_FEED_ID:
        board.log("Ingestion", "A meeting is running. This alert was not recorded.")
        raise HTTPException(status_code=409, detail="The desks are in a meeting or the initial analysis. This interval alert was not sent.")
    try:
        result = handle_interval(board, signal)
    except Exception as exc:
        board.log("Floor Trader", f"The order was not sent: {exc}")
        board.post("Floor Trader", f"The order was not sent: {exc}", kind="error", channel="Floor Trader")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/hooks/mail/{token}")
async def inbound_mail(token: str, request: Request):
    """Prop-firm mail. The systems administrator records it for the 5:00pm meeting."""
    import os

    from futuresfund.account_mail import accept_mail

    expected = os.environ.get("MAIL_WEBHOOK_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Set MAIL_WEBHOOK_TOKEN before prop-firm mail can reach this desk.")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Unknown webhook.")
    payload = await _mail_payload(request)
    return accept_mail(board, payload)


async def _mail_payload(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    raw = (await request.body()).decode("utf-8", errors="replace")
    if "application/json" in content_type:
        import json

        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="The email body was not JSON.") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="The email body must be an object.")
        return body
    return {"subject": request.headers.get("subject", ""), "from": request.headers.get("from", ""), "text": raw}


class ToggleRequest(BaseModel):
    active: bool


def _strategy_row(strategy_id: str) -> tuple[dict, dict]:
    strategy = get_strategy()
    if not strategy:
        raise HTTPException(status_code=404, detail="No strategies are on file yet.")
    for item in strategy.get("strategies") or []:
        if item.get("id") == strategy_id:
            return strategy, item
    raise HTTPException(status_code=404, detail="That strategy is not on file.")


@app.get("/api/strategies/{strategy_id}")
def strategy_pine(strategy_id: str):
    _book, item = _strategy_row(strategy_id)
    return {"id": item["id"], "title": item["title"], "formula": item["formula"], "pine": item.get("pine") or ""}


@app.post("/api/strategies/{strategy_id}")
def strategy_toggle(strategy_id: str, body: ToggleRequest):
    strategy, item = _strategy_row(strategy_id)
    if body.active and not item.get("proven"):
        raise HTTPException(status_code=400, detail="Only a profitable strategy can be turned on.")
    item["active"] = body.active
    sync_lead(strategy)
    store_strategy(strategy)
    active = [row["title"] for row in strategy["strategies"] if row.get("active")]
    return {"ok": True, "active": active}


@app.post("/api/review")
def review_now():
    if board.busy:
        raise HTTPException(status_code=409, detail="A futures analysis is already running.")

    def _run():
        strategy_meeting(board, "Manual")

    threading.Thread(target=_run, daemon=True, name="futures-review").start()
    return {"ok": True}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    agent: str = "Portfolio Manager"


_chat_busy = False


@app.post("/api/chat")
def chat(body: ChatRequest):
    global _chat_busy
    if _chat_busy:
        raise HTTPException(status_code=409, detail="That desk is still answering.")
    text = body.message.strip()
    name = _agent_name(body.agent) or "Portfolio Manager"
    board.post("You", text, kind="chat", channel=name)
    _chat_busy = True

    def _reply():
        global _chat_busy
        board.log(name, "Reading the firm book")
        try:
            answer = _ask_agent(name, text)
            board.post(name, answer, kind="chat", channel=name)
            _relay(name, answer)
        except Exception as exc:
            board.post(name, f"I could not answer just now: {exc}", kind="error", channel=name)
        finally:
            _chat_busy = False

    threading.Thread(target=_reply, daemon=True, name="futures-chat").start()
    return {"ok": True}


def _strategy_facts(view: dict) -> str:
    """Answer strategy questions from the engine. The chat model was inventing trade counts."""
    from futuresfund.lab import load_report

    rows = load_report().get("accepted") or []
    if not rows:
        rows = [
            item for item in (view.get("strategy") or {}).get("strategies") or []
            if item.get("proven")
        ]
    if not rows:
        return "No rule has cleared the Apex trailing drawdown yet. Nothing is up for discussion."
    lines = []
    if view.get("positions"):
        from futuresfund.book import open_trade_brief

        lines.append(open_trade_brief(view))
    else:
        lines.append("No trade is open. These are backtests, not live positions.")
    for number, item in enumerate(rows, start=1):
        backtest = item.get("backtest") or {}
        lines.append(
            f"{number}. {item.get('title')}: {backtest.get('trades')} trades, "
            f"profit {backtest.get('net_profit')}, drawdown {backtest.get('max_drawdown')}. "
            f"{item.get('formula')}"
        )
    lines.append("None of these are live. You turn one on when you want TradingView alerts to reach the floor trader.")
    from futuresfund.strategy import lead_script

    lead = lead_script()
    if lead:
        lines.append(
            f"The headquarters script is the most profitable of these: {lead['title']}. "
            f"{lead['trades']} trades, profit {lead['net_profit']}, drawdown {lead['max_drawdown']}. "
            f"{lead['pine']}"
        )
    return " ".join(lines)


def _agent_name(raw: str) -> str | None:
    from futuresfund.roster import ROSTER

    text = (raw or "").strip().lower()
    if not text:
        return None
    for agent in ROSTER:
        if agent["name"].lower() == text:
            return agent["name"]
    return None


def _ask_agent(name: str, message: str) -> str:
    from futuresfund.book import firm_record, open_trade_brief
    from futuresfund.llm import answer_chat
    from futuresfund.roster import ROSTER

    view = book_snapshot()
    asked = message.lower()
    trade_detail = ""
    if any(word in asked for word in ("stop", "take profit", "take-profit", "open trade", "current trade")):
        trade_detail = open_trade_brief(view)
    names = ", ".join(agent["name"] for agent in ROSTER)
    prompt = (
        f"You are {name} on this futures desk. "
        "Answer from the firm record. If a number is not in the record, say you do not have that number. "
        "Do not invent a profit, a drawdown, a trade count, or an open position. "
        "Do not place an order. Do not debate whether a trade should have been taken. "
        "Do not write a To: line. "
        f"The desks are: {names}.\n\n"
        f"Firm record:\n{firm_record(view)}\n\n"
        + (f"Open trade detail:\n{trade_detail}\n\n" if trade_detail else "")
        + f"Message: {message}"
    )
    return answer_chat(name, prompt) or "I have nothing to add."


def _relay(speaker: str, text: str) -> None:
    """One written handoff. The next desk answers once and does not hand it on again."""
    target = None
    note: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if target is None and stripped.lower().startswith("to:"):
            target = _agent_name(stripped.split(":", 1)[1])
            continue
        if target and stripped:
            note.append(stripped)
    if not target or target == speaker or not note:
        return
    from futuresfund.discuss import hand_report

    handed = " ".join(note)[:500]
    hand_report(board, speaker, target, handed)
    reply = _ask_agent(target, f"{speaker} handed you this written report: {handed}")
    board.post(target, reply, kind="chat", channel=speaker)
    if target != speaker:
        board.post(target, reply, kind="chat", channel=target)


@app.on_event("startup")
def _startup():
    threading.Thread(target=scheduler, args=(board,), daemon=True, name="futures-scheduler").start()
    from futuresfund.ninjatrader import start_listener

    start_listener(board)

    def _boot():
        from futuresfund.discuss import load_chart_file
        from futuresfund.research import load_bars

        count = load_chart_file()
        stored = len(load_bars())
        if count:
            board.post(
                "System",
                f"Loaded {count} bars from the 15-minute ES file. Ingestion adds the next TradingView alert onto this series.",
                kind="system",
                channel="headquarters",
            )
        elif stored:
            board.post(
                "System",
                f"Keeping {stored} bars. Ingestion adds the next TradingView alert, and the next meeting discusses that series.",
                kind="system",
                channel="headquarters",
            )
        bars_on_disk = stored or count
        board.post(
            "2min chart developer",
            "ES orders fill at the bar close. A stop can fill inside a later bar. Commission is $5.50 a side and slippage is one tick.",
            kind="report",
            channel="2min chart developer",
        )
        board.post(
            "Systems Administrator",
            f"This desk is running on this machine with {bars_on_disk} bars loaded. Ingestion appends each TradingView alert. Prop-firm mail is posted to this desk. There is no exchange co-location.",
            kind="report",
            channel="Systems Administrator",
        )
        board.post(
            "Compliance & Operations",
            "New risk stops at 4:45pm ET, 15 minutes before the 5:00pm ET futures halt. "
            "Open contracts are flattened until 6:00pm ET. The trailing drawdown and any daily loss limit stated by the prop firm are enforced with the systems administrator.",
            kind="report",
            channel="Compliance & Operations",
        )
        board.post(
            "System",
            "Processing is stopped. Press Start to run the Pine scripts. Each agent has their own model and their own thread. Backtrader runs the backtests: engine 1 is the 2-minute chart, engine 2 is the 5-minute chart, and engine 3 is the 15-minute chart.",
            kind="system",
            channel="headquarters",
        )
        from futuresfund.crew import get_crew
        from futuresfund.llm import ensure_agent_models

        ensure_agent_models()
        get_crew(board)

    threading.Thread(target=_boot, daemon=True, name="futures-boot").start()
