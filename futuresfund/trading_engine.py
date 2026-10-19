"""Each trader has a trading engine in its own process.

The floor trader's engine and the quantitative trader's engine read the chart
and run the active strategy's Pine script there, apart from the desk and from
the four research engines. The trader's thread in the desk sends the card's
script, waits on that trader's engine only, and then books what the script
did, on paper or live. An engine starts the first time its trader checks a
script, and it stops when the desk that started it is gone.

When the engines are not started, as in the tests, the script runs in the
trader's thread.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import time
from datetime import datetime

ENGINES = ("Floor Trader", "Quantitative Trader")
_RUN_SECONDS = 180.0
# The downloaded chart is read again after this many seconds. Live candles are added on every run.
_CHART_SECONDS = 300.0


class ScriptStopped(Exception):
    """The Pine script raised on the newest bars."""


def _engine_process(trader: str, inbound, outbound, stop) -> None:
    """One trader's engine. The chart is read and the script runs here."""
    import futuresfund.live_trading as live
    from futuresfund.pine_lib import PineError
    from futuresfund.pine_runtime import PineUnsupported

    live._CHART_SECONDS = _CHART_SECONDS
    desk = mp.parent_process()
    while not stop.is_set():
        try:
            job = inbound.get(timeout=0.5)
        except queue.Empty:
            if desk is not None and not desk.is_alive():
                return
            continue
        if not isinstance(job, dict):
            continue
        try:
            if job.get("op") == "ping":
                outbound.put({"id": job["id"], "ok": True, "result": {"pid": os.getpid(), "trader": trader}})
                continue
            result = live.script_run(
                job["source"],
                job["root"],
                job["timeframe"],
                int(job["since"]),
                feed=str(job.get("feed") or "tradingview"),
                instrument=str(job.get("instrument") or ""),
            )
            outbound.put({"id": job["id"], "ok": True, "result": result})
        except (PineError, PineUnsupported) as exc:
            outbound.put({"id": job["id"], "ok": False, "stopped": True, "error": str(exc)})
        except Exception as exc:
            outbound.put({"id": job["id"], "ok": False, "error": f"{type(exc).__name__}: {exc}"})


class _Engine:
    """One trader's process and the two queues to it."""

    def __init__(self, ctx, trader: str, stop, target) -> None:
        self.trader = trader
        self.lock = threading.Lock()
        self.runs = 0
        self.last = ""
        self.seconds = None
        self._ctx = ctx
        self._stop = stop
        self._target = target
        self._seq = 0
        self.proc = None
        self._spawn()

    def _spawn(self) -> None:
        self.close()
        self.inbound = self._ctx.Queue()
        self.outbound = self._ctx.Queue()
        self.proc = self._ctx.Process(
            target=self._target,
            args=(self.trader, self.inbound, self.outbound, self._stop),
            daemon=True,
            name=f"trading-engine-{self.trader.lower().replace(' ', '-')}",
        )
        self.proc.start()

    def close(self) -> None:
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=2)

    def ask(self, job: dict, seconds: float) -> dict:
        with self.lock:
            if self.proc is None or not self.proc.is_alive():
                self._spawn()
            self._seq += 1
            job = {**job, "id": self._seq}
            began = time.monotonic()
            self.inbound.put(job)
            while True:
                left = seconds - (time.monotonic() - began)
                if left <= 0:
                    self._spawn()
                    raise RuntimeError(f"The {self.trader}'s trading engine did not answer in {int(seconds)} seconds, so it was restarted.")
                if not self.proc.is_alive():
                    self._spawn()
                    raise RuntimeError(f"The {self.trader}'s trading engine stopped, so it was restarted.")
                try:
                    message = self.outbound.get(timeout=min(0.5, left))
                except queue.Empty:
                    continue
                if message.get("id") != job["id"]:
                    continue
                self.seconds = round(time.monotonic() - began, 2)
                return message


