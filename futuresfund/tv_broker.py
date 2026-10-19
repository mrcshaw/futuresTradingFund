"""TradingView's broker emulator for one futures contract.

With process_orders_on_close, a market order sent while the script runs on a
bar fills at that bar's close. Without it, the order fills at the next bar's
open. Either way the fill is one slippage tick against the order. An exit
order fills inside a bar along TradingView's path: open, the extreme nearer
the open, the other extreme, close. A stop or limit set while the trade is
open is moved to the next tick away from the fill, so the offset is never
shortened. A stop fills one slippage tick beyond its price, and a limit fills
at its price. A gap through either level fills at the open.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TICK = 0.25
ET = ZoneInfo("America/New_York")
_CLOSE_MINUTE = 16 * 60 + 45
_REOPEN_MINUTE = 18 * 60


def round_tick(price: float, tick: float = TICK) -> float:
    """The nearest tick, ties up, like math.round_to_mintick."""
    return math.floor(price / tick + 0.5 + 1e-9) * tick


def round_away(entry: float, price: float, tick: float = TICK) -> float:
    """The next tick further from the fill.

    TradingView does not shorten a stop or a target onto the nearer tick.
    An offset that is already on a tick stays there.
    """
    if tick <= 0:
        return price
    offset = price - entry
    if abs(offset) <= 1e-9:
        return round_tick(price, tick)
    steps = abs(offset) / tick
    whole = math.floor(steps + 1e-8)
    if steps - whole < 1e-8:
        ticks = whole
    else:
        ticks = whole + 1
    return entry + math.copysign(ticks * tick, offset)


def bar_path(bar) -> list[float]:
    """TradingView's path inside a bar: open, the extreme nearer the open, the other extreme, close."""
    if abs(bar.h - bar.o) <= abs(bar.o - bar.l):
        return [bar.o, bar.h, bar.l, bar.c]
    return [bar.o, bar.l, bar.h, bar.c]


def session_reopened(when: datetime) -> bool:
    """The first bars of the 18:00 ET reopen. The script gets this bar before a forced flat."""
    minute = when.hour * 60 + when.minute
    return minute >= _REOPEN_MINUTE and not past_session_close(when)


def past_session_close(when: datetime) -> bool:
    """From 16:45 ET until the 18:00 open, and from Friday 16:45 until Sunday 18:00."""
    minute = when.hour * 60 + when.minute
    day = when.weekday()
    if day == 5 or (day == 6 and minute < _REOPEN_MINUTE) or (day == 4 and minute >= _CLOSE_MINUTE):
        return True
    return _CLOSE_MINUTE <= minute < _REOPEN_MINUTE


def session_deadline(when: datetime) -> datetime:
    """The 16:45 ET by which a position that is open at this moment has to be flat."""
    minute = when.hour * 60 + when.minute
    day = when.weekday()
    if day == 6 and minute >= _REOPEN_MINUTE:
        shift = 1
    elif day <= 3 and minute >= _REOPEN_MINUTE:
        shift = 1
    elif day == 5:
        shift = -1
    elif day == 6:
        shift = -2
    else:
        shift = 0
    return (when + timedelta(days=shift)).replace(hour=16, minute=45, second=0, microsecond=0)


def touch(stop: float | None, limit: float | None, side: int, price: float, at_open: bool, slip: float) -> tuple[float, str] | None:
    """The exit fill when this point on the path reaches the stop or the limit, and which one it was."""
    if stop is not None and (price <= stop if side > 0 else price >= stop):
        return (price if at_open else stop) - slip * side, "stop"
    if limit is not None and (price >= limit if side > 0 else price <= limit):
        return (price if at_open else limit), "limit"
    return None


def sharpe_ratio(pnls: list[float], days: float | None = None) -> float | None:
    """Average trade divided by the variation between trades.

    When the test covers a known number of trading days, the figure is scaled to a year of 252 sessions.
    """
    if len(pnls) < 2:
        return None
    mean = sum(pnls) / len(pnls)
    variance = sum((pnl - mean) ** 2 for pnl in pnls) / (len(pnls) - 1)
    if variance <= 0:
        return None
    ratio = mean / math.sqrt(variance)
    if days and days > 0:
        ratio *= math.sqrt(len(pnls) / (float(days) / 252.0))
    return round(ratio, 3)


