"""Each Backtrader engine has its own thread and its own process.

The thread in this desk waits on that process. The bar loop runs there, so a
request such as opening a library script does not wait on a backtest.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time

_SLOTS = 4
_desk: "EngineDesk | None" = None
_desk_lock = threading.Lock()


def _pack(bars: list[dict] | None) -> list[tuple]:
    packed = []
    for row in bars or []:
        packed.append((
            row.get("t"),
            float(row.get("o") or 0),
            float(row.get("h") or 0),
            float(row.get("l") or 0),
            float(row.get("c") or 0),
            float(row.get("v") or 0),
        ))
    return packed


def _unpack(packed: list[tuple]) -> list[dict]:
    return [
        {"t": row[0], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5]}
        for row in packed
    ]


def _bars_key(bars: list[dict] | None, timeframe: str, instrument: str) -> tuple:
    rows = bars or []
    if not rows:
        return (timeframe, instrument, 0)
    step = max(len(rows) // 8, 1)
    sample = tuple(round(float(rows[i].get("c") or 0), 5) for i in range(0, len(rows), step))
    return (timeframe, instrument, len(rows), rows[0].get("t"), rows[-1].get("t"), sample)


def _engine_process(slot: int, inbound: mp.Queue, outbound: mp.Queue, stop: mp.synchronize.Event) -> None:
    """One engine. This process is the only place its bar loop runs."""
    cached_key = None
    cached_bars = None
    while not stop.is_set():
        try:
            job = inbound.get(timeout=0.5)
        except queue.Empty:
            continue
        if not isinstance(job, dict):
            continue
        job_id = job.get("id")
        try:
            if job.get("op") == "hold":
                time.sleep(float(job.get("seconds") or 0))
                outbound.put({"id": job_id, "ok": True, "result": {"held": True}, "logs": []})
                continue
            if job.get("op") == "burn":
                end = time.perf_counter() + float(job.get("seconds") or 0)
                token = 0
                while time.perf_counter() < end:
                    token = (token * 3 + 1) % 1_000_003
                outbound.put({"id": job_id, "ok": True, "result": {"burned": token}, "logs": []})
                continue
            packed = job.get("bars")
            key = job.get("bars_key")
            if packed is not None:
                cached_bars = _unpack(packed)
                cached_key = key
            if cached_bars is None or key != cached_key:
                raise RuntimeError("This engine does not have the chart for that job.")
            from futuresfund.backtrader_engine import execute_script

            result = execute_script(
                job.get("source") or "",
                cached_bars,
                job.get("timeframe") or "",
                job.get("inputs"),
                job.get("instrument") or "ES",
                job.get("label") or "",
                slot,
            )
            logs = result.pop("logs", [])
            outbound.put({"id": job_id, "ok": True, "result": result, "logs": logs})
        except Exception as exc:
            outbound.put({"id": job_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})


class EngineDesk:
    """Four engine threads. Each thread waits on its own engine process."""

    def __init__(self) -> None:
        self.online = False
        self._ctx = None
        self._closed = threading.Event()
        self._stop = None
        self._local = [queue.Queue() for _ in range(_SLOTS)]
        self._sent: list = [None] * _SLOTS
        self._proc: list = [None] * _SLOTS
        self._in: list = [None] * _SLOTS
        self._out: list = [None] * _SLOTS
        self._seq = 0
        self._seq_lock = threading.Lock()

    def start(self) -> None:
        if self.online:
            return
        self._ctx = mp.get_context("spawn")
        self._closed = threading.Event()
        self._stop = self._ctx.Event()
        self._local = [queue.Queue() for _ in range(_SLOTS)]
        self._sent = [None] * _SLOTS
        self._proc = [None] * _SLOTS
        self._in = [None] * _SLOTS
        self._out = [None] * _SLOTS
        for slot in range(_SLOTS):
            self._spawn(slot)
            threading.Thread(
                target=self._loop,
                args=(slot,),
                daemon=True,
                name=f"engine-{slot + 1}",
            ).start()
        self.online = True

    def stop(self) -> None:
        self.online = False
        self._closed.set()
        if self._stop is not None:
            self._stop.set()
        for proc in self._proc:
            if proc is not None and proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)
        for jobs in self._local:
            jobs.put(None)

    def run(self, slot: int, source: str, bars: list[dict], timeframe: str, inputs: dict | None, instrument: str, label: str, cancel, start_line: str) -> dict:
        """Queue one backtest on this engine and wait for that process only."""
        return self._dispatch(
            slot,
            op="run",
            source=source,
            bars=bars,
            timeframe=timeframe,
            inputs=inputs,
            instrument=instrument,
            label=label,
            cancel=cancel,
            start_line=start_line,
        )

    def hold(self, slot: int, seconds: float) -> None:
        """Occupy one engine without using the CPU. Tests use this."""
        self._dispatch(slot, op="hold", seconds=seconds, label="hold", timeframe="", instrument="", start_line="START  Backtrader  hold", source="", bars=None, inputs=None, cancel=None)

    def burn(self, slot: int, seconds: float) -> None:
        """Occupy one engine with CPU work. Tests use this to show the desk stays responsive."""
        self._dispatch(slot, op="burn", seconds=seconds, label="burn", timeframe="", instrument="", start_line="START  Backtrader  burn", source="", bars=None, inputs=None, cancel=None)

    def _dispatch(self, slot: int, **job) -> dict:
        done = threading.Event()
        box: dict = {}
        job["done"] = done
        job["box"] = box
        with self._seq_lock:
            self._seq += 1
            job["id"] = self._seq
        self._local[slot].put(job)
        done.wait()
        if box.get("error"):
            raise box["error"]
        return box.get("result") or {}

    def _spawn(self, slot: int) -> None:
        old = self._proc[slot]
        if old is not None and old.is_alive():
            old.terminate()
            old.join(timeout=2)
        inbound = self._ctx.Queue()
        outbound = self._ctx.Queue()
        proc = self._ctx.Process(
            target=_engine_process,
            args=(slot, inbound, outbound, self._stop),
            daemon=True,
            name=f"engine-{slot + 1}",
        )
        proc.start()
        self._in[slot] = inbound
        self._out[slot] = outbound
        self._proc[slot] = proc
        self._sent[slot] = None

    def _loop(self, slot: int) -> None:
        from futuresfund.backtrader_engine import _acquire_engine, _engine_log, _release_engine, _stopped

        while not self._closed.is_set():
            job = self._local[slot].get()
            if job is None:
                return
            done = job["done"]
            box = job["box"]
            if self._closed.is_set():
                box["error"] = RuntimeError("The engine stopped.")
                done.set()
                return
            held = False
            try:
                cancel = job.get("cancel")
                if cancel is not None and cancel.is_set():
                    _engine_log(slot, f"STOP   Backtrader  {job.get('label') or 'script'}  cancelled")
                    box["result"] = _stopped(job.get("timeframe") or "")
                    continue
                if self._proc[slot] is None or not self._proc[slot].is_alive():
                    self._spawn(slot)
                _acquire_engine(job.get("label") or "script", job.get("timeframe") or "", job.get("instrument") or "", slot)
                held = True
                _engine_log(slot, job.get("start_line") or "START  Backtrader")
                message = self._exchange(slot, job)
                if not message.get("ok"):
                    _engine_log(slot, f"STOP   Backtrader  {job.get('label') or 'script'}  {message.get('error')}")
                    box["result"] = {
                        "engine": "backtrader",
                        "runner": "backtrader",
                        "error": message.get("error") or "The engine process stopped.",
                        "fatal": False,
                        "net_profit": None,
                        "max_drawdown": None,
                        "trades": 0,
                        "timeframe": job.get("timeframe") or "",
                        "instrument": job.get("instrument") or "",
                    }
                    if self._proc[slot] is None or not self._proc[slot].is_alive():
                        self._spawn(slot)
                else:
                    for line in message.get("logs") or []:
                        _engine_log(slot, line)
                    box["result"] = message.get("result") or {}
            except Exception as exc:
                box["error"] = exc
                if not self._closed.is_set():
                    try:
                        self._spawn(slot)
                    except Exception:
                        pass
            finally:
                if held:
                    _release_engine(slot)
                done.set()

    def _exchange(self, slot: int, job: dict) -> dict:
        payload = {
            "id": job["id"],
            "op": job.get("op") or "run",
            "source": job.get("source") or "",
            "timeframe": job.get("timeframe") or "",
            "inputs": job.get("inputs"),
            "instrument": job.get("instrument") or "ES",
            "label": job.get("label") or "",
            "seconds": job.get("seconds") or 0,
        }
        key = None
        if payload["op"] != "hold":
            key = _bars_key(job.get("bars"), payload["timeframe"], payload["instrument"])
            payload["bars_key"] = key
            if key != self._sent[slot]:
                payload["bars"] = _pack(job.get("bars"))
        self._in[slot].put(payload)
        outbound = self._out[slot]
        proc = self._proc[slot]
        while not self._closed.is_set():
            if proc is None or not proc.is_alive():
                self._sent[slot] = None
                raise RuntimeError(f"Engine {slot + 1} stopped.")
            try:
                message = outbound.get(timeout=0.4)
            except queue.Empty:
                continue
            if message.get("id") != job["id"]:
                continue
            if message.get("ok") and key is not None:
                self._sent[slot] = key
            elif not message.get("ok"):
                self._sent[slot] = None
            return message
        self._sent[slot] = None
        raise RuntimeError(f"Engine {slot + 1} stopped.")


def start_engines() -> EngineDesk:
    """Start the four engine processes once. A second call leaves them running."""
    global _desk
    with _desk_lock:
        if _desk is None:
            _desk = EngineDesk()
        _desk.start()
        return _desk


def stop_engines() -> None:
    """Stop the engine processes. The next backtest in this process runs locally."""
    with _desk_lock:
        if _desk is not None:
            _desk.stop()


def active_desk() -> EngineDesk | None:
    desk = _desk
    if desk is not None and desk.online:
        return desk
    return None
