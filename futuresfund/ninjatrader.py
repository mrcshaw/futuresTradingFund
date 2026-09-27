"""NinjaTrader 8 through the CrossTrade add-on.

The alert thread never calls the network. A background poll keeps a short cache of
positions and executions, and a confirmation request may make one read of its own.
The same cache accepts a streamed order, execution, or position frame.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from futuresfund.config import crosstrade_key

_API = "https://app.crosstrade.io/v1/api"
_TIMEOUT = 2.0
_FRESH_SECONDS = 15.0
_lock = threading.Lock()
_thread: threading.Thread | None = None
_cache: dict = {"positions": {}, "executions": {}, "updated": 0.0, "connected": False, "reason": ""}
_announced: set[str] = set()


def api_token() -> str:
    import os

    return os.environ.get("CROSSTRADE_API_TOKEN", "").strip() or crosstrade_key()


def api_root() -> str:
    import os

    return os.environ.get("CROSSTRADE_API_URL", "").strip() or _API


def configured() -> bool:
    return bool(api_token())


def reset_cache() -> None:
    """Drop the in-memory NinjaTrader book. Tests use this so one case cannot leak into the next."""
    with _lock:
        _cache["positions"].clear()
        _cache["executions"].clear()
        _cache["updated"] = 0.0
        _cache["connected"] = False
        _cache["reason"] = ""


def cache_snapshot() -> dict:
    with _lock:
        return {
            "positions": dict(_cache["positions"]),
            "executions": {key: list(rows) for key, rows in _cache["executions"].items()},
            "updated": _cache["updated"],
            "connected": _cache["connected"],
            "reason": _cache["reason"],
        }


def cached_position(account: str, instrument: str) -> dict | None:
    """The last NinjaTrader position, when the poll is still fresh."""
    root = _root(instrument)
    if root is None:
        return None
    with _lock:
        if time.time() - float(_cache["updated"] or 0) > _FRESH_SECONDS:
            return None
        return dict(_cache["positions"].get((account, root)) or {}) or None


def cached_executions(account: str) -> list[dict]:
    with _lock:
        return [dict(row) for row in _cache["executions"].get(account, [])]


def apply_frame(frame: dict) -> None:
    """Store one streamed NinjaTrader frame. Unknown frames are ignored."""
    kind = str(frame.get("type") or "")
    now = time.time()
    with _lock:
        if kind == "positionUpdate":
            row = frame.get("position") if isinstance(frame.get("position"), dict) else frame
            _store_position(row)
        elif kind == "executionUpdate":
            row = frame.get("execution") if isinstance(frame.get("execution"), dict) else frame
            _store_execution(row)
        elif kind == "orderUpdate":
            _cache["connected"] = True
        else:
            return
        _cache["updated"] = now
        _cache["connected"] = True
        _cache["reason"] = ""


def get_position(account: str, instrument: str) -> dict:
    query = urllib.parse.urlencode({"instrument": instrument})
    payload = _request("GET", f"/accounts/{_quote(account)}/position?{query}")
    if payload.get("ok") and isinstance(payload.get("body"), dict):
        _remember_position(payload["body"])
    return payload


def list_executions(account: str, lookback: int = 300) -> dict:
    query = urllib.parse.urlencode({"lookbackTime": max(1, min(int(lookback), 900))})
    payload = _request("GET", f"/accounts/{_quote(account)}/executions?{query}")
    if payload.get("ok") and isinstance(payload.get("body"), dict):
        rows = payload["body"].get("executions")
        if not isinstance(rows, list):
            rows = payload["body"].get("data") if isinstance(payload["body"].get("data"), list) else []
        with _lock:
            _cache["executions"][account] = [row for row in rows if isinstance(row, dict)]
            _cache["updated"] = time.time()
            _cache["connected"] = True
            _cache["reason"] = ""
        payload["executions"] = [row for row in rows if isinstance(row, dict)]
    return payload


def order_status(account: str, order_id: str) -> dict:
    payload = _request("GET", f"/accounts/{_quote(account)}/orders/{_quote(order_id)}/status")
    if payload.get("ok") and isinstance(payload.get("body"), dict):
        payload["status"] = str(payload["body"].get("status") or "")
    return payload


def flatten_position(account: str, instrument: str) -> dict:
    """Close one NinjaTrader position and cancel working orders left behind."""
    return _request(
        "POST",
        f"/accounts/{_quote(account)}/positions/flatten",
        {"instrument": instrument, "cancelOrders": True},
    )


def start_listener(board=None) -> None:
    """Poll NinjaTrader on a side thread. Safe to call once at startup."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, args=(board,), daemon=True, name="nt8-listener")
    _thread.start()