def tv_metrics(trades: list[dict]) -> dict:
    """The Strategy Tester figures for closed trades.

    The drawdown runs from the highest closed-trade equity to the lowest point
    an open trade reached, so a trade's adverse excursion counts. The profit
    ratio is the average winning trade divided by the average losing trade.
    """
    closed = [trade for trade in trades if trade.get("exit_time") and trade.get("pnl") is not None]
    realized = 0.0
    peak = 0.0
    drawdown = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    pnls: list[float] = []
    for trade in closed:
        pnl = float(trade["pnl"])
        pnls.append(pnl)
        adverse = trade.get("adverse")
        if adverse is not None:
            drawdown = max(drawdown, peak - (realized + min(float(adverse), 0.0)))
        realized += pnl
        peak = max(peak, realized)
        drawdown = max(drawdown, peak - realized)
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            gross_loss -= pnl
    winners = [pnl for pnl in pnls if pnl > 0]
    losers = [pnl for pnl in pnls if pnl < 0]
    profit_ratio = None
    if winners and losers:
        profit_ratio = round((sum(winners) / len(winners)) / abs(sum(losers) / len(losers)), 3)
    return {
        "net_profit": round(realized, 2),
        "max_drawdown": round(drawdown, 2),
        "trades": len(closed),
        "wins": wins,
        "win_rate": round(100.0 * wins / len(closed), 2) if closed else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "profit_ratio": profit_ratio,
        "sharpe": sharpe_ratio(pnls),
        "biggest_winner": round(max(pnls), 2) if pnls else None,
        "biggest_loser": round(min(pnls), 2) if pnls else None,
    }


