"""Turn a TradingView alert into the fields the desks and CrossTrade need."""

from __future__ import annotations

import json
import re

from futuresfund.config import max_qty
from futuresfund.contracts import check_instrument, crosstrade_name, yahoo_symbol
from futuresfund.strategy import normalize_timeframe

_PAIR = re.compile(r"([A-Za-z_]+)\s*=\s*(.*?)(?:;|$)")
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,64}$")
_ACTIONS = {"BUY": "BUY", "SELL": "SELL", "LONG": "BUY", "SHORT": "SELL"}
_ORDERS = {"MARKET", "LIMIT", "STOPMARKET", "STOPLIMIT"}
_TIF = {"DAY", "GTC", "IOC", "FOK"}
_SIDES = {"long", "short", "flat"}
# key and command are read by CrossTrade, not by this desk. A trailing alert message is ignored.
_FIELDS = {
    "account", "instrument", "ticker", "action", "qty", "quantity", "contracts",
    "order_type", "tif", "price", "close", "position", "position_size", "market_position",
    "prev_market_position", "sync_strategy", "out_of_sync", "limit_price", "stop_price",
    "take_profit", "stop_loss", "flatten_first", "atm_strategy", "destination",
    "timeframe", "interval", "time", "bar_time", "open", "high", "low",
    "volume", "vol", "id", "poc", "poc_volume", "delta", "delta_pct",
    "strategy", "strategy_name", "macd", "macd_signal", "macd_histogram",
    "histogram", "signal",
}

# A candle-close order with this id is chart data. Ingestion records it and does not send it.
BAR_FEED_ID = "desk-bar"


def parse_alert(raw: str) -> dict:
    """Accept JSON or CrossTrade-style key=value text. Ignore any key= line."""
    text = (raw or "").strip().lstrip("\ufeff")
    if not text:
        raise ValueError("The alert was empty.")
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    fields = _json_fields(text) if text.startswith("{") else _text_fields(text)
    return normalize(fields)


