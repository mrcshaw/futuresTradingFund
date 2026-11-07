"""PineTS engine for the POC Confluence strategy.

TradingView fills a market order at the bar close. A long entry is one tick
above that close, a short entry is one tick below it. A stop fills one tick
beyond the stop. A limit fills at the limit. The trade list
POC_Confluence_CME_MINI_ES1!_2026-10-02_ea063.csv is the check.
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from bisect import bisect_left
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
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
ENGINE = "pinets"
# TradingView's "Last 30 days", used when a result is compared with a TradingView export.
# Research backtests use objective.window_start: the last year of futures sessions.
TESTER_DAYS = 30
# Bars run ahead of a proving stage's window so its indicators are settled. Their trades do not count.
WARMUP_BARS = 2000


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
        if i and i % 250 == 0:
            time.sleep(0)
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
                for field, name in (("favorable", "Favorable excursion USD"), ("adverse", "Adverse excursion USD")):
                    try:
                        item[field] = float(row.get(name) or "")
                    except ValueError:
                        pass
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
    """Run the bars through PineTS, then apply the TradingView fill rules.

    PineTS walks the 15-minute bars and keeps the account. The fill price
    is the TradingView price: the bar close, plus one tick against a market
    order or a stop, and the limit price for a target. PineTS's own stop
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


def _slot_table() -> tuple[int, int, tuple, tuple, tuple]:
    """Every slot from roster.ENGINE_DESKS: three chart engines per instrument, and the creation engine at slot 3."""
    from futuresfund.roster import CREATION_DEVELOPER, CREATION_ENGINE_ID, ENGINE_DESKS

    count_ = len(ENGINE_DESKS) + 1
    creation = CREATION_ENGINE_ID - 1
    charts, roots, owners = [""] * count_, [""] * count_, [CREATION_DEVELOPER] * count_
    for desk in ENGINE_DESKS:
        index = desk["engine"] - 1
        charts[index] = desk["timeframe"]
        roots[index] = desk["root"]
        owners[index] = desk["developer"]
    return count_, creation, tuple(charts), tuple(roots), tuple(owners)


