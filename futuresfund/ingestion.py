"""Ingestion accepts a TradingView alert and adds it to the chart. It does not place an order."""

from __future__ import annotations

from datetime import datetime

from futuresfund.board import Board
from futuresfund.book import load, record_order
from futuresfund.csv_bars import _stamp
from futuresfund.research import absorb_alert, append_bar, load_bars, save_bars
from futuresfund.strategy import get_strategy, series_match, store_strategy


def ingest_alert(board: Board, signal: dict) -> dict:
    """Store the alert on the chart series and say so. The floor trader is not called."""
    strategy = get_strategy()
    if isinstance(strategy, dict):
        strategy = dict(strategy)
        strategy["running"] = load().get("running") or []
    matched, detail = series_match(strategy, signal)
    board.log("Ingestion", f"Checking {signal.get('instrument')} {signal.get('timeframe') or ''} at {signal.get('price')}")
    if not matched:
        if signal.get("id") == "desk-bar" and signal.get("price") is not None:
            from futuresfund.book import mark_paper

            stats = mark_paper(load(), signal)
            note = (
                f"Paper price {signal['instrument']} {signal['price']}. "
                f"Active P&L ${stats['active_pnl']:,.2f}. Drawdown ${stats['drawdown']:,.2f}."
            )
            board.log("Ingestion", note)
            board.post("Ingestion", note, kind="report", channel="Ingestion")
            return {
                "ok": True,
                "sent": False,
                "strategy": strategy or {},
                "bars": [],
                "closed": [],
                "bar": _bar(signal),
                "frame": str((strategy or {}).get("timeframe") or ""),
                "alert_frame": str(signal.get("timeframe") or ""),
                "reason": note,
            }
        record_order(load(), signal, "skip", {"sent": False, "reason": detail})
        board.log("Ingestion", detail)
        board.post("Ingestion", detail, kind="report", channel="Ingestion")
        return {"ok": False, "sent": False, "reason": detail}
    if signal.get("price") is None:
        reason = "The interval alert needs a price so its bar can be added."
        record_order(load(), signal, "skip", {"sent": False, "reason": reason})
        board.log("Ingestion", reason)
        board.post("Ingestion", reason, kind="report", channel="Ingestion")
        return {"ok": False, "sent": False, "reason": reason}

    strategy = dict(strategy)
    frame = str(strategy.get("timeframe") or "5m")
    alert_frame = str(signal.get("timeframe") or frame)
    bar = _bar(signal)
    closed, forming = absorb_alert(strategy.get("forming"), bar, alert_frame, frame)
    bars = load_bars()
    for candle in closed:
        bars = append_bar(bars, candle)
    if closed:
        save_bars(bars)
    strategy["forming"] = forming or None
    store_strategy(strategy)
    if closed:
        note = f"Added the closed {frame} candle {closed[-1]['t']} at {closed[-1]['c']} to the chart. Volume {closed[-1].get('v')}."
    else:
        note = (
            f"Added the {alert_frame} price {signal.get('price')} to the open {frame} candle. "
            f"Volume {bar.get('v')}. The chart is updated. No order is placed from this desk."
        )
    from futuresfund.book import mark_paper

    mark_paper(load(), signal)
    board.log("Ingestion", note)
    board.post("Ingestion", note, kind="report", channel="Ingestion")
    return {
        "ok": True,
        "strategy": strategy,
        "bars": bars,
        "closed": closed,
        "bar": bar,
        "frame": frame,
        "alert_frame": alert_frame,
        "reason": note,
    }


def _bar(signal: dict) -> dict:
    price = float(signal["price"])
    stamp = _stamp(signal.get("bar_time"), None) or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "t": stamp,
        "o": signal.get("open") if signal.get("open") is not None else price,
        "h": signal.get("high") if signal.get("high") is not None else price,
        "l": signal.get("low") if signal.get("low") is not None else price,
        "c": price,
        "v": float(signal["volume"]) if signal.get("volume") is not None else 0,
        "poc": signal.get("poc"),
        "poc_volume": signal.get("poc_volume"),
        "delta": signal.get("delta"),
        "delta_pct": signal.get("delta_pct"),
    }