class Broker:
    def __init__(
        self,
        bars,
        point: float,
        commission: float,
        slip_ticks: float = 1,
        tick: float = TICK,
        qty: int = 1,
        *,
        on_close: bool = True,
        commission_type: str = "cash_per_contract",
    ) -> None:
        self.bars = bars
        self.point = float(point)
        self.commission = float(commission)
        self.commission_type = commission_type
        self.tick = float(tick)
        self.slip = float(slip_ticks) * self.tick
        self.qty = int(qty)
        self.on_close = on_close
        self.position = 0
        self.trades: list[dict] = []
        self._open: dict | None = None
        self._exits: dict[str, dict] = {}
        self._queued: list[tuple] = []
        self._carry: dict | None = None
        self._carry_bar: int | None = None
        self._risk: dict | None = None
        self._risk_peak = 0.0

    # Orders the script sends while it runs on a bar.

    def entry(self, entry_id: str, side: int, qty: int | None = None) -> None:
        self._queued.append(("entry", entry_id, side, int(qty or self.qty)))

    def exit(
        self,
        exit_id: str,
        from_entry: str,
        stop: float | None = None,
        limit: float | None = None,
        profit: float | None = None,
        loss: float | None = None,
        label: str | None = None,
    ) -> None:
        """Place or replace the exit for that entry. It waits until the entry fills. The label is the trade list's signal.

        A new stop or limit replaces the old one, including a stop that moves farther away.
        na clears that price. A call with no stop and no limit removes the order, so a flat
        bar does not leave the previous trade's exit armed for the next entry.
        """
        key = from_entry or "*"
        entry = self.entry_price
        new_stop = self._round_exit(entry, stop)
        new_limit = self._round_exit(entry, limit)
        if new_stop is None and new_limit is None and profit is None and loss is None:
            self._exits.pop(key, None)
            return
        self._exits[key] = {
            "id": exit_id,
            "label": label or exit_id,
            "stop": new_stop,
            "limit": new_limit,
            "profit": profit,
            "loss": loss,
        }

    def close(self, entry_id: str, comment: str) -> None:
        """Close an entry that is already open. A close beside a new entry does not flatten that entry."""
        if self.position != 0 and self._open and self._open["entry_signal"] == str(entry_id):
            self._queued.append(("close_id", entry_id, comment))

    def close_all(self, comment: str) -> None:
        """Close the open position. It does nothing while the strategy is flat."""
        if self.position != 0:
            self._queued.append(("close", comment))

    def cancel(self, order_id: str) -> None:
        self._exits = {key: order for key, order in self._exits.items() if order["id"] != order_id and key != order_id}
        self._queued = [order for order in self._queued if not (order[0] == "entry" and order[1] == order_id)]

    def cancel_all(self) -> None:
        self._exits = {}
        self._queued = [order for order in self._queued if order[0] != "entry"]

    def arm_risk(self, settings: dict) -> None:
        """Cap every open trade with the dollar stop, the dollar target, and the optional trail."""
        self._risk = {
            "stop_dollars": float(settings.get("stop_dollars") or 0),
            "tp_dollars": float(settings.get("tp_dollars") or 0),
            "use_trail": bool(settings.get("use_trail")),
            "trail_pct": float(settings.get("trail_pct") or 0),
            "trail_step_amt": float(settings.get("trail_step_amt") or 0),
            "trail_step_pct": float(settings.get("trail_step_pct") or 0),
            "trail_min_dollars": float(settings.get("trail_min_dollars") or 0),
        }
        self._risk_peak = 0.0

    @property
    def entry_id(self) -> str:
        return self._open["entry_signal"] if self._open else ""

    @property
    def entry_price(self) -> float | None:
        """strategy.position_avg_price while a trade is open."""
        return self._open["entry_price"] if self._open else None

    @property
    def open_trade(self) -> dict | None:
        return self._open

    def pending(self) -> dict:
        """What the broker holds after the last bar: the open trade's exit prices and the orders waiting for the next open."""
        levels = None
        if self._open is not None:
            order = self._exits.get(self._open["entry_signal"]) or self._exits.get("*")
            if order:
                stop, limit = self._levels(order, 1 if self.position > 0 else -1)
                levels = {"stop": stop, "limit": limit, "label": order.get("label") or order["id"]}
        queued = []
        for order in self._queued:
            if order[0] == "entry":
                queued.append({"kind": "entry", "id": order[1], "side": order[2], "qty": order[3], "exit": self._exits.get(order[1]) or self._exits.get("*")})
            elif order[0] == "close":
                queued.append({"kind": "close", "id": None, "comment": order[1]})
            else:
                queued.append({"kind": "close", "id": order[1], "comment": order[2]})
        return {"exit": levels, "queued": queued}

    def exit_levels(self, order: dict, side: int, entry: float | None) -> tuple[float | None, float | None]:
        """The stop and limit prices of an exit order for a trade filled at entry."""
        stop = order.get("stop")
        limit = order.get("limit")
        if stop is None and order.get("loss") is not None and entry is not None:
            stop = round_tick(entry - float(order["loss"]) * self.tick * side, self.tick)
        if limit is None and order.get("profit") is not None and entry is not None:
            limit = round_tick(entry + float(order["profit"]) * self.tick * side, self.tick)
        if entry is not None:
            stop, limit = self.protect(entry, side, stop, limit)
        return stop, limit

    def protect(self, entry: float, side: int, stop: float | None, limit: float | None) -> tuple[float | None, float | None]:
        """The tighter of the script's exit and the dollar stop. A wider ATR stop does not replace it."""
        risk = self._risk
        if not risk or entry is None or side == 0:
            return stop, limit
        point = self.point or 1.0
        if point <= 0 or risk["stop_dollars"] <= 0:
            return stop, limit
        capped_stop = entry - (risk["stop_dollars"] / point) * side
        if stop is None or (stop - capped_stop) * side < 0:
            stop = capped_stop
        if risk["tp_dollars"] > 0:
            capped_limit = entry + (risk["tp_dollars"] / point) * side
            if limit is None or (limit - capped_limit) * side > 0:
                limit = capped_limit
        return stop, limit

    def fee(self, price: float, qty: int) -> float:
        return self._fee(price, qty)

    def _round_exit(self, entry: float | None, price: float | None) -> float | None:
        if price is None:
            return None
        if entry is None:
            return round_tick(price, self.tick)
        return round_away(entry, price, self.tick)

    # The bar cycle.

    def start_bar(self, i: int) -> None:
        """Before the script runs on bar i: orders from the last bar fill at the open, then exits fill inside the bar.

        The script closes the session when it calls strategy.close_all. The broker does not
        flatten at 16:45 on its own, because TradingView only fills the order the script sent.
        """
        if not self.on_close and self._queued:
            self._fill_market(i, self.bars[i].o)
        if self.position == 0 or self._open is None or i < self._open["checks_from"]:
            if self.position == 0:
                self._risk_peak = 0.0
            return
        self._apply_risk()
        self._walk_stop(i)

    def end_bar(self, i: int) -> None:
        """With process_orders_on_close, the market orders fill at the close of bar i."""
        if self.on_close and self._queued:
            self._fill_market(i, self.bars[i].c)
        # The script may have just replaced the stop with a wider ATR stop.
        # Put the dollar stop back and test this bar against it before the next bar.
        self._apply_risk()
        if self._risk and self.position != 0 and self._open is not None and i >= self._open["checks_from"]:
            self._walk_stop(i)
        carry = self._carry
        carry_bar = self._carry_bar
        self._carry = None
        self._carry_bar = None
        if carry is not None and carry_bar is not None and self._open is carry and self.position != 0:
            side = 1 if self.position > 0 else -1
            self._close(carry_bar, self.bars[carry_bar].c - self.slip * side, "Session")

    # Inside the emulator.

    def _fill_market(self, i: int, price: float) -> None:
        queued, self._queued = self._queued, []
        for order in queued:
            kind = order[0]
            if kind == "close":
                if self.position != 0:
                    side = 1 if self.position > 0 else -1
                    self._close(i, price - self.slip * side, order[1])
            elif kind == "close_id":
                if self.position != 0 and self._open and self._open["entry_signal"] == order[1]:
                    side = 1 if self.position > 0 else -1
                    self._close(i, price - self.slip * side, order[2])
            elif kind == "entry":
                entry_id, side, qty = order[1], order[2], order[3]
                if self._entry_is_shut(i):
                    continue
                if self.position * side > 0:
                    continue
                if self.position != 0:
                    self._close(i, price + self.slip * side, entry_id)
                self._open_trade(i, side, price + self.slip * side, entry_id, qty)

    def _flatten_session(self, i: int) -> None:
        """Close a trade that has reached 16:45 ET. A later bar does not get to exit it."""
        if self.position == 0 or self._open is None or i < 1:
            return
        current = self._when(self.bars[i])
        previous = self._when(self.bars[i - 1])
        if current is None or previous is None:
            return
        deadline = session_deadline(previous)
        if current < deadline:
            return
        side = 1 if self.position > 0 else -1
        on_the_close = past_session_close(current) and current - deadline <= timedelta(hours=2)
        if on_the_close:
            self._close(i, self.bars[i].o - self.slip * side, "Session")
        elif session_reopened(current):
            # The script gets the reopen bar, so its own close can fill there.
            # A script that leaves the trade open is flattened at the last bar
            # before 16:45, which is the price that was still trading.
            self._carry = self._open
            self._carry_bar = i - 1
        else:
            self._close(i - 1, self.bars[i - 1].c - self.slip * side, "Session")

    def _entry_is_shut(self, i: int) -> bool:
        """A fill at or after 16:45 ET is the session close, so it cannot open a trade."""
        when = self._when(self.bars[i])
        if when is None:
            return False
        if self.on_close:
            when = when + timedelta(seconds=self._width(i))
        return past_session_close(when)

    def _width(self, i: int) -> int:
        if i + 1 < len(self.bars):
            return max(60, int(self.bars[i + 1].epoch) - int(self.bars[i].epoch))
        if i > 0:
            return max(60, int(self.bars[i].epoch) - int(self.bars[i - 1].epoch))
        return 60

    def _when(self, bar):
        when = getattr(bar, "when", None)
        if when is not None:
            return when
        epoch = getattr(bar, "epoch", None)
        if epoch is None:
            return None
        return datetime.fromtimestamp(int(epoch), timezone.utc).astimezone(ET)

    def _path(self, bar) -> list[float]:
        return bar_path(bar)

    def _apply_risk(self) -> None:
        """Tighten the open trade to the dollar stop and target. A tighter exit already on the trade stays."""
        risk = self._risk
        if not risk or self.position == 0 or self._open is None:
            return
        entry = self._open.get("entry_price")
        point = self.point or 1.0
        if entry is None or point <= 0 or risk["stop_dollars"] <= 0:
            return
        side = 1 if self.position > 0 else -1
        stop = entry - (risk["stop_dollars"] / point) * side
        limit = entry + (risk["tp_dollars"] / point) * side if risk["tp_dollars"] > 0 else None
        key = self._open["entry_signal"]
        order = self._exits.get(key) or self._exits.get("*")
        if order:
            existing_stop, existing_limit = self.exit_levels(order, side, entry)
            if existing_stop is not None and (existing_stop - stop) * side > 0:
                stop = existing_stop
            if existing_limit is not None and (limit is None or (limit - existing_limit) * side > 0):
                limit = existing_limit
        if risk["use_trail"] and self._risk_peak * point >= risk["trail_min_dollars"]:
            step_amt = risk["trail_step_amt"]
            steps = math.floor(self._risk_peak * point / step_amt) if step_amt else 0
            keep = min(risk["trail_pct"] + steps * risk["trail_step_pct"], 95.0) / 100.0
            candidate = entry + self._risk_peak * keep * side
            if (candidate - stop) * side > 0:
                stop = candidate
        label = (order or {}).get("label") or "Risk"
        exit_id = (order or {}).get("id") or "Risk"
        self.exit(exit_id, key, stop=stop, limit=limit, label=label)

    def _remember_peak(self, bar, side: int) -> None:
        if not self._risk or self._open is None:
            return
        entry = self._open.get("entry_price")
        if entry is None:
            return
        favorable = (bar.h - entry) if side > 0 else (entry - bar.l)
        if favorable > self._risk_peak:
            self._risk_peak = favorable

    def _levels(self, order: dict, side: int) -> tuple[float | None, float | None]:
        return self.exit_levels(order, side, self._open["entry_price"] if self._open else None)

    def _walk_stop(self, i: int) -> None:
        """See whether this bar already traded through the stop. An ATR stop is capped first."""
        order = self._exits.get(self._open["entry_signal"]) or self._exits.get("*")
        bar = self.bars[i]
        side = 1 if self.position > 0 else -1
        for price in self._path(bar):
            fill = self._trigger(order, side, price, bar) if order else None
            if fill is not None:
                level, signal = fill
                self._close(i, level, signal)
                return
            self._excursion(price)
        self._remember_peak(bar, side)

    def _loss_floor(self, side: int) -> float | None:
        """The price at which a long or short has lost the armed dollar stop. A gap cannot go past it."""
        risk = self._risk
        trade = self._open
        if not risk or trade is None or self.point <= 0 or risk["stop_dollars"] <= 0:
            return None
        entry = trade.get("entry_price")
        if entry is None:
            return None
        entry = float(entry)
        qty = abs(int(trade.get("size") or self.qty or 1))
        room = risk["stop_dollars"] - (self._fee(entry, qty) * 2)
        if room <= 0:
            room = risk["stop_dollars"]
        return entry - (room / self.point) * side

    def _trigger(self, order: dict, side: int, price: float, bar) -> tuple[float, str] | None:
        """The fill if this point on the path reaches the stop or the limit."""
        stop, limit = self._levels(order, side)
        fill = touch(stop, limit, side, price, price == bar.o, self.slip)
        if fill is None:
            return None
        level, kind = fill
        if kind == "stop":
            floor = self._loss_floor(side)
            if floor is not None and (level - floor) * side < 0:
                level = floor
        return level, order.get("label") or order["id"]

    def _fee(self, price: float, qty: int) -> float:
        if self.commission_type == "percent":
            return abs(price) * self.point * qty * self.commission / 100.0
        if self.commission_type == "cash_per_order":
            return self.commission
        return self.commission * qty

    def _open_trade(self, i: int, side: int, price: float, entry_id: str, qty: int) -> None:
        bar = self.bars[i]
        self.position = side * qty
        self._open = {
            "index": i,
            "checks_from": i + 1 if self.on_close else i,
            "side": "long" if side > 0 else "short",
            "size": side * qty,
            "entry_time": bar.when.strftime("%Y-%m-%d %H:%M"),
            "entry_ms": bar.epoch * 1000,
            "entry_price": round(price, 2),
            "entry_signal": entry_id,
            "entry_fee": self._fee(price, qty),
            "best": price,
            "worst": price,
        }

    def _close(self, i: int, price: float, signal: str) -> None:
        trade = self._open
        if trade is None:
            self.position = 0
            return
        self._excursion(price)
        side = 1 if trade["size"] > 0 else -1
        qty = abs(trade["size"])
        points = (price - trade["entry_price"]) * side
        fees = trade["entry_fee"] + self._fee(price, qty)
        trade.update({
            "exit_time": self.bars[i].when.strftime("%Y-%m-%d %H:%M"),
            "exit_ms": self.bars[i].epoch * 1000,
            "exit_price": round(price, 2),
            "exit_signal": signal,
            "exit_index": i,
            "commission": round(fees, 2),
            "pnl": round(points * self.point * qty - fees, 2),
            "favorable": round((trade["best"] - trade["entry_price"]) * side * self.point * qty - trade["entry_fee"], 2),
            "adverse": round((trade["worst"] - trade["entry_price"]) * side * self.point * qty - trade["entry_fee"], 2),
        })
        self.trades.append(trade)
        self._exits.pop(trade["entry_signal"], None)
        self._exits.pop("*", None)
        self._open = None
        self.position = 0

    def _excursion(self, price: float) -> None:
        trade = self._open
        if trade is None:
            return
        side = 1 if trade["size"] > 0 else -1
        if (price - trade["best"]) * side > 0:
            trade["best"] = price
        if (price - trade["worst"]) * side < 0:
            trade["worst"] = price