# 22 slots today: engines 1-3 ES 2m/5m/15m, 4 creation, then 5-7 GC, 8-10 NQ, 11-13 MNQ, 14-16 MES, 17-19 M2K, 20-22 YM.
_ENGINE_SLOTS, _CREATION_SLOT, _CHART_FOR_SLOT, _ROOT_FOR_SLOT, _OWNER_FOR_SLOT = _slot_table()
_ENGINE_LOCKS = [threading.Lock() for _ in range(_ENGINE_SLOTS)]
_ENGINE_GATE = threading.Condition()
_SKIP = [threading.Event() for _ in range(_ENGINE_SLOTS)]
_RESTART = [threading.Event() for _ in range(_ENGINE_SLOTS)]
_ENGINE_LOGS = [deque(maxlen=40) for _ in range(_ENGINE_SLOTS)]
_ENGINE_RESULTS = [deque(maxlen=20) for _ in range(_ENGINE_SLOTS)]
_ENGINE_RUNS = count(1)
_ENGINE_STATUS = [
    {
        "id": index + 1,
        "busy": False,
        "script": "",
        "timeframe": _CHART_FOR_SLOT[index],
        # A chart engine always runs its own instrument. The creation engine names the draft's while it runs.
        "root": _ROOT_FOR_SLOT[index],
        "instrument": _ROOT_FOR_SLOT[index],
        "creation": index == _CREATION_SLOT,
        "developer": _OWNER_FOR_SLOT[index],
        "study": "",
        "studying": False,
        "successes": 0,
        "goal": 10,
        "scale": 0.0,
        "scale_move": "",
        "scale_phase": "",
        "notice": "",
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
    """Start the engine processes if the desk has not started them yet."""
    from futuresfund.engine_workers import start_engines

    start_engines()


def slot_for(timeframe: str, root: str | None = None) -> int:
    """The slot of the chart engine for this instrument's chart. Without a root it is the ES chart (engines 1-3).

    Anything that is not a chart engine's chart is the creation engine.
    """
    wanted = str(root or "ES").upper()
    for index, (chart, owner_root) in enumerate(zip(_CHART_FOR_SLOT, _ROOT_FOR_SLOT)):
        if chart and chart == timeframe and owner_root == wanted:
            return index
    return _CREATION_SLOT


def _valid(index) -> bool:
    return index is not None and 0 <= int(index) < _ENGINE_SLOTS


def set_engine_notice(index: int, text: str) -> None:
    """A reason this engine is not inside a backtest. Blank clears it."""
    if index is None or index < 0 or index >= _ENGINE_SLOTS:
        return
    with _ENGINE_GATE:
        _ENGINE_STATUS[index]["notice"] = str(text or "")


def begin_study(index: int, script: str, goal: int = 10, *, limit: int = 0, stage: int = 0) -> None:
    """The script now running on this engine. A different script starts its success count over.

    limit is the attempts the search that starts now has, and stage is its test in days (7, 30, 90, or
    365 for the year), so the page can show "attempt N of M · 7-day test". 0 means not known yet.
    """
    if not _valid(index):
        return
    with _ENGINE_GATE:
        row = _ENGINE_STATUS[index]
        if row.get("study") != script:
            row["successes"] = 0
            row["scale"] = 0.0
            row["scale_move"] = ""
            row["scale_phase"] = ""
        row["study"] = script
        row["studying"] = True
        row["goal"] = int(goal)
        row["attempt"] = 0
        row["attempt_limit"] = int(limit or 0)
        row["stage"] = int(stage or 0)


def record_attempt(index: int, number: int) -> None:
    """The attempt this engine just finished on the script it is studying."""
    if index < 0 or index >= _ENGINE_SLOTS:
        return
    with _ENGINE_GATE:
        _ENGINE_STATUS[index]["attempt"] = int(number)


def record_scale(index: int, position: float, move: str, phase: str) -> None:
    """Where this engine sits between the opposite of the goal and the goal."""
    if index < 0 or index >= _ENGINE_SLOTS:
        return
    with _ENGINE_GATE:
        row = _ENGINE_STATUS[index]
        row["scale"] = round(max(0.0, min(1.0, float(position))), 4)
        row["scale_move"] = move
        row["scale_phase"] = phase


def record_research(index: int | None, key: str) -> None:
    """One more of model_choices, search_choices, rewrites_tried, or rewrites_kept since the desk started."""
    if index is None or index < 0 or index >= _ENGINE_SLOTS:
        return
    with _ENGINE_GATE:
        row = _ENGINE_STATUS[index]
        row[key] = int(row.get(key) or 0) + 1


def set_engine_thinking(index: int | None, text: str) -> None:
    """What the researcher is working out while this engine waits. Blank clears it."""
    if index is None or index < 0 or index >= _ENGINE_SLOTS:
        return
    with _ENGINE_GATE:
        _ENGINE_STATUS[index]["thinking"] = str(text or "")


def record_success(index: int) -> int:
    """One more profitable run inside the one-year drawdown cap on the script this engine is studying."""
    if not _valid(index):
        return 0
    with _ENGINE_GATE:
        row = _ENGINE_STATUS[index]
        row["successes"] = int(row.get("successes") or 0) + 1
        row["studying"] = True
        return row["successes"]


def clear_idle_engine(index: int) -> bool:
    """Clear a working flag when this engine has no script and no backtest holds the lock."""
    if index < 0 or index >= _ENGINE_SLOTS:
        return False
    lock = _ENGINE_LOCKS[index]
    if not lock.acquire(blocking=False):
        return False
    try:
        row = _ENGINE_STATUS[index]
        if str(row.get("script") or "").strip() or str(row.get("study") or "").strip():
            return False
        row["busy"] = False
        row["studying"] = False
        row["study"] = ""
        row["script"] = ""
        return True
    finally:
        lock.release()


def finish_study(index: int) -> None:
    """The engine is between scripts. The count belongs to the script that is actually running."""
    if not _valid(index):
        return
    with _ENGINE_GATE:
        row = _ENGINE_STATUS[index]
        row["studying"] = False
        row["study"] = ""
        row["attempt"] = 0
        row["attempt_limit"] = 0
        row["stage"] = 0
        row["successes"] = 0
        row["scale"] = 0.0
        row["scale_move"] = ""
        row["scale_phase"] = ""


def skipped(index: int) -> bool:
    """True after Skip is pressed for this engine, until the study consumes it."""
    if index < 0 or index >= _ENGINE_SLOTS:
        return False
    return _SKIP[index].is_set()


def clear_skip(index: int) -> None:
    if 0 <= index < _ENGINE_SLOTS:
        _SKIP[index].clear()


def clear_skips() -> None:
    for event in _SKIP:
        event.clear()


def restarted(index: int) -> bool:
    """True after Restart is pressed for this engine, until its loop consumes it."""
    if index < 0 or index >= _ENGINE_SLOTS:
        return False
    return _RESTART[index].is_set()


def clear_restart(index: int) -> None:
    if 0 <= index < _ENGINE_SLOTS:
        _RESTART[index].clear()


def clear_restarts() -> None:
    for event in _RESTART:
        event.clear()


def _cut_engine(index: int) -> None:
    """Stop the backtest process now. The skip flag tells the desk to take the next script."""
    from futuresfund.engine_workers import active_desk

    desk = active_desk()
    if desk is not None:
        desk.restart(index)


def request_skip(engine_id: int) -> dict:
    """Skip the script on this engine. The chart or the creation team takes the next one.

    The flag is set before this returns. A running backtest is cut on a side thread so the
    button does not wait for the measurement to finish.
    """
    index = int(engine_id) - 1
    if index < 0 or index >= _ENGINE_SLOTS:
        raise ValueError(f"Engine {engine_id} is not on this desk.")
    # Skip pressed after a Restart is a skip: the study ends as skipped, and its stage does not resume.
    _RESTART[index].clear()
    _SKIP[index].set()
    with _ENGINE_GATE:
        row = _ENGINE_STATUS[index]
        busy = bool(row.get("busy"))
        script = row.get("study") or row.get("script") or "the current script"
    _engine_log(index, f"SKIP   requested  {script}")
    if busy:
        threading.Thread(target=_cut_engine, args=(index,), daemon=True, name=f"skip-{index + 1}").start()
    return {"ok": True, "engine": int(engine_id), "script": script}


def request_restart(engine_id: int) -> dict:
    """Stop a hung backtest on this engine and let it take the next script.

    The restart flag is set before the skip flag that cuts a running or queued backtest, so the search
    that sees the cut reads it as a Restart (learn._search sets trials.restarted): the study ends as
    "restarted", not "skipped", and its proving stage resumes on the script's next turn.
    """
    index = int(engine_id) - 1
    if index < 0 or index >= _ENGINE_SLOTS:
        raise ValueError(f"Engine {engine_id} is not on this desk.")
    with _ENGINE_GATE:
        row = _ENGINE_STATUS[index]
        busy = bool(row.get("busy"))
        # An engine waiting for a free worker has no worker to end yet. Its queued job is dropped instead.
        waiting = str(row.get("notice") or "").startswith("Waiting for a free worker")
        script = row.get("study") or row.get("script") or "the current script"
    _RESTART[index].set()
    if busy or waiting:
        _SKIP[index].set()
    from futuresfund.engine_workers import active_desk

    desk = active_desk()
    if desk is not None:
        desk.restart(index)
    _engine_log(index, f"RESTART requested  {script}")
    return {"ok": True, "engine": int(engine_id), "script": script, "busy": busy}


def skipped_measurement(timeframe: str = "") -> dict:
    """The result a backtest returns when Skip stops it."""
    return {
        "skipped": True,
        "stopped": True,
        "runner": ENGINE,
        "engine": ENGINE,
        "timeframe": timeframe,
        "net_profit": None,
        "max_drawdown": None,
        "trades": 0,
    }


def record_engine_result(index: int, result: dict) -> None:
    """Keep the last 20 finished runs on this engine."""
    if index < 0 or index >= _ENGINE_SLOTS or not isinstance(result, dict):
        return
    if result.get("net_profit") is None:
        return
    stamp = datetime.now(ET).strftime("%H:%M:%S")
    item = {
        "run": next(_ENGINE_RUNS),
        "script": str(result.get("script") or result.get("label") or ""),
        "time": str(result.get("time") or stamp),
        "net_profit": result.get("net_profit"),
        "max_drawdown": result.get("max_drawdown"),
        "profit_factor": result.get("profit_factor"),
        "profit_ratio": result.get("profit_ratio"),
        "trades": result.get("trades"),
        "win_rate": result.get("win_rate"),
        # What the run used, kept off the status report because the page asks for one script at a time.
        "_source": result.get("source") or "",
        "_inputs": dict(result.get("inputs") or {}),
        "_instrument": str(result.get("instrument") or "ES"),
        "_timeframe": str(result.get("timeframe") or ""),
    }
    with _ENGINE_GATE:
        _ENGINE_RESULTS[index].append(item)


def engine_result_script(run: int) -> dict | None:
    """The script and settings one result ran, while that result is still on its engine's list."""
    with _ENGINE_GATE:
        item = next((row for results in _ENGINE_RESULTS for row in results if row["run"] == run), None)
    if item is None or not item["_source"]:
        return None
    from futuresfund.contracts import root_of

    root = root_of(item["_instrument"])
    return {
        "title": item["script"],
        "pine": script_as_run(item["_source"], item["_inputs"], root),
        "note": f"What this run used on {root} {item['_timeframe']} at {item['time']}.",
    }


def clear_engine_results() -> None:
    with _ENGINE_GATE:
        for results in _ENGINE_RESULTS:
            results.clear()


def engine_status() -> list[dict]:
    """What each chart engine is running, with its recent log lines and last 20 results."""
    with _ENGINE_GATE:
        rows = []
        for row, logs, results in zip(_ENGINE_STATUS, _ENGINE_LOGS, _ENGINE_RESULTS):
            item = dict(row)
            item["system"] = "PineTS"
            item["logs"] = list(logs)
            item["results"] = [{key: value for key, value in result.items() if not key.startswith("_")} for result in results]
            rows.append(item)
        return rows


def _engine_log(index: int, text: str) -> None:
    stamp = datetime.now(ET).strftime("%H:%M:%S")
    with _ENGINE_GATE:
        _ENGINE_LOGS[index].append(f"{stamp}  {text}")


def _settings_line(inputs: dict | None) -> str:
    if not inputs:
        return "original settings"
    parts = [f"{key} {value}" for key, value in list(inputs.items())[:3]]
    extra = len(inputs) - len(parts)
    text = ", ".join(parts)
    if extra > 0:
        text += f", +{extra} more"
    return text


_INPUT_LABEL = re.compile(r"""(?m)^\s*(?:(?:var|varip|simple|const|int|float|bool)\s+)*(\w+)\s*=\s*input(?:\.\w+)?\s*\(\s*[^,\n]+,\s*(?:title\s*=\s*)?(["'])(.*?)\2""")


def input_names(source: str, inputs: dict | None) -> dict:
    """Key each input by the script's variable name.

    Research names an input by its label, such as "55 EMA Length". The script is
    rewritten by variable name, such as ema55_len, so a label left as the key
    changes nothing in the run.
    """
    if not inputs:
        return dict(inputs or {})
    by_label = {}
    for name, _quote, label in _INPUT_LABEL.findall(source or ""):
        by_label.setdefault(label, name)
    named = {}
    for key, value in inputs.items():
        name = key if key not in by_label or key in by_label.values() else by_label[key]
        if name in named and name != key:
            continue
        named[name] = value
    return named


def script_as_run(source: str, inputs: dict | None, root: str = "ES", *, as_written: bool = False) -> str:
    """The script text PineTS runs for these settings."""
    from futuresfund.strategy import _apply_inputs

    inputs = input_names(source, inputs)
    if not as_written:
        from futuresfund.risk_style import merge_risk, point_value
        from futuresfund.strategy_team import convert_atr_exit, prepare_backtest

        # An ATR exit is converted before the run, so its stop_dollars counts as written: merge_risk
        # keeps the converted default instead of filling in RISK_DEFAULTS.
        written, _note = convert_atr_exit(source, point_value(root), root=root)
        source = prepare_backtest(source, root)
        inputs = merge_risk(inputs, root, written)
    return _apply_inputs(source, inputs) if inputs else source


def script_trades(source: str, series: list[Bar], inputs: dict | None, root: str = "ES", timeframe: str = "2m", *, as_written: bool = False, candles_key: str | None = None) -> list[dict]:
    """The closed trades one script takes on these bars.

    PineTS runs the script. Research rewrites the session and arms the dollar
    stop before that run. A stop or target the script declares keeps its own
    default unless the attempt moves it. A TradingView comparison passes
    as_written so the script's own orders are what fill. candles_key names this
    exact window of bars, so the next attempt on it reuses its candles.
    """
    from futuresfund.pinets_engine import run_pinets

    return run_pinets(script_as_run(source, inputs, root, as_written=as_written), series, timeframe, root, candles_key=candles_key)


_chart_tokens = count(1)


class _Chart:
    """One chart's bars with their times read once, so a backtest does not read every bar again.

    A backtest builds Bar objects only for its own window. The chart is the same while the
    engine holds the same list of rows; a new list is a new chart.
    """

    KEEP = 2

    def __init__(self, rows: list[dict]) -> None:
        from futuresfund.chart_feed import _epoch_ms

        self.rows = rows
        self.count = len(rows)
        self.last = rows[-1] if rows else None
        pairs = []
        for position, row in enumerate(rows):
            stamp = _epoch_ms(row.get("t"))
            if stamp is None:
                continue
            pairs.append((int(stamp // 1000) if abs(stamp) > 10_000_000_000 else int(stamp), position))
        # The same order _bars_from gives: by time, and rows with the same time in file order.
        pairs.sort(key=lambda pair: pair[0])
        self.epochs = [pair[0] for pair in pairs]
        self.positions = [pair[1] for pair in pairs]
        self.token = f"{os.getpid()}-{next(_chart_tokens)}"
        self.starts: dict = {}
        self.windows: OrderedDict = OrderedDict()
        self.days: dict = {}
        self.jumps: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def same(self, rows: list[dict]) -> bool:
        return rows is self.rows and len(rows) == self.count and (not rows or rows[-1] is self.last)

    def first(self, sessions: int | None) -> int:
        """Index of the first bar of the window: the last year, or the last that many sessions."""
        with self.lock:
            if sessions not in self.starts:
                from futuresfund.objective import window_start

                start = window_start(self.epochs, sessions)
                self.starts[sessions] = 0 if start is None else bisect_left(self.epochs, start)
            return self.starts[sessions]

    def bars(self, start: int) -> list[Bar]:
        """The bars from this index to the last bar."""
        with self.lock:
            found = self.windows.get(start)
            if found is not None:
                self.windows.move_to_end(start)
                return found
        rows = self.rows
        found = []
        for epoch, position in zip(self.epochs[start:], self.positions[start:]):
            row = rows[position]
            found.append(Bar(
                epoch,
                float(row.get("o") or 0),
                float(row.get("h") or 0),
                float(row.get("l") or 0),
                float(row.get("c") or 0),
                float(row.get("v") or 0),
            ))
        with self.lock:
            self.windows[start] = found
            while len(self.windows) > self.KEEP:
                self.windows.popitem(last=False)
        return found

    def session_count(self, start: int, series: list[Bar]) -> int:
        from futuresfund.objective import session_days

        with self.lock:
            if start not in self.days:
                self.days[start] = session_days(bar.epoch for bar in series)
            return self.days[start]

    def minutes(self, start: int, run: list[Bar]) -> dict:
        """Each bar's New York minute to its index in this run, for the price jump check."""
        with self.lock:
            found = self.jumps.get(start)
            if found is not None:
                return found
        found = {bar.when.strftime("%Y-%m-%d %H:%M"): index for index, bar in enumerate(run)}
        with self.lock:
            self.jumps[start] = found
            while len(self.jumps) > 1:
                self.jumps.popitem(last=False)
        return found


_charts: list[_Chart] = []
_charts_lock = threading.Lock()


# A pool worker runs jobs for more than one chart, so it keeps the last two (engine_workers.CHARTS_PER_WORKER).
_CHARTS_KEPT = 2


def _chart_of(rows: list[dict]) -> _Chart:
    """This list of rows as a chart, read once. The two charts used last are kept."""
    with _charts_lock:
        for chart in _charts:
            if chart.same(rows):
                _charts.remove(chart)
                _charts.append(chart)
                return chart
    chart = _Chart(rows)
    with _charts_lock:
        _charts.append(chart)
        del _charts[:-_CHARTS_KEPT]
    return chart


_window_starts: OrderedDict = OrderedDict()
_window_lock = threading.Lock()


def _epoch_of(stamp: int) -> int:
    """The bar time in seconds, the way _Chart reads an integer t."""
    return stamp // 1000 if abs(stamp) > 10_000_000_000 else stamp


def window_start_index(rows: list[dict]) -> int:
    """Index of the first row a chart engine's backtest can use: the year's first bar, less WARMUP_BARS.

    execute_script with full=False starts at the last RESEARCH_SESSIONS sessions (the year) and a
    proving stage starts WARMUP_BARS before its own, shorter window, so nothing before this row is
    read. 0 keeps the whole series: a chart of a year or less, or one whose times are not whole
    numbers in time order (then _Chart's own sort could place an older row inside the window).
    """
    from futuresfund import objective

    total = len(rows or [])
    if total <= WARMUP_BARS:
        return 0
    sessions = int(objective.RESEARCH_SESSIONS)
    key = (total, rows[0].get("t"), rows[-1].get("t"), rows[total // 2].get("t"), sessions, WARMUP_BARS)
    with _window_lock:
        if key in _window_starts:
            _window_starts.move_to_end(key)
            return _window_starts[key]
    stamps = [row.get("t") for row in rows]
    start = 0
    if all(type(stamp) is int for stamp in stamps) and all(a <= b for a, b in zip(stamps, stamps[1:])):
        days = 0
        current = None
        for index in range(total - 1, -1, -1):
            day = objective._session_day(_epoch_of(stamps[index]))
            if day != current:
                current = day
                days += 1
                if days > sessions:
                    # index + 1 is the first bar of the year's oldest session, the bar window_start picks.
                    start = max(0, index + 1 - WARMUP_BARS)
                    break
    with _window_lock:
        _window_starts[key] = start
        while len(_window_starts) > 64:
            _window_starts.popitem(last=False)
    return start


def research_window(rows: list[dict], sessions: int | None = None) -> list[dict]:
    """The rows a chart engine's run reads (the last year and WARMUP_BARS before it), for sending to a worker.

    A run on these rows gives the same result as a run on the whole series
    (tests/test_engine_pool.py proves it). A stage longer than a year gets the whole series.
    """
    from futuresfund import objective

    if sessions is not None and int(sessions) > int(objective.RESEARCH_SESSIONS):
        return rows
    start = window_start_index(rows)
    return rows[start:] if start else rows


def execute_script(source: str, bars: list[dict], timeframe: str, inputs: dict | None, instrument: str, label: str, index: int, full: bool = False, sessions: int | None = None) -> dict:
    """Run one backtest. Chart engines keep the last year. The creation engine passes full=True for the five-year download.

    sessions keeps only the last that many futures sessions, for a proving stage. That run
    starts WARMUP_BARS earlier so the indicators are settled, and only trades that enter
    inside the window count.
    """
    from futuresfund.pinets_engine import PineTSError, PineTSScriptError, run_pinets

    title = _strategy_title(source)
    chart = _chart_of(bars)
    total = len(chart.epochs)
    first = 0 if full else chart.first(sessions)
    start_epoch = chart.epochs[first] if total else None
    start = ""
    if start_epoch is not None:
        start = datetime.fromtimestamp(start_epoch, timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M")
    begin = first
    if sessions is not None and not full and 0 < first < total:
        begin = max(0, first - WARMUP_BARS)
    run = chart.bars(begin)
    warmed = begin < first
    series = run[first - begin:] if warmed else run
    name = label or title or "script"
    logs: list[str] = []
    root_name = str(instrument or "ES").upper()
    try:
        as_run = script_as_run(source, inputs, root_name)
        trades = run_pinets(as_run, run, timeframe, root_name, candles_key=f"{chart.token}:{begin}")
        if warmed:
            trades = [trade for trade in trades if str(trade.get("entry_time") or "") >= start]
    except PineTSError as exc:
        logs.append(f"STOP   PineTS  {name}  the script stopped: {exc}")
        return {
            "engine": ENGINE,
            "runner": ENGINE,
            "error": f"The script stopped in PineTS: {exc}",
            "fatal": True,
            "script_error": isinstance(exc, PineTSScriptError),
            "net_profit": None,
            "max_drawdown": None,
            "trades": 0,
            "timeframe": timeframe,
            "instrument": instrument,
            "logs": logs,
        }
    from futuresfund.contracts import POINT_VALUE, TICK_SIZE, root_of
    from futuresfund.tv_broker import mark_price_jumps

    root = root_of(str(instrument or "ES"))
    point, tick = POINT_VALUE.get(root), TICK_SIZE.get(root)
    if point and tick and trades:
        mark_price_jumps(trades, run, point, tick, where=chart.minutes(begin, run))
    measured = _metrics(trades)
    from futuresfund.objective import stop_in_script

    # The stop the run was set to. A fill past a stop set inside the limit is not the script's fault.
    stop_root = root.upper() if root else "ES"
    measured["stop_dollars"] = stop_in_script(as_run if stop_root == root_name else script_as_run(source, inputs, stop_root))
    from futuresfund.script_diagnosis import exit_summary

    measured["exits"] = exit_summary(trades)
    from futuresfund.skip_window import clock

    # Profit by the half hour each trade entered, so a researcher can find the time of day that keeps losing.
    measured["clock"] = clock(trades)
    from futuresfund.objective import trade_spans

    days = chart.session_count(first, series)
    end = ""
    if series:
        end = datetime.fromtimestamp(series[-1].epoch, timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M")
    measured["days"] = days
    if end:
        measured["spans"] = trade_spans(trades, datetime.strptime(end, "%Y-%m-%d %H:%M"))
    else:
        measured["spans"] = {}
    from futuresfund.tv_broker import sharpe_ratio

    measured["sharpe"] = sharpe_ratio(
        [float(trade["pnl"]) for trade in trades if trade.get("exit_time") and trade.get("pnl") is not None],
        days,
    )
    measured["bars"] = len(series)
    measured["tested_through"] = end
    measured["trades_per_day"] = round(int(measured.get("trades") or 0) / days, 2) if days else None
    measured["full_dataset"] = bool(full)
    measured["window"] = "five years" if full else ("one year" if sessions is None else f"{sessions} sessions")
    span = "five-year download" if full else ("one-year window" if sessions is None else f"last {sessions} sessions")
    logs.append(
        f"DONE   PineTS  {name}  profit {measured.get('net_profit')}  drawdown {measured.get('max_drawdown')}  "
        f"profit factor {measured.get('profit_factor')}  profit ratio {measured.get('profit_ratio')}  "
        f"trades {measured.get('trades')}  win rate {measured.get('win_rate')}%  "
        f"{measured['trades_per_day'] if days else '?'} a day over {days} days  "
        f"{span}  {len(series):,} bars  {start} through {end}"
    )
    measured["timeframe"] = timeframe
    measured["instrument"] = instrument
    measured["engine_slot"] = index + 1
    measured["tested_from"] = start
    measured["logs"] = logs
    return measured


def tester_range(today=None, last_epoch: int | None = None) -> tuple[int, str]:
    """TradingView's "Last 30 days": the script starts at 00:00 UTC, 30 days before today, with no earlier bars.

    Bars that stop more than four days before today are older files, so the range ends at their last bar instead.
    Returns the epoch of that first bar and the same moment in New York time.
    """
    day = today or datetime.now(ET).date()
    if today is None and last_epoch is not None:
        last_day = datetime.fromtimestamp(last_epoch, timezone.utc).astimezone(ET).date()
        if (day - last_day).days > 4:
            day = last_day + timedelta(days=1)
    first = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(days=TESTER_DAYS)
    return int(first.timestamp()), first.astimezone(ET).strftime("%Y-%m-%d %H:%M")


def tester_start(today=None) -> str:
    return tester_range(today)[1]


def run_script(source: str, bars: list[dict], timeframe: str, inputs: dict | None, cancel, instrument: str = "ES", label: str = "", slot: int | None = None, full: bool = False, sessions: int | None = None) -> dict:
    """Backtest one script. A running desk sends it to that engine's process."""
    if cancel is not None and cancel.is_set():
        return _stopped(timeframe)
    title = _strategy_title(source)
    index = slot if slot is not None else slot_for(timeframe)
    name = label or title or "script"
    # The engine reads the bars. Here they are only counted, so the desk does not read every bar before each run.
    rows = bars or []
    if "volume" in (source or "").lower() and not any(float(row.get("v") or 0) > 0 for row in rows):
        _engine_log(index, f"BLOCK  PineTS  {name}  {timeframe} {instrument}  no volume column")
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
    start_line = f"START  PineTS  {name}  {timeframe} {instrument}  {len(rows):,} bars  {_settings_line(inputs)}"
    from futuresfund.engine_workers import active_desk

    desk = active_desk()
    if desk is not None:
        if cancel is not None and cancel.is_set():
            _engine_log(index, f"STOP   PineTS  {name}  cancelled")
            return _stopped(timeframe)
        return desk.run(index, source, bars, timeframe, inputs, instrument, name, cancel, start_line, full=full, sessions=sessions)
    _acquire_engine(name, timeframe, instrument, index)
    _engine_log(index, start_line)
    try:
        if cancel is not None and cancel.is_set():
            _engine_log(index, f"STOP   PineTS  {name}  cancelled")
            return _stopped(timeframe)
        measured = execute_script(source, bars, timeframe, inputs, instrument, name, index, full=full, sessions=sessions)
        for line in measured.pop("logs", []):
            _engine_log(index, line)
        record_engine_result(index, {**measured, "script": name, "source": source, "inputs": inputs, "instrument": instrument, "timeframe": timeframe})
        return measured
    finally:
        _release_engine(index)


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
    from futuresfund.tv_broker import tv_metrics

    measured = tv_metrics(trades)
    measured["engine"] = ENGINE
    measured["runner"] = ENGINE
    return measured


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
            "instrument": instrument or _ROOT_FOR_SLOT[index],
            "developer": _OWNER_FOR_SLOT[index],
            "notice": "",
        })


def _release_engine(index: int) -> None:
    _ENGINE_STATUS[index].update({
        "busy": False,
        "script": "",
        "timeframe": _CHART_FOR_SLOT[index],
        # A chart engine keeps its instrument between runs. The creation engine has none until the next draft.
        "instrument": _ROOT_FOR_SLOT[index],
        "developer": _OWNER_FOR_SLOT[index],
    })
    _ENGINE_LOCKS[index].release()
    with _ENGINE_GATE:
        _ENGINE_GATE.notify()

