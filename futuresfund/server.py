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
    "Each interval alert places, holds, or closes only when the active rules agree and the account matches. "
    "Research continues until three rules pass risk. The vote is at 8:00am.",
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
    report = load_report()
    report["running"] = running()
    report["trials"] = report.get("trials", [])[-40:]
    return report


@app.post("/api/lab/run")
def lab_run():
    from futuresfund.lab import running, start_search
    if running():
        raise HTTPException(status_code=409, detail="The developer is already running the engine.")
    if not start_search(board):
        raise HTTPException(status_code=409, detail="The developer is already running the engine.")
    return {"ok": True}


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
    timeframe: str = Form("15m"),
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


@app.post("/hooks/tradingview/{token}")
async def tradingview(token: str, request: Request):
    expected = webhook_token()
    if not expected:
        raise HTTPException(status_code=503, detail="Set TV_WEBHOOK_TOKEN before TradingView can call this desk.")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Unknown webhook.")
    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        signal = parse_alert(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if board.busy:
        raise HTTPException(status_code=409, detail="The desks are in a meeting or the initial analysis. This interval alert was not sent.")
    result = handle_interval(board, signal)
    return result


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


@app.post("/api/chat")
def chat(body: ChatRequest):
    if board.snapshot()["agent_status"].get("Portfolio Manager") == "working":
        raise HTTPException(status_code=409, detail="The portfolio manager is busy.")
    text = body.message.strip()
    board.post("You", text, kind="chat", channel="Portfolio Manager")

    def _reply():
        board.set_status("Portfolio Manager", "working")
        try:
            answer = _ask(text)
            board.post("Portfolio Manager", answer, kind="chat", channel="Portfolio Manager")
        except Exception as exc:
            board.post("Portfolio Manager", f"I could not answer just now: {exc}", kind="error", channel="Portfolio Manager")
        board.set_status("Portfolio Manager", "done")

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
    lines = ["The book is flat. No trade is open. These are backtests, not live positions."]
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


def _ask(message: str) -> str:
    from futuresfund.llm import complete
    from futuresfund.prop_rules import describe, gate

    view = book_snapshot()
    strategy = dict(view.get("strategy") or {})
    account_size = float(strategy.get("account_size") or 50000)
    eligible, rejected = [], []
    for item in strategy.get("strategies") or []:
        backtest = item.get("backtest") or {}
        line = (
            f"{item.get('title')}: {backtest.get('trades')} trades, "
            f"profit {backtest.get('net_profit')}, drawdown {backtest.get('max_drawdown')}. "
            f"{item.get('formula')}"
        )
        if gate(dict(item), account_size):
            eligible.append(line)
        else:
            rejected.append(line)
    asked = message.lower()
    if any(word in asked for word in ("strateg", "trade", "profit", "drawdown", "show", "pine")):
        return _strategy_facts(view)

    prompt = (
        "You are the portfolio manager of one prop futures account. Answer in plain sentences. "
        "Do not place trades from this chat. The book is flat unless a position is listed. "
        "Repeat the trade count from the list. Do not invent a trade, an open position, or an indicator. "
        f"{describe(account_size)} "
        "The next meeting may discuss only the eligible list.\n"
        f"Eligible: {'; '.join(eligible) if eligible else 'none'}.\n"
        f"Rejected: {'; '.join(rejected) if rejected else 'none'}.\n"
        f"Positions: {view.get('positions') or 'flat'}.\n"
        f"User: {message}"
    )
    return complete(prompt) or "I have nothing to add."


@app.on_event("startup")
def _startup():
    threading.Thread(target=scheduler, args=(board,), daemon=True, name="futures-scheduler").start()

    def _boot():
        from futuresfund.discuss import load_chart_file
        from futuresfund.research import load_bars

        count = load_chart_file()
        stored = len(load_bars())
        from futuresfund.strategy import get_strategy

        if count:
            board.post(
                "System",
                f"Loaded {count} bars from the 15-minute ES file. Alerts add the next bar onto this series.",
                kind="system",
                channel="headquarters",
            )
        elif stored:
            board.post(
                "System",
                f"Keeping {stored} bars. Alerts add the next bar, and the next meeting discusses that series.",
                kind="system",
                channel="headquarters",
            )
        bars_on_disk = stored or count
        board.post(
            "Quantitative Developer",
            "ES orders fill at the bar close. A stop can fill inside a later bar. Commission is $5.50 a side and slippage is one tick.",
            kind="report",
            channel="Quantitative Developer",
        )
        board.post(
            "Systems Administrator",
            f"This desk is running on this machine with {bars_on_disk} bars loaded. Alerts append the next bar. There is no exchange co-location.",
            kind="report",
            channel="Systems Administrator",
        )
        board.post(
            "Compliance & Operations",
            "New risk stops at 4:45pm ET, 15 minutes before the 5:00pm ET futures halt. Open contracts are flattened until 6:00pm ET.",
            kind="report",
            channel="Compliance & Operations",
        )
        approved = [
            item for item in (get_strategy() or {}).get("strategies") or []
            if item.get("proven") and item.get("accepted")
        ]
        if len(approved) < 3:
            from futuresfund.discuss import run_discussion

            run_discussion(board, "Research")
        else:
            board.post(
                "Portfolio Manager",
                "Three rules have passed risk. The vote is at 8:00am.",
                kind="report",
                channel="Portfolio Manager",
            )

    threading.Thread(target=_boot, daemon=True, name="futures-boot").start()