def _loop(board) -> None:
    while True:
        try:
            _poll(board)
        except Exception:
            _mark_down("NinjaTrader did not answer.")
        time.sleep(3)


def _poll(board) -> None:
    if not api_token():
        _say(board, "missing", (
            "NinjaTrader confirmation is waiting on CROSSTRADE_API_TOKEN. "
            "TradingView alerts still update the chart, and orders still go out through the CrossTrade webhook."
        ))
        return
    pairs = _watch_pairs()
    if not pairs:
        return
    seen = False
    for account, instrument in pairs:
        position = get_position(account, instrument)
        executions = list_executions(account)
        seen = seen or bool(position.get("ok") or executions.get("ok"))
    if seen:
        _say(board, "connected", (
            "NinjaTrader is connected. The trading analyst can confirm fills, "
            "and the floor trader can read the live position."
        ))
    else:
        _mark_down("NinjaTrader is not connected.")


def _watch_pairs() -> list[tuple[str, str]]:
    from futuresfund.book import load

    found: list[tuple[str, str]] = []
    book = load()
    strategy = book.get("strategy") if isinstance(book.get("strategy"), dict) else {}
    if strategy.get("account") and strategy.get("contract"):
        found.append((str(strategy["account"]), str(strategy["contract"])))
    for position in (book.get("positions") or {}).values():
        if position.get("account") and position.get("instrument"):
            found.append((str(position["account"]), str(position["instrument"])))
    unique = []
    seen = set()
    for pair in found:
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(pair)
    return unique


def _request(method: str, path: str, body: dict | None = None) -> dict:
    token = api_token()
    if not token:
        return {"ok": False, "reason": "CROSSTRADE_API_TOKEN is not set."}
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{api_root().rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.read(200_000).decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read(20_000).decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "reason": _public_error(raw)}
    except (OSError, urllib.error.URLError, TimeoutError):
        _mark_down("NinjaTrader did not answer.")
        return {"ok": False, "reason": "NinjaTrader did not answer."}
    parsed = _json(raw)
    if status < 200 or status >= 300:
        return {"ok": False, "status": status, "reason": _public_error(raw), "body": parsed}
    if isinstance(parsed, dict) and parsed.get("success") is False:
        return {"ok": False, "status": status, "reason": _public_error(str(parsed.get("error") or raw)), "body": parsed}
    return {"ok": True, "status": status, "body": parsed}


def _remember_position(body: dict) -> None:
    with _lock:
        _store_position(body)
        _cache["updated"] = time.time()
        _cache["connected"] = True
        _cache["reason"] = ""


def _store_position(row: dict) -> None:
    if not isinstance(row, dict):
        return
    account = str(row.get("account") or "")
    instrument = str(row.get("instrument") or "")
    root = _root(instrument)
    if account and root:
        _cache["positions"][(account, root)] = row


def _store_execution(row: dict) -> None:
    if not isinstance(row, dict):
        return
    account = str(row.get("account") or "")
    if not account:
        return
    rows = _cache["executions"].setdefault(account, [])
    rows.append(row)
    _cache["executions"][account] = rows[-40:]


def _mark_down(reason: str) -> None:
    with _lock:
        _cache["connected"] = False
        _cache["reason"] = reason


def _say(board, key: str, text: str) -> None:
    if key in _announced or board is None:
        _announced.add(key)
        return
    _announced.add(key)
    board.post("Trading Analyst", text, kind="report", channel="Trading Analyst")


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _json(raw: str):
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _public_error(raw: str) -> str:
    text = " ".join(str(raw or "").split())
    lowered = text.lower()
    if "bearer" in lowered or "key=" in lowered or "authorization" in lowered:
        return "NinjaTrader refused the request."
    return text[:180] or "NinjaTrader refused the request."


def _root(instrument: str) -> str | None:
    from futuresfund.contracts import root_of

    try:
        return root_of(instrument)
    except ValueError:
        return None


def signed_contracts(row: dict | None) -> int | None:
    """Long is positive, short is negative, flat is zero."""
    if not row:
        return None
    if row.get("netPos") is not None:
        return int(float(row["netPos"]))
    quantity = row.get("quantity")
    if quantity is None:
        return None
    magnitude = abs(int(float(quantity)))
    market = str(row.get("marketPosition") or "").lower()
    if market == "short":
        return -magnitude
    if market == "long":
        return magnitude
    if market == "flat":
        return 0
    return None


def unrealized(row: dict | None) -> float | None:
    if not row or row.get("unrealizedProfitLoss") is None:
        return None
    return float(row["unrealizedProfitLoss"])