def _json_fields(text: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("The alert JSON could not be read.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The alert JSON must be an object.")
    return {str(key): payload[key] for key in payload}


def _text_fields(text: str) -> dict:
    fields = {}
    for match in _PAIR.finditer(text.replace("\n", " ")):
        name = match.group(1).strip().lower()
        if name in _FIELDS and name not in fields:
            fields[name] = match.group(2).strip()
    if not fields:
        raise ValueError("The alert had no account, instrument, or qty fields.")
    return fields


def normalize(fields: dict) -> dict:
    lowered = {str(key).strip().lower(): value for key, value in fields.items()}
    account = str(lowered.get("account") or "").strip()
    if not _ACCOUNT.fullmatch(account):
        raise ValueError("Account must be the prop account name, with no semicolon.")
    instrument = crosstrade_name(check_instrument(str(lowered.get("instrument") or lowered.get("ticker") or "")))
    qty = _qty(lowered.get("qty") if lowered.get("qty") not in (None, "") else lowered.get("quantity") or lowered.get("contracts"))
    price = _optional_float(lowered.get("price") or lowered.get("close"))
    position = _position(
        lowered.get("position") or lowered.get("market_position"),
        lowered.get("position_size"),
        qty,
    )
    order_type = str(lowered.get("order_type") or "MARKET").strip().upper()
    if order_type not in _ORDERS:
        raise ValueError("order_type must be MARKET, LIMIT, STOPMARKET, or STOPLIMIT.")
    tif = str(lowered.get("tif") or "DAY").strip().upper()
    if tif not in _TIF:
        raise ValueError("tif must be DAY, GTC, IOC, or FOK.")
    limit_price = _optional_float(lowered.get("limit_price"))
    stop_price = _optional_float(lowered.get("stop_price"))
    if order_type == "LIMIT" and limit_price is None:
        limit_price = price
    if order_type in {"STOPMARKET", "STOPLIMIT"} and stop_price is None:
        stop_price = price
    destination = str(lowered.get("destination") or "").strip().lower()
    if destination and destination not in {"tradovate", "ninjatrader"}:
        raise ValueError("destination must be tradovate or ninjatrader.")
    hinted = str(lowered.get("action") or "").strip().upper()
    return {
        "account": account,
        "instrument": instrument,
        "yahoo": yahoo_symbol(instrument),
        "qty": qty,
        "price": price,
        "position": position,
        "order_type": order_type,
        "tif": tif,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "take_profit": _optional_text(lowered.get("take_profit")),
        "stop_loss": _optional_text(lowered.get("stop_loss")),
        "flatten_first": _optional_bool(lowered.get("flatten_first")),
        "atm_strategy": _optional_text(lowered.get("atm_strategy")),
        "destination": destination or None,
        "hinted_action": _ACTIONS.get(hinted),
        "sync_strategy": _optional_bool(lowered.get("sync_strategy")) is True,
        "prev_market_position": _side_name(lowered.get("prev_market_position")),
        "out_of_sync": _out_of_sync(lowered.get("out_of_sync")),
        "timeframe": _timeframe(lowered.get("timeframe") or lowered.get("interval")),
        "bar_time": _optional_text(lowered.get("time") or lowered.get("bar_time")),
        "open": _optional_float(lowered.get("open")),
        "high": _optional_float(lowered.get("high")),
        "low": _optional_float(lowered.get("low")),
        "volume": _optional_float(lowered.get("volume") if lowered.get("volume") not in (None, "") else lowered.get("vol")),
        "poc": _optional_float(lowered.get("poc")),
        "poc_volume": _optional_float(lowered.get("poc_volume")),
        "delta": _optional_float(lowered.get("delta")),
        "delta_pct": _optional_float(lowered.get("delta_pct")),
        "macd": _optional_float(lowered.get("macd")),
        "macd_signal": _optional_float(lowered.get("macd_signal") if lowered.get("macd_signal") not in (None, "") else lowered.get("signal")),
        "macd_histogram": _optional_float(lowered.get("macd_histogram") if lowered.get("macd_histogram") not in (None, "") else lowered.get("histogram")),
        "id": _feed_id(lowered.get("id")),
        "strategy": _strategy_name(lowered.get("strategy") if lowered.get("strategy") not in (None, "") else lowered.get("strategy_name")),
    }


def _strategy_name(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > 120 or any(mark in text for mark in (";", "\n", "\r")):
        raise ValueError("strategy must be the strategy name.")
    return text


def _feed_id(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,40}", text):
        raise ValueError("id must be letters, numbers, or a hyphen.")
    return text


def _qty(value) -> int:
    if value is None or str(value).strip() == "":
        return 0
    number = float(value)
    if number < 0 or number != int(number):
        raise ValueError("qty must be a whole number of contracts.")
    qty = int(number)
    if qty > max_qty():
        raise ValueError(f"qty {qty} is above FUTURES_MAX_QTY ({max_qty()}).")
    return qty


def _optional_float(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a number.") from exc


def _optional_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ";" in text or "\n" in text:
        raise ValueError("A field contained a semicolon.")
    return text


def _timeframe(value):
    if value is None or str(value).strip() == "":
        return None
    return normalize_timeframe(str(value))


def _side_name(value) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text not in _SIDES:
        raise ValueError("Position must be long, short, or flat.")
    return text


def _out_of_sync(value) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text != "flatten":
        raise ValueError("out_of_sync must be flatten.")
    return text


def _optional_bool(value):
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _position(position, size, qty: int):
    if position is None or str(position).strip() == "":
        return None
    text = str(position).strip().lower()
    if text in {"flat", "none", "0"}:
        return 0.0
    if text == "long":
        magnitude = _optional_float(size) or float(qty)
        return abs(magnitude)
    if text == "short":
        magnitude = _optional_float(size) or float(qty)
        return -abs(magnitude)
    return float(text)
