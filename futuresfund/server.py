"""Futures desk. Meetings and the bar engine set the strategy. Interval alerts place the orders."""

from __future__ import annotations

import secrets
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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

from futuresfund import errors as _errors  # noqa: E402  the journal needs the board to exist first

board.on_error = _errors.journal_post


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """A request the server could not serve is an error in the workflow: it goes on the board and into the journal."""
    board.post(
        _errors.SYSTEMS,
        f"Request {request.method} {request.url.path} failed: {type(exc).__name__}: {exc}",
        kind="error",
        channel=_errors.SYSTEMS,
    )
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


@app.get("/api/errors")
def error_journal():
    """The error journal and the last report, for the page and for checks."""
    last = _errors.last_report()
    rows = _errors.entries(last.get("time"))
    return {"last_report": last, "since_last": rows[-100:], "summary": _errors.summary_lines()}

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
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


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
    from futuresfund.learn import chart_progress, research_slate

    report = load_report()
    report["running"] = running()
    report["studying"] = board.research in {"running", "stopping"}
    report["slate"] = research_slate()
    report["progress"] = chart_progress()
    report["trials"] = report.get("trials", [])[-40:]
    from futuresfund.backtrader_engine import engine_status

    report["engines"] = engine_status()
    from futuresfund.kronos_replay import kronos_report

    report["kronos"] = kronos_report()
    from futuresfund.acceptance_gaps import gaps_report

    report["gaps"] = gaps_report()
    from futuresfund.acceptance_gaps import published_report

    report["acceptance_report"] = published_report()
    return report


_checks: dict = {"stamp": None, "reports": [], "running": False, "checked_at": None}
_checks_lock = threading.Lock()


def _checks_stamp() -> tuple:
    from futuresfund.tv_checks import exports

    return tuple((path.name, path.stat().st_mtime) for path in exports())


def _run_checks(stamp: tuple) -> None:
    from datetime import datetime

    from futuresfund.tv_checks import check_all

    try:
        reports = check_all()
    except Exception as exc:
        reports = [{"error": f"The TradingView check stopped: {exc}"}]
    with _checks_lock:
        _checks.update({"stamp": stamp, "reports": reports, "running": False, "checked_at": datetime.now().strftime("%H:%M")})
    for report in reports:
        if report.get("error") or report.get("matched") is None:
            continue
        board.post(
            "Systems Administrator",
            f"TradingView check for {report['title']} on the {report['timeframe']} {report['instrument']} chart: "
            f"{report['matched']} of {report['expected']} TradingView trades are identical on the engine.",
            kind="report",
            channel="Systems Administrator",
        )


def start_checks(force: bool = False) -> None:
    """Check the engine against every Strategy Tester export in the project root, in the background."""
    stamp = _checks_stamp()
    with _checks_lock:
        if _checks["running"] or (not force and stamp == _checks["stamp"]):
            return
        _checks["running"] = True
    threading.Thread(target=_run_checks, args=(stamp,), daemon=True, name="tradingview-checks").start()


@app.get("/api/tradingview-checks")
def tradingview_checks():
    start_checks()
    with _checks_lock:
        return {"running": _checks["running"], "checked_at": _checks["checked_at"], "reports": list(_checks["reports"])}


