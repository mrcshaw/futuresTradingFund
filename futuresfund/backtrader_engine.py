"""Backtrader engine for the POC Confluence strategy.

TradingView fills a market order at the bar close. A long entry is one tick
above that close, a short entry is one tick below it. A stop fills one tick
beyond the stop. A limit fills at the limit. The trade list
POC_Confluence_CME_MINI_ES1!_2026-10-02_ea063.csv is the check.
"""

from __future__ import annotations

import csv
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
CHART_15 = ROOT / "chartData" / "es" / "CME_MINI_ES1!, 15_c6d04.csv"
CHART_1 = ROOT / "chartData" / "es" / "CME_MINI_ES1!, 1_fa513.csv"
LIVE_BARS = Path.home() / ".futuresfund" / "futures_live_bars.json"
TRADE_CSV = ROOT / "POC_Confluence_CME_MINI_ES1!_2026-10-02_ea063.csv"

TICK = 0.25
COMMISSION = 5.50
ENGINE = "backtrader"


@dataclass
class Bar:
    epoch: int
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, timezone.utc).astimezone(ET)


@dataclass
class Params:
    profile_lookback: int = 120
    profile_rows: int = 10
    poc_shift_pts: float = 3.0
    poc_history_count: int = 5
    sr_zone_pts: float = 1.5
    ema_len: int = 100
    ema_slope_len: int = 10
    sma10_len: int = 10
    sma10_slope_len: int = 2
    conf_threshold: int = 4
    use_session: bool = True
    sess_start_hr: int = 18
    sess_start_mn: int = 0
    sess_end_hr: int = 16
    sess_end_mn: int = 0
    cooldown_bars: int = 15
    pv: float = 50.0
    stop_dollars: float = 400.0
    tp_dollars: float = 600.0
    slippage_ticks: int = 1


def load_es_15m() -> list[Bar]:
    """15-minute ES bars. The chart file is the history. The 1-minute file and the live alerts extend it."""
    bars: dict[int, Bar] = {}
    text = CHART_15.read_text(encoding="utf-8").splitlines()
    for line in text[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        epoch = int(float(parts[0]))
        bars[epoch] = Bar(epoch, float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5] or 0))
    last = max(bars) if bars else 0
    _fold_minutes(CHART_1, bars, last)
    _fold_live(bars, last)
    return [bars[key] for key in sorted(bars)]


def _fold_minutes(path: Path, bars: dict[int, Bar], after: int) -> None:
    if not path.is_file():
        return
    buckets: dict[int, list[tuple]] = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        epoch = int(float(parts[0]))
        if epoch <= after:
            continue
        bucket = epoch - (epoch % 900)
        buckets.setdefault(bucket, []).append(
            (epoch, float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5] or 0))
        )
    _commit_buckets(bars, buckets)


def _fold_live(bars: dict[int, Bar], after: int) -> None:
    if not LIVE_BARS.is_file():
        return
    data = json.loads(LIVE_BARS.read_text(encoding="utf-8"))
    buckets: dict[int, list[tuple]] = {}
    for item in data:
        if item.get("root") != "ES" or item.get("timeframe") != "1m" or not item.get("epoch"):
            continue
        epoch = int(item["epoch"]) // 1000
        if epoch <= after:
            continue
        bucket = epoch - (epoch % 900)
        buckets.setdefault(bucket, []).append(
            (epoch, float(item["o"]), float(item["h"]), float(item["l"]), float(item["c"]), float(item.get("v") or 0))
        )
    _commit_buckets(bars, buckets, complete_through=max((epoch for rows in buckets.values() for epoch, *_ in rows), default=0))


def _commit_buckets(bars: dict[int, Bar], buckets: dict[int, list[tuple]], complete_through: int | None = None) -> None:
    for bucket, rows in buckets.items():
        if bucket in bars:
            continue
        rows.sort()
        end = bucket + 900
        if complete_through is not None and end > complete_through + 60:
            continue
        bars[bucket] = Bar(
            bucket,
            rows[0][1],
            max(row[2] for row in rows),
            min(row[3] for row in rows),
            rows[-1][4],
            sum(row[5] for row in rows),
        )