class TradingEngines:
    """The traders' engines. Each trader's engine is started the first time it is asked to run a script."""

    def __init__(self, target=None) -> None:
        """target is the engine process's function. A test hands in one that reads its own bars."""
        self.online = False
        self._ctx = None
        self._stop = None
        self._target = target or _engine_process
        self._engines: dict[str, _Engine] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.online:
                return
            self._ctx = mp.get_context("spawn")
            self._stop = self._ctx.Event()
            self._engines = {}
            self.online = True

    def stop(self) -> None:
        with self._lock:
            self.online = False
            if self._stop is not None:
                self._stop.set()
            engines, self._engines = list(self._engines.values()), {}
        for engine in engines:
            engine.close()

    def engine(self, trader: str) -> _Engine:
        if trader not in ENGINES:
            raise ValueError(f"{trader} has no trading engine.")
        with self._lock:
            if not self.online:
                raise RuntimeError("The trading engines are stopped.")
            engine = self._engines.get(trader)
            if engine is None:
                engine = _Engine(self._ctx, trader, self._stop, self._target)
                self._engines[trader] = engine
            return engine

    def run(self, trader: str, source: str, root: str, frame: str, since: int, *, feed: str = "tradingview", instrument: str = "") -> dict:
        engine = self.engine(trader)
        message = engine.ask({
            "op": "run",
            "source": source,
            "root": root,
            "timeframe": frame,
            "since": int(since),
            "feed": feed,
            "instrument": instrument,
        }, _RUN_SECONDS)
        if message.get("ok"):
            engine.runs += 1
            engine.last = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return message.get("result") or {}
        if message.get("stopped"):
            raise ScriptStopped(message.get("error") or "The script stopped.")
        raise RuntimeError(f"The {trader}'s trading engine could not run the script: {message.get('error')}")

    def ping(self, trader: str, seconds: float = 60.0) -> dict:
        message = self.engine(trader).ask({"op": "ping"}, seconds)
        return message.get("result") or {}

    def view(self) -> list[dict]:
        with self._lock:
            engines = dict(self._engines)
        rows = []
        for trader in ENGINES:
            engine = engines.get(trader)
            alive = bool(engine and engine.proc is not None and engine.proc.is_alive())
            rows.append({
                "trader": trader,
                "running": alive,
                "pid": engine.proc.pid if alive else None,
                "runs": engine.runs if engine else 0,
                "last_run": engine.last if engine else "",
                "seconds": engine.seconds if engine else None,
            })
        return rows


_engines: TradingEngines | None = None
_engines_lock = threading.Lock()


def start_trading_engines() -> TradingEngines:
    """Turn on the traders' engines once. A second call leaves them as they are."""
    global _engines
    with _engines_lock:
        if _engines is None:
            _engines = TradingEngines()
        _engines.start()
        return _engines


def stop_trading_engines() -> None:
    with _engines_lock:
        if _engines is not None:
            _engines.stop()


def active_engines() -> TradingEngines | None:
    engines = _engines
    if engines is not None and engines.online:
        return engines
    return None


def engines_view() -> list[dict]:
    engines = active_engines()
    if engines is None:
        return [{"trader": trader, "running": False, "pid": None, "runs": 0, "last_run": "", "seconds": None} for trader in ENGINES]
    return engines.view()


def run_for_trader(trader: str, source: str, root: str, frame: str, since: int, *, feed: str = "tradingview", instrument: str = "") -> dict:
    """The script's run for one trader, in that trader's engine process when the engines are on."""
    engines = active_engines()
    if engines is not None and trader in ENGINES:
        return engines.run(trader, source, root, frame, since, feed=feed, instrument=instrument)
    import futuresfund.live_trading as live
    from futuresfund.pine_lib import PineError
    from futuresfund.pine_runtime import PineUnsupported

    try:
        return live.script_run(source, root, frame, since, feed=feed, instrument=instrument)
    except (PineError, PineUnsupported) as exc:
        raise ScriptStopped(str(exc)) from exc