@app.post("/api/rnd/acceptance-report")
def acceptance_report():
    """The Portfolio Manager files the acceptance table and returns it for the R&D page."""
    from futuresfund.acceptance_gaps import backtest_report_text, published_report

    text = backtest_report_text()
    board.post("Portfolio Manager", text, kind="report", channel="Portfolio Manager")
    return published_report()


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

    try:
        return {"strategies": save_active_strategies(body.strategies)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ActiveCard(BaseModel):
    id: str = ""
    account: str = "paper"
    instrument: str = ""
    timeframe: str = ""
    order_instrument: str = ""
    title: str = ""
    text: str = ""
    locked: bool = False


@app.post("/api/active-strategies/add")
def add_strategy_card(body: ActiveCard | None = None):
    from futuresfund.book import add_active_strategy

    row = {key: value for key, value in (body.model_dump() if body else {}).items() if value not in ("", None)}
    try:
        return {"strategies": add_active_strategy(row)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/active-strategies/{card_id}")
def save_strategy_card(card_id: str, body: ActiveCard):
    from futuresfund.book import save_active_strategy

    try:
        return {"strategies": save_active_strategy({**body.model_dump(), "id": card_id})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/active-strategies/{card_id}/remove")
def remove_strategy_card(card_id: str):
    from futuresfund.book import remove_active_strategy

    try:
        return {"strategies": remove_active_strategy(card_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _execution(message: str, work):
    from futuresfund.crew import get_crew

    def guarded():
        try:
            return work()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return get_crew(board).application(message, guarded, announce=True)


@app.post("/api/execution/{card_id}/start")
def execution_start(card_id: str):
    from futuresfund.live_trading import executing_view, start

    def work():
        start(board, card_id)
        return {"executing": executing_view()}

    return _execution("Handing an active strategy to a trader", work)


class KronosPaper(BaseModel):
    contract: str
    timeframe: str


@app.post("/api/kronos-paper/start")
def kronos_paper_start(body: KronosPaper):
    from futuresfund.live_trading import executing_view, start_kronos

    def work():
        slot = start_kronos(board, body.contract, body.timeframe)
        return {"executing": executing_view(), "trader": slot["trader"], "note": slot.get("note") or ""}

    return _execution("Handing Kronos signals to a paper trader", work)


@app.post("/api/execution/{card_id}/stop")
def execution_stop(card_id: str):
    from futuresfund.live_trading import executing_view, stop

    def work():
        result = stop(board, card_id)
        return {"note": result["note"], "executing": executing_view()}

    return _execution("Stopping an executing strategy", work)


class TradingMode(BaseModel):
    mode: str


@app.post("/api/trading-mode")
def trading_mode_set(body: TradingMode):
    from futuresfund.live_trading import set_mode

    return _execution("Changing paper or live trading", lambda: set_mode(board, body.mode))


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


class StrategyAcceptance(BaseModel):
    min_profit: float
    profit_factor: float
    win_rate: float
    min_trades: int
    drawdown_pct: float = 5
    goal: str | None = None
    use_drawdown: bool = True
    use_trades: bool = True
    use_profit: bool = False
    use_factor: bool = False
    use_win_rate: bool = False


@app.post("/api/strategy-acceptance")
def strategy_acceptance(body: StrategyAcceptance):
    from futuresfund.book import save_strategy_acceptance

    try:
        saved = save_strategy_acceptance({
            "min_profit": body.min_profit,
            "profit_factor": body.profit_factor,
            "win_rate": body.win_rate,
            "min_trades": body.min_trades,
            "drawdown_pct": body.drawdown_pct,
            "goal": body.goal,
            "use_drawdown": body.use_drawdown,
            "use_trades": body.use_trades,
            "use_profit": body.use_profit,
            "use_factor": body.use_factor,
            "use_win_rate": body.use_win_rate,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"strategy_acceptance": saved}


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
    from futuresfund.kronos_replay import enable_live

    enable_live()
    try:
        return chart_payload(contract, timeframe, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/kronos-training")
def kronos_training():
    from futuresfund.kronos_feed import progress

    return progress()


class ResearchGuide(BaseModel):
    text: str = ""


@app.get("/api/research-guide")
def research_guide():
    from futuresfund.learn import research_guidelines

    return {"text": research_guidelines()}


@app.post("/api/research-guide")
def save_research_guide(body: ResearchGuide):
    from futuresfund.indicator_guidance import save_guidance

    try:
        text = save_guidance(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    board.post(
        "Risk Manager",
        "The research guide was edited on the Under the hood page. The researchers read that text.",
        kind="report",
        channel="Risk Manager",
    )
    return {"ok": True, "text": text}


@app.get("/api/risk-suggestions")
def risk_suggestions():
    from futuresfund.risk_style import look

    message, rows = look()
    if message:
        board.post("Risk Manager", message, kind="report", channel="Risk Manager")
    return {"suggestions": rows}


@app.post("/api/risk-suggestions/{suggestion_id}/apply")
@app.post("/api/risk-suggestions/{suggestion_id}/approve")
def apply_risk_suggestion(suggestion_id: str):
    from futuresfund.risk_style import apply

    try:
        saved = apply(suggestion_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="That suggestion is not on the list.") from exc
    board.post(
        "Risk Manager",
        f"Applied a research-guide change: {saved.get('proposed')}",
        kind="report",
        channel="Risk Manager",
    )
    return {"ok": True, "suggestion": saved}


@app.post("/api/risk-suggestions/{suggestion_id}/deny")
def deny_risk_suggestion(suggestion_id: str):
    from futuresfund.risk_style import deny

    try:
        saved = deny(suggestion_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="That suggestion is not on the list.") from exc
    board.post(
        "Risk Manager",
        f"Denied a research-guide change: {saved.get('proposed')} The guide was not changed.",
        kind="report",
        channel="Risk Manager",
    )
    return {"ok": True, "suggestion": saved}


@app.get("/api/library")
def library(contract: str = "ES1!", q: str = ""):
    from futuresfund.crew import get_crew
    from futuresfund.library import search_library

    return get_crew(board).application("Opening the strategy library", lambda: search_library(contract, q))


class LibraryAdd(BaseModel):
    title: str = ""
    contract: str = "ES1!"
    pine: str = ""
    timeframe: str = ""


@app.post("/api/library")
def library_add(body: LibraryAdd):
    from futuresfund.crew import get_crew
    from futuresfund.library import add_strategy

    def add():
        try:
            return add_strategy(body.title, body.contract, body.pine, body.timeframe)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return get_crew(board).application("Adding a library strategy", add, announce=True)


@app.delete("/api/library/{strategy_id}")
def library_delete(strategy_id: str, contract: str = ""):
    from futuresfund.crew import get_crew
    from futuresfund.library import remove_strategy

    def remove():
        if not remove_strategy(strategy_id, contract or None):
            raise HTTPException(status_code=404, detail="That strategy is not on the shelf.")
        return {"ok": True, "id": strategy_id}

    return get_crew(board).application("Removing a library strategy", remove, announce=True)


@app.get("/api/library/{strategy_id}")
def library_one(strategy_id: str, contract: str = ""):
    from futuresfund.crew import get_crew
    from futuresfund.library import library_script

    def open_script():
        row = library_script(strategy_id, contract or None)
        if row is None or not row.get("pine"):
            raise HTTPException(status_code=404, detail="That strategy script is not in the library.")
        return row

    return get_crew(board).application("Opening a library script", open_script, announce=True)


@app.get("/api/indicators")
def indicators_list(q: str = ""):
    from futuresfund.crew import get_crew
    from futuresfund.indicators import all_indicators

    def work():
        needle = q.strip().lower()
        rows = []
        for row in all_indicators():
            text = f"{row['name']} {row.get('summary', '')} {' '.join(row.get('scripts', []))}".lower()
            if needle and needle not in text:
                continue
            rows.append({key: row.get(key) for key in ("id", "name", "kind", "signature", "summary", "scripts", "engine", "added_by", "added_at")})
        return {"indicators": rows}

    return get_crew(board).application("Opening the indicator library", work)


@app.get("/api/indicators/{indicator_id:path}")
def indicator_one(indicator_id: str):
    from futuresfund.crew import get_crew
    from futuresfund.indicators import indicator

    def work():
        row = indicator(indicator_id)
        if row is None:
            raise HTTPException(status_code=404, detail="That indicator is not in the library.")
        if row["kind"] == "built-in":
            text = f"// {row['signature']}\n// {row['summary']}\n"
            if row.get("example"):
                text += f"// Used in {row['scripts'][0] if row['scripts'] else 'a script'}:\n{row['example']}\n"
        else:
            text = f"// {row['name']} from {', '.join(row['scripts'][:3])}\n"
            if row.get("summary"):
                text += f"// {row['summary']}\n"
            text += row["source"] + "\n"
            for variant in row.get("variants", [])[:2]:
                text += f"\n// Another version, from {variant['script']}:\n{variant['source']}\n"
        return {"id": row["id"], "title": row["name"], "pine": text}

    return get_crew(board).application("Opening an indicator", work)


@app.get("/api/leaders/{strategy_id}")
def leader(strategy_id: str):
    from futuresfund.strategy import leader_script

    row = leader_script(strategy_id)
    if row is None or not row.get("pine"):
        raise HTTPException(status_code=404, detail="That strategy script is not on headquarters.")
    return row


@app.get("/api/sentiment")
def sentiment():
    """Bull, bear, and risk on the last closed 18:00–16:00 session, with political and energy headlines."""
    from futuresfund.sentiment import current_gauge

    return current_gauge()


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


@app.post("/api/engines/{engine_id}/skip")
def skip_engine(engine_id: int):
    """Skip the script on this engine and take the next one."""
    from futuresfund.backtrader_engine import request_skip

    try:
        return request_skip(engine_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/engines/{engine_id}/restart")
def restart_engine(engine_id: int):
    """Stop a hung backtest on this engine. Engine 4 then takes a learning script."""
    from futuresfund.learn import restart_research_engine

    try:
        return restart_research_engine(engine_id, board)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/hold-new")
def hold_new_scripts():
    """The strategy team stops writing a new script. The chart engines keep backtesting the learning folder."""
    from futuresfund.crew import get_crew
    from futuresfund.learn import hold_chart_research

    return get_crew(board).application("Holding new strategies", lambda: hold_chart_research(board))


@app.post("/api/runs/release-new")
def release_new_scripts():
    """The strategy team may write scripts again."""
    from futuresfund.crew import get_crew
    from futuresfund.learn import release_chart_research

    return get_crew(board).application("Taking new scripts", lambda: release_chart_research(board))


@app.post("/api/runs/stop")
def stop_run():
    from futuresfund.crew import get_crew
    from futuresfund.learn import end_research

    return get_crew(board).application("Stopping the researchers", lambda: end_research(board))


@app.post("/api/runs/start")
def start_run():
    from futuresfund.crew import get_crew
    from futuresfund.learn import begin_research

    return get_crew(board).application("Starting the researchers", lambda: begin_research(board))


@app.get("/hooks/tradingview/{token}")
def tradingview_ready(token: str):
    """Opening the alert address in a browser shows that it is live. TradingView still posts the prices."""
    expected = webhook_token()
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Unknown webhook.")
    last = _last_price_bar()
    return PlainTextResponse(
        "This TradingView alert address is live.\n"
        "TradingView sends each price with a POST. Opening this page does not send a price.\n"
        f"Last price bar: {last}\n"
    )


def _last_price_bar() -> str:
    import json

    from futuresfund.config import LIVE_BARS_PATH

    try:
        rows = json.loads(LIVE_BARS_PATH.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError):
        return "none stored"
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict):
        return "none stored"
    row = rows[-1]
    return f"{row.get('root') or row.get('instrument') or ''} {row.get('timeframe') or ''} {row.get('t') or ''}".strip()


@app.post("/hooks/tradingview/{token}")
async def tradingview(token: str, request: Request):
    expected = webhook_token()
    if not expected:
        raise HTTPException(status_code=503, detail="Set TV_WEBHOOK_TOKEN before TradingView can call this desk.")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Unknown webhook.")
    raw = (await request.body()).decode("utf-8", errors="replace")
    if raw.strip() == "desk-link-check":
        return {"ok": True, "probe": "desk-link-check"}
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
    message: str = Field(min_length=1, max_length=60000)
    agent: str = "Portfolio Manager"


_chat_lock = threading.Lock()
_chat_busy: set[str] = set()
CHAT_PROMPT_LIMIT = 3500  # answer_chat clips the prompt here; the question must not be the part that is cut.


@app.post("/api/chat")
def chat(body: ChatRequest):
    text = body.message.strip()
    name = _agent_name(body.agent) or "Portfolio Manager"
    with _chat_lock:
        if name in _chat_busy:
            raise HTTPException(status_code=409, detail=f"{name} is still answering.")
        _chat_busy.add(name)
    from futuresfund.desk_chat import pine_script, script_title

    source = pine_script(text)
    if source:
        shown = text[: len(text) - len(source)].strip()
        title = script_title(source) or "a Pine script"
        shown = f"{shown} [pasted {title}, {len(source.splitlines())} lines]".strip()
    else:
        shown = text
    board.post("You", shown, kind="chat", channel=name)

    def work():
        try:
            answer = _ask_agent(name, text)
            board.post(name, answer, kind="chat", channel=name)
            _relay(name, answer)
        except Exception as exc:
            board.post(name, f"I could not answer just now: {exc}", kind="error", channel=name)
        finally:
            with _chat_lock:
                _chat_busy.discard(name)

    from futuresfund.crew import get_crew

    get_crew(board).submit(name, "Reading the firm book", work, during_stop=True)
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
    lines.append("None of these are live. Turn one on from the active strategy page. The trader then uses that script's stop, take profit, and exit.")
    from futuresfund.strategy import lead_script

    lead = lead_script()
    if lead:
        lines.append(
            f"The headquarters script is the lowest-drawdown profitable one of these: {lead['title']}. "
            f"{lead['trades']} trades, drawdown {lead['max_drawdown']}, profit {lead['net_profit']}. "
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
    """A question about the application is answered from the desk's records first. The model answers the rest."""
    from futuresfund.book import firm_record, open_trade_brief
    from futuresfund.desk_chat import answer, facts_for
    from futuresfund.llm import answer_chat
    from futuresfund.roster import ROSTER

    direct = answer(board, name, message)
    if direct:
        return direct
    view = book_snapshot()
    asked = message.lower()
    trade_detail = ""
    if any(word in asked for word in ("stop", "take profit", "take-profit", "open trade", "current trade")):
        trade_detail = open_trade_brief(view)
    names = ", ".join(agent["name"] for agent in ROSTER)
    head = (
        f"You are {name} on this futures desk. A person asked you something. "
        "Answer the question that was asked, directly, in the first sentence. Do not summarize the desk unless asked to. "
        "Use the facts and the firm record below. If a number is not there, say you do not have that number. "
        "Do not invent a profit, a drawdown, a trade count, or an open position. "
        "Do not place an order. Do not debate whether a trade should have been taken. "
        "Do not write a To: line. "
        f"The desks are: {names}.\n\n"
        f"Question: {message[:1200]}\n\n"
        f"Facts for this question:\n{facts_for(board, message)}\n\n"
        + (f"Open trade detail:\n{trade_detail}\n\n" if trade_detail else "")
    )
    # The chat model reads about 3500 characters. The question and the facts come first; the record fills the rest.
    room = max(600, CHAT_PROMPT_LIMIT - len(head) - 40)
    record = firm_record(view)
    if len(record) > room:
        record = record[: room - 1].rstrip() + "…"
    prompt = f"{head}Firm record:\n{record}\n\nAnswer the question: {message[:300]}"
    return answer_chat(name, prompt, board=board) or "I have nothing to add."


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
    from futuresfund.crew import get_crew

    reply = get_crew(board).run(
        target,
        f"Reading a note from {speaker}",
        lambda: _ask_agent(target, f"{speaker} handed you this written report: {handed}"),
        during_stop=True,
    )
    if not reply:
        return
    board.post(target, reply, kind="chat", channel=speaker)
    if target != speaker:
        board.post(target, reply, kind="chat", channel=target)


@app.on_event("startup")
def _startup():
    from futuresfund.crew import get_crew
    from futuresfund.engine_workers import start_engines
    from futuresfund.learn import load_hold

    if load_hold():
        board.set_hold_new(True)
    start_engines()
    get_crew(board)
    from futuresfund.kronos_feed import start_kronos_feed

    start_kronos_feed(board)
    threading.Thread(target=scheduler, args=(board,), daemon=True, name="futures-scheduler").start()
    from futuresfund.ninjatrader import start_listener

    start_listener(board)
    from futuresfund.live_trading import resume
    from futuresfund.trading_engine import start_trading_engines

    start_trading_engines()
    resume(board)

    def _boot():
        from futuresfund.discuss import load_chart_file
        from futuresfund.research import load_bars

        try:
            from futuresfund.learn import chart_progress

            chart_progress()
        except Exception:
            traceback.print_exc()

        try:
            from futuresfund.tunnel import supervise

            supervise(board)
        except Exception as exc:
            board.post("Systems Administrator", f"The phone address could not be watched: {exc}", kind="error", channel="Systems Administrator")
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
            "Processing is stopped. Press Start to run the Pine scripts. Each agent has their own thread. Each PineTS engine has its own process: engine 1 is the 2-minute chart, engine 2 is the 5-minute chart, and engine 3 is the 15-minute chart. Engine 4 is the creation engine. The strategy developers write new scripts from scratch the whole time research is running, and engine 4 develops them. A new library strategy is learned before the next script. The systems administrator serves the library and the other desk requests.",
            kind="system",
            channel="headquarters",
        )
        from futuresfund.crew import get_crew
        from futuresfund.llm import ensure_agent_models

        ensure_agent_models()
        get_crew(board)
        from futuresfund.indicators import seed

        held = seed()
        board.post(
            "Systems Administrator",
            f"The indicator library holds {held} indicators: the built-in ones the engine runs and the ones our scripts calculate. Every desk can read it.",
            kind="report",
            channel="Systems Administrator",
        )
        _rerank_notes_if_needed()
        _point_headquarters()
        _refold_library_if_needed()
        _recheck_library_if_stale()
        start_checks(force=True)

    threading.Thread(target=_boot, daemon=True, name="futures-boot").start()


def _point_headquarters() -> None:
    """Headquarters follows the best finished study under the objective until a meeting decides."""
    from futuresfund.strategy import absorb_research_notes

    try:
        absorb_research_notes()
    except Exception as exc:
        board.post("Chart researcher", f"Headquarters could not be pointed at the best study: {exc}", kind="error", channel="Chart researcher")


def _rerank_notes_if_needed() -> None:
    """Studies finished under an older objective get their best result chosen again under objective.py."""
    from futuresfund.learn import rerank_notes
    from futuresfund.objective import short

    try:
        changed = rerank_notes()
    except Exception as exc:
        board.post("Chart researcher", f"The finished studies could not be ranked again: {exc}", kind="error", channel="Chart researcher")
        return
    if changed:
        board.post(
            "Chart researcher",
            f"The research objective: {short().rstrip('.')}. {changed} finished studies had their best result "
            "chosen again under it. The trials are unchanged; only which attempt counts as best moved.",
            kind="report",
            channel="Chart researcher",
        )


def _refold_library_if_needed() -> None:
    """A shelf folded under an older objective is read again from the notes under objective.py."""
    from futuresfund.library import refold_if_needed

    try:
        changed = refold_if_needed()
    except Exception as exc:  # the shelf is not worth a failed boot
        board.post("Systems Administrator", f"The strategy library could not be refolded: {exc}", kind="error", channel="Systems Administrator")
        return
    if changed:
        board.post(
            "Systems Administrator",
            "The strategy library was refolded from the research notes. Each script keeps its most profitable run that trades at least 4 times a week. More than 7 a week still counts. "
            "Two or three trades stays off the shelf. Engine 4 measures each row again next.",
            kind="report",
            channel="Systems Administrator",
        )


def _recheck_library_if_stale() -> None:
    """Library rows measured on an older engine are measured again on engine 4 when the desk starts."""
    from futuresfund.library import LIBRARY_ENGINE, index_engine, recheck_library

    if index_engine() == LIBRARY_ENGINE:
        return
    board.post(
        "Systems Administrator",
        "The strategy library was measured on an older engine. Engine 4 is measuring each row again on PineTS.",
        kind="report",
        channel="Systems Administrator",
    )
    result = recheck_library()
    board.post(
        "Systems Administrator",
        f"The strategy library was measured again from {result['tested_from']} ET. "
        f"{result['kept']} rows are the most profitable run of their script at 130 to 600 trades a year, and {result['dropped']} left the shelf.",
        kind="report",
        channel="Systems Administrator",
    )