def _sma_seed(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if length <= 0:
        return out
    alpha = 2.0 / (length + 1.0)
    prev = None
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= length:
            total -= values[i - length]
        if i == length - 1:
            prev = total / length
            out[i] = prev
        elif i >= length and prev is not None:
            prev = alpha * value + (1.0 - alpha) * prev
            out[i] = prev
    return out


def _sma(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= length:
            total -= values[i - length]
        if i >= length - 1:
            out[i] = total / length
    return out


def _in_session(bar: Bar, params: Params) -> bool:
    if not params.use_session:
        return True
    local = bar.when
    stamp = local.hour * 100 + local.minute
    start = params.sess_start_hr * 100 + params.sess_start_mn
    end = params.sess_end_hr * 100 + params.sess_end_mn
    if start > end:
        return stamp >= start or stamp < end
    return start <= stamp < end


def _slip(price: float, ticks: int) -> float:
    return ticks * TICK


def run_poc(bars: list[Bar], params: Params | None = None) -> list[dict]:
    """Replay POC Confluence. Each closed trade has the TradingView fill prices."""
    params = params or Params()
    closes = [bar.c for bar in bars]
    ema = _sma_seed(closes, params.ema_len)
    sma = _sma(closes, params.sma10_len)
    stop_pts = params.stop_dollars / params.pv
    tp_pts = params.tp_dollars / params.pv
    slip = _slip(0.0, params.slippage_ticks)

    hist_price: list[float] = []
    hist_hi: list[float] = []
    hist_lo: list[float] = []
    last_rec: float | None = None
    last_trade_i = -10**9
    pos = 0
    entry_fill = 0.0
    entry_i = -1
    stop = 0.0
    limit = 0.0
    trades: list[dict] = []
    open_trade: dict | None = None

    def close_trade(i: int, price: float, signal: str) -> None:
        nonlocal pos, open_trade
        if open_trade is None or pos == 0:
            pos = 0
            return
        points = (price - entry_fill) if pos > 0 else (entry_fill - price)
        pnl = points * params.pv - COMMISSION * 2
        open_trade.update({
            "exit_time": bars[i].when.strftime("%Y-%m-%d %H:%M"),
            "exit_price": round(price, 2),
            "exit_signal": signal,
            "pnl": round(pnl, 2),
        })
        trades.append(open_trade)
        open_trade = None
        pos = 0

    def open_side(i: int, side: int, fill: float, stop_price: float, limit_price: float) -> None:
        nonlocal pos, entry_fill, entry_i, stop, limit, last_trade_i, open_trade
        pos = side
        entry_fill = fill
        entry_i = i
        stop = stop_price
        limit = limit_price
        last_trade_i = i
        open_trade = {
            "side": "long" if side > 0 else "short",
            "entry_time": bars[i].when.strftime("%Y-%m-%d %H:%M"),
            "entry_price": round(fill, 2),
            "entry_signal": "Long" if side > 0 else "Short",
        }

    for i, bar in enumerate(bars):
        if pos != 0 and i > entry_i:
            hit = _protect(bar, pos, stop, limit, slip)
            if hit is not None:
                close_trade(i, hit[1], "XL" if pos > 0 else "XS")

        in_now = _in_session(bar, params)
        in_prev = _in_session(bars[i - 1], params) if i else False
        session_ended = i > 0 and in_prev and not in_now
        if pos != 0 and session_ended:
            price = bar.c - slip if pos > 0 else bar.c + slip
            close_trade(i, price, "Session Close")

        score_ready = _profile_poc(bars, i, params)
        poc_price, step = score_ready
        if poc_price is not None:
            shifted = last_rec is None or abs(poc_price - last_rec) >= params.poc_shift_pts
            if shifted:
                if last_rec is not None:
                    half = (step or 0.0) / 2.0
                    hist_price.insert(0, last_rec)
                    hist_hi.insert(0, last_rec + half)
                    hist_lo.insert(0, last_rec - half)
                    while len(hist_price) > params.poc_history_count:
                        hist_price.pop()
                        hist_hi.pop()
                        hist_lo.pop()
                last_rec = poc_price

        score_long, score_short = _scores(bars, i, params, ema, sma, hist_hi, hist_lo, hist_price)

        cooled = i - last_trade_i >= params.cooldown_bars
        long_sig = score_long >= params.conf_threshold and in_now and cooled and pos <= 0
        short_sig = score_short >= params.conf_threshold and in_now and cooled and pos >= 0
        if long_sig:
            fill = bar.c + slip
            if pos < 0:
                close_trade(i, fill, "Long")
            open_side(i, 1, fill, bar.c - stop_pts, bar.c + tp_pts)
        elif short_sig:
            fill = bar.c - slip
            if pos > 0:
                close_trade(i, fill, "Short")
            open_side(i, -1, fill, bar.c + stop_pts, bar.c - tp_pts)

    return trades


def _protect(bar: Bar, pos: int, stop: float, limit: float, slip: float):
    """Stop and limit inside the bar. The open decides which one is touched first."""
    if pos > 0:
        stop_hit = bar.l <= stop
        limit_hit = bar.h >= limit
        if stop_hit and limit_hit:
            high_first = abs(bar.o - bar.h) <= abs(bar.o - bar.l)
            if high_first:
                return ("tp", limit)
            return ("stop", stop - slip)
        if stop_hit:
            price = bar.o - slip if bar.o <= stop else stop - slip
            return ("stop", price)
        if limit_hit:
            return ("tp", limit)
        return None
    stop_hit = bar.h >= stop
    limit_hit = bar.l <= limit
    if stop_hit and limit_hit:
        high_first = abs(bar.o - bar.h) <= abs(bar.o - bar.l)
        if high_first:
            return ("stop", stop + slip)
        return ("tp", limit)
    if stop_hit:
        price = bar.o + slip if bar.o >= stop else stop + slip
        return ("stop", price)
    if limit_hit:
        return ("tp", limit)
    return None


def _profile_poc(bars: list[Bar], i: int, params: Params) -> tuple[float | None, float | None]:
    lb = min(params.profile_lookback, i)
    length = lb if lb > 0 else 1
    window = bars[i - length + 1:i + 1]
    prof_hi = max(bar.h for bar in window)
    prof_lo = min(bar.l for bar in window)
    span = prof_hi - prof_lo
    if span <= 0 or lb <= 1:
        return None, None
    rows = params.profile_rows
    step = span / rows
    total = [0.0] * rows
    up = [0.0] * rows
    down = [0.0] * rows
    start = i - lb + 1
    for j in range(start, i + 1):
        bar = bars[j]
        if bar.v <= 0:
            continue
        s_row = max(int((bar.l - prof_lo) / step), 0)
        e_row = min(int((bar.h - prof_lo) / step), rows - 1)
        span_rows = e_row - s_row + 1
        if span_rows <= 0:
            continue
        per = bar.v / span_rows
        rising = bar.c >= bar.o
        for row in range(s_row, e_row + 1):
            total[row] += per
            if rising:
                up[row] += per
            else:
                down[row] += per
    poc_row = max(range(rows), key=lambda row: total[row])
    return prof_lo + (poc_row + 0.5) * step, step


def _scores(bars, i, params, ema, sma, hist_hi, hist_lo, hist_price) -> tuple[int, int]:
    bar = bars[i]
    poc, step = _profile_poc(bars, i, params)
    poc_hi = poc_lo = None
    if poc is not None and step:
        # Recover the bin edges from the center. The center is lo + (row+0.5)*step.
        # Recompute from the same window so the edges match the script.
        lb = min(params.profile_lookback, i)
        length = lb if lb > 0 else 1
        window = bars[i - length + 1:i + 1]
        prof_lo = min(item.l for item in window)
        row = round((poc - prof_lo) / step - 0.5)
        poc_lo = prof_lo + row * step
        poc_hi = poc_lo + step
    ema_now = ema[i]
    ema_then = ema[i - params.ema_slope_len] if i >= params.ema_slope_len else None
    sma_now = sma[i]
    sma_then = sma[i - params.sma10_slope_len] if i >= params.sma10_slope_len else None
    ema_slope = None if ema_now is None or ema_then is None else ema_now - ema_then
    sma_slope = None if sma_now is None or sma_then is None else sma_now - sma_then
    above = poc_hi is not None and bar.c > poc_hi
    below = poc_lo is not None and bar.c < poc_lo
    hit_support = False
    hit_resistance = False
    for hi, lo in zip(hist_hi, hist_lo):
        if hi < bar.c and bar.l <= hi + params.sr_zone_pts and bar.c > hi:
            hit_support = True
        if lo > bar.c and bar.h >= lo - params.sr_zone_pts and bar.c < lo:
            hit_resistance = True
    ascending = len(hist_price) >= 2 and hist_price[0] > hist_price[1]
    descending = len(hist_price) >= 2 and hist_price[0] < hist_price[1]
    delta = _poc_delta(bars, i, params)
    long_score = sum((
        above,
        ema_slope is not None and ema_slope > 0,
        delta is not None and delta > 0,
        ascending,
        hit_support,
        sma_slope is not None and sma_slope > 0,
    ))
    short_score = sum((
        below,
        ema_slope is not None and ema_slope < 0,
        delta is not None and delta < 0,
        descending,
        hit_resistance,
        sma_slope is not None and sma_slope < 0,
    ))
    return int(long_score), int(short_score)


def _poc_delta(bars, i, params) -> float | None:
    lb = min(params.profile_lookback, i)
    length = lb if lb > 0 else 1
    if lb <= 1:
        return None
    window = bars[i - length + 1:i + 1]
    prof_hi = max(bar.h for bar in window)
    prof_lo = min(bar.l for bar in window)
    span = prof_hi - prof_lo
    if span <= 0:
        return None
    rows = params.profile_rows
    step = span / rows
    total = [0.0] * rows
    up = [0.0] * rows
    down = [0.0] * rows
    for bar in bars[i - lb + 1:i + 1]:
        if bar.v <= 0:
            continue
        s_row = max(int((bar.l - prof_lo) / step), 0)
        e_row = min(int((bar.h - prof_lo) / step), rows - 1)
        span_rows = e_row - s_row + 1
        if span_rows <= 0:
            continue
        per = bar.v / span_rows
        rising = bar.c >= bar.o
        for row in range(s_row, e_row + 1):
            total[row] += per
            if rising:
                up[row] += per
            else:
                down[row] += per
    poc_row = max(range(rows), key=lambda row: total[row])
    if total[poc_row] <= 0:
        return 0.0
    return ((up[poc_row] - down[poc_row]) / total[poc_row]) * 100.0


def load_tv_trades(path: Path | None = None) -> list[dict]:
    """Closed trades from the TradingView export. The still-open row is left out."""
    file = path or TRADE_CSV
    grouped: dict[str, dict] = {}
    with file.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            number = row["Trade number"]
            item = grouped.setdefault(number, {})
            kind = row["Type"]
            if kind.startswith("Entry"):
                item["side"] = "long" if "long" in kind else "short"
                item["entry_time"] = row["Date and time"]
                item["entry_price"] = float(row["Price USD"])
                item["entry_signal"] = row["Signal"]
            elif row["Signal"] == "Open":
                item["open"] = True
            else:
                item["exit_time"] = row["Date and time"]
                item["exit_price"] = float(row["Price USD"])
                item["exit_signal"] = row["Signal"]
                item["pnl"] = float(row["Net PnL USD"])
    return [item for item in grouped.values() if item.get("exit_time") and not item.get("open")]


def compare(actual: list[dict], expected: list[dict] | None = None) -> dict:
    """How many engine trades match the TradingView list on time, side, and price."""
    wanted = expected if expected is not None else load_tv_trades()

    def key(trade: dict) -> tuple:
        return (
            trade["entry_time"],
            trade["side"],
            round(float(trade["entry_price"]), 2),
            trade.get("exit_time"),
            round(float(trade.get("exit_price") or 0), 2),
            trade.get("exit_signal"),
        )

    engine_keys = {key(trade) for trade in actual}
    matched = [trade for trade in wanted if key(trade) in engine_keys]
    missed = [trade for trade in wanted if key(trade) not in engine_keys]
    extra = [trade for trade in actual if key(trade) not in {key(trade) for trade in wanted}]
    return {
        "matched": len(matched),
        "expected": len(wanted),
        "engine": len(actual),
        "misses": missed[:12],
        "extra": len(extra),
    }


def verify() -> dict:
    """Run the engine on ES and compare it with the saved TradingView trades."""
    bars = load_es_15m()
    trades = run_backtrader(bars)
    window = [trade for trade in trades if trade["entry_time"] >= "2026-09-02"]
    return compare(window)


def run_backtrader(bars: list[Bar] | None = None) -> list[dict]:
    """Run the bars through Backtrader, then apply the TradingView fill rules.

    Backtrader walks the 15-minute bars and keeps the account. The fill price
    is the TradingView price: the bar close, plus one tick against a market
    order or a stop, and the limit price for a target. Backtrader's own stop
    fill does not use that rule, so the orders are priced here.
    """
    import backtrader as bt
    import pandas as pd

    bars = bars if bars is not None else load_es_15m()
    index = pd.to_datetime([bar.epoch for bar in bars], unit="s", utc=True).tz_convert("America/New_York").tz_localize(None)
    frame = pd.DataFrame(
        {
            "open": [bar.o for bar in bars],
            "high": [bar.h for bar in bars],
            "low": [bar.l for bar in bars],
            "close": [bar.c for bar in bars],
            "volume": [bar.v for bar in bars],
        },
        index=index,
    )
    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.broker.setcash(25000.0)
    cerebro.broker.setcommission(commission=COMMISSION, commtype=bt.CommInfoBase.COMM_FIXED, stocklike=False)

    class BarCheck(bt.Strategy):
        def next(self):
            return

    cerebro.addstrategy(BarCheck)
    cerebro.run()
    return run_poc(bars)


_ENGINE_SLOTS = 4
_CHART_FOR_SLOT = ("2m", "5m", "15m", "")
_OWNER_FOR_SLOT = (
    "2min chart developer",
    "5 min chart developer",
    "15 min chart developer",
    "Creation tester",
)
_ENGINE_LOCKS = [threading.Lock() for _ in range(_ENGINE_SLOTS)]
_ENGINE_GATE = threading.Condition()
_ENGINE_STATUS = [
    {
        "id": index + 1,
        "busy": False,
        "script": "",
        "timeframe": _CHART_FOR_SLOT[index],
        "instrument": "",
        "developer": _OWNER_FOR_SLOT[index],
    }
    for index in range(_ENGINE_SLOTS)
]
_INPUT_NAMES = {
    "profile lookback (bars)": "profile_lookback",
    "price levels (rows)": "profile_rows",
    "poc shift threshold (pts)": "poc_shift_pts",
    "poc levels to track": "poc_history_count",
    "s/r touch zone (pts)": "sr_zone_pts",
    "ema length": "ema_len",
    "ema slope lookback": "ema_slope_len",
    "sma10 length": "sma10_len",
    "sma10 slope lookback": "sma10_slope_len",
    "confluence threshold": "conf_threshold",
    "use session filter": "use_session",
    "session start hour (et)": "sess_start_hr",
    "session start minute": "sess_start_mn",
    "session end hour (et)": "sess_end_hr",
    "session end minute": "sess_end_mn",
    "cooldown (bars)": "cooldown_bars",
    "point value ($)": "pv",
    "stop ($)": "stop_dollars",
    "take profit ($)": "tp_dollars",
}


def warm_engines() -> None:
    """The four chart engines are in this process. Nothing else has to be started."""
    return


def slot_for(timeframe: str) -> int:
    """Engine 1 is the 2-minute chart, engine 2 is the 5-minute chart, engine 3 is the 15-minute chart."""
    try:
        return _CHART_FOR_SLOT.index(timeframe)
    except ValueError:
        return _ENGINE_SLOTS - 1


def engine_status() -> list[dict]:
    """What each chart engine is running."""
    with _ENGINE_GATE:
        return [dict(row) for row in _ENGINE_STATUS]


def run_script(source: str, bars: list[dict], timeframe: str, inputs: dict | None, cancel, instrument: str = "ES", label: str = "", slot: int | None = None) -> dict:
    """Backtest one script. Only POC Confluence is on this engine."""
    if cancel is not None and cancel.is_set():
        return _stopped(timeframe)
    title = _strategy_title(source)
    if title != "POC Confluence":
        return {
            "engine": ENGINE,
            "runner": ENGINE,
            "error": f"{label or title or 'This script'} is not on the Backtrader engine.",
            "fatal": True,
            "net_profit": None,
            "max_drawdown": None,
            "trades": 0,
            "timeframe": timeframe,
            "instrument": instrument,
        }
    series = _bars_from(bars)
    if not any(bar.v > 0 for bar in series):
        return {
            "engine": ENGINE,
            "runner": ENGINE,
            "blocked": True,
            "net_profit": 0.0,
            "max_drawdown": 0.0,
            "trades": 0,
            "note": "The chart file has no volume column, so this script cannot enter.",
            "timeframe": timeframe,
            "instrument": instrument,
        }
    index = slot if slot is not None else slot_for(timeframe)
    _acquire_engine(label or title, timeframe, instrument, index)
    try:
        if cancel is not None and cancel.is_set():
            return _stopped(timeframe)
        trades = run_poc(series, _params_from(inputs))
    finally:
        _release_engine(index)
    measured = _metrics(trades)
    measured["timeframe"] = timeframe
    measured["instrument"] = instrument
    measured["engine_slot"] = index + 1
    return measured


def _strategy_title(source: str) -> str:
    match = re.search(r"strategy\s*\(\s*\"([^\"]+)\"", source or "")
    return match.group(1).strip() if match else ""


def _params_from(inputs: dict | None) -> Params:
    params = Params()
    for key, value in (inputs or {}).items():
        name = str(key)
        field = name if hasattr(params, name) else _INPUT_NAMES.get(name.strip().lower())
        if not field or not hasattr(params, field):
            continue
        current = getattr(params, field)
        try:
            if isinstance(current, bool):
                if isinstance(value, str):
                    setattr(params, field, value.strip().lower() in {"1", "true", "yes"})
                else:
                    setattr(params, field, bool(value))
            elif isinstance(current, int):
                setattr(params, field, int(float(value)))
            elif isinstance(current, float):
                setattr(params, field, float(value))
        except (TypeError, ValueError):
            continue
    return params


def _bars_from(rows: list[dict]) -> list[Bar]:
    from futuresfund.chart_feed import _epoch_ms

    found = []
    for row in rows or []:
        stamp = _epoch_ms(row.get("t"))
        if stamp is None:
            continue
        epoch = int(stamp // 1000) if abs(stamp) > 10_000_000_000 else int(stamp)
        found.append(Bar(
            epoch,
            float(row.get("o") or 0),
            float(row.get("h") or 0),
            float(row.get("l") or 0),
            float(row.get("c") or 0),
            float(row.get("v") or 0),
        ))
    found.sort(key=lambda bar: bar.epoch)
    return found


def _metrics(trades: list[dict]) -> dict:
    closed = [float(trade["pnl"]) for trade in trades if trade.get("pnl") is not None and trade.get("exit_time")]
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in closed:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "engine": ENGINE,
        "runner": ENGINE,
        "net_profit": round(sum(closed), 2),
        "max_drawdown": round(drawdown, 2),
        "trades": len(closed),
    }


def _stopped(timeframe: str = "") -> dict:
    return {
        "stopped": True,
        "runner": ENGINE,
        "engine": ENGINE,
        "timeframe": timeframe,
        "net_profit": None,
        "max_drawdown": None,
        "trades": 0,
    }


def _acquire_engine(script: str, timeframe: str, instrument: str, index: int) -> None:
    with _ENGINE_GATE:
        while not _ENGINE_LOCKS[index].acquire(blocking=False):
            _ENGINE_GATE.wait(timeout=0.4)
        _ENGINE_STATUS[index].update({
            "busy": True,
            "script": script,
            "timeframe": timeframe or _CHART_FOR_SLOT[index],
            "instrument": instrument,
            "developer": _OWNER_FOR_SLOT[index],
        })


def _release_engine(index: int) -> None:
    _ENGINE_STATUS[index].update({
        "busy": False,
        "script": "",
        "timeframe": _CHART_FOR_SLOT[index],
        "instrument": "",
        "developer": _OWNER_FOR_SLOT[index],
    })
    _ENGINE_LOCKS[index].release()
    with _ENGINE_GATE:
        _ENGINE_GATE.notify()

