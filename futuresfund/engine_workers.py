"""The research engines run their backtests on a small pool of worker processes.

Every chart engine (three per instrument) and the creation engine is a logical engine: its own
thread here, its own status card, queue, Skip and Restart. The bar loop itself runs in one of at
most ENGINES_AT_ONCE worker processes, so 22 engines do not mean 22 CPU-bound processes on an
8-core desk. A worker is started only when a backtest needs one, keeps the bars of up to
CHARTS_PER_WORKER charts, and a job goes to a free worker that already holds its chart first.

A request such as opening a library script never waits on a backtest: it does not use the pool.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import signal
import threading
import time
from collections import OrderedDict

from futuresfund.roster import ENGINE_COUNT

# Logical engines: every chart engine plus the creation engine (22 today).
_SLOTS = ENGINE_COUNT


def _default_at_once() -> int:
    """One worker for every core but one, and never fewer than two."""
    return max(2, (os.cpu_count() or 2) - 1)


# The most backtests that run at the same time, one per worker process. Read before every backtest,
# so a saved setting applies at the next backtest.
ENGINES_AT_ONCE = _default_at_once()
# Charts (bar series) one worker keeps between jobs, newest used last.
CHARTS_PER_WORKER = 2

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


def at_once() -> int:
    """The pool size now: the engines_at_once setting, at least 1."""
    try:
        return max(1, int(ENGINES_AT_ONCE))
    except (TypeError, ValueError):
        return _default_at_once()


def waiting_notice(limit: int | None = None) -> str:
    return f"Waiting for a free worker ({limit if limit is not None else at_once()} running at once)"


def _end_with_node(signum=None, frame=None) -> None:
    """A skipped or restarted engine's worker is terminated. Its Node process goes with it instead of finishing the run."""
    try:
        from futuresfund.pinets_engine import close_server

        close_server()
    finally:
        os._exit(0)


def _engine_process(number: int, inbound: mp.Queue, outbound: mp.Queue, stop: mp.synchronize.Event) -> None:
    """One worker. Its bar loop runs any engine's job. It ends when the desk that started it is gone.

    It keeps the bars of up to CHARTS_PER_WORKER charts by their key, the least recently used
    dropped first. The desk keeps the same order, so it sends bars only for a chart this worker
    does not hold. A job whose chart was dropped anyway answers need_bars, and the desk sends them.
    """
    try:
        signal.signal(signal.SIGTERM, _end_with_node)
    except (ValueError, OSError, AttributeError):
        pass
    cached: OrderedDict = OrderedDict()
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
        job_id = job.get("id")
        try:
            if job.get("op") == "hold":
                time.sleep(float(job.get("seconds") or 0))
                outbound.put({"id": job_id, "ok": True, "result": {"held": True, "pid": os.getpid()}, "logs": []})
                continue
            if job.get("op") == "burn":
                end = time.perf_counter() + float(job.get("seconds") or 0)
                token = 0
                while time.perf_counter() < end:
                    token = (token * 3 + 1) % 1_000_003
                outbound.put({"id": job_id, "ok": True, "result": {"burned": token, "pid": os.getpid()}, "logs": []})
                continue
            packed = job.get("bars")
            key = job.get("bars_key")
            if packed is not None:
                cached[key] = _unpack(packed)
            if key not in cached:
                outbound.put({"id": job_id, "ok": False, "need_bars": True, "error": "This worker does not have the chart for that job."})
                continue
            cached.move_to_end(key)
            while len(cached) > CHARTS_PER_WORKER:
                cached.popitem(last=False)
            from futuresfund.backtrader_engine import execute_script

            result = execute_script(
                job.get("source") or "",
                cached[key],
                job.get("timeframe") or "",
                job.get("inputs"),
                job.get("instrument") or "ES",
                job.get("label") or "",
                int(job.get("slot") or 0),
                full=bool(job.get("full")),
                sessions=job.get("sessions"),
            )
            logs = result.pop("logs", [])
            outbound.put({"id": job_id, "ok": True, "result": result, "logs": logs})
        except Exception as exc:
            outbound.put({"id": job_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})


class _Worker:
    """One worker process as the desk sees it: its queues, the charts it holds, and the engine using it."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.proc = None
        self.inbound = None
        self.outbound = None
        # The chart keys the process holds, in the process's own least-recently-used order.
        self.charts: OrderedDict = OrderedDict()
        # The engine index whose job is on this worker now. None when it is free.
        self.slot: int | None = None
        self.used = 0.0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def holds(self, key) -> bool:
        return key is not None and key in self.charts

    def keep(self, key) -> None:
        """The process just used or stored this chart. Mirror its order."""
        if key is None:
            return
        self.charts[key] = True
        self.charts.move_to_end(key)
        while len(self.charts) > CHARTS_PER_WORKER:
            self.charts.popitem(last=False)


class EngineDesk:
    """One thread per logical engine. Each thread borrows a pool worker for every backtest."""

    def __init__(self) -> None:
        self.online = False
        self._ctx = None
        self._closed = threading.Event()
        self._stop = None
        self._local = [queue.Queue() for _ in range(_SLOTS)]
        self._workers: list[_Worker] = []
        self._pool = threading.Condition()
        self._numbers = 0
        self._seq = 0
        self._seq_lock = threading.Lock()
        # The most backtests seen running at once, for the tests and the Systems Administrator.
        self.peak = 0

    def start(self) -> None:
        if self.online:
            return
        self._ctx = mp.get_context("spawn")
        self._closed = threading.Event()
        self._stop = self._ctx.Event()
        self._local = [queue.Queue() for _ in range(_SLOTS)]
        with self._pool:
            self._workers = []
            self.peak = 0
        # Workers start when a backtest needs one, so a desk with nothing to run holds no process.
        for slot in range(_SLOTS):
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
        with self._pool:
            workers = list(self._workers)
            self._pool.notify_all()
        for worker in workers:
            self._end(worker)
        for jobs in self._local:
            jobs.put(None)

    def pids(self) -> list[int]:
        """The worker processes running now."""
        with self._pool:
            return [worker.proc.pid for worker in self._workers if worker.alive()]

    def running(self) -> int:
        """Backtests on a worker now."""
        with self._pool:
            return sum(1 for worker in self._workers if worker.slot is not None)

    def workers(self) -> list[dict]:
        """Each worker: its number, process id, the engine on it, and how many charts it holds."""
        with self._pool:
            return [
                {
                    "worker": worker.number,
                    "pid": worker.proc.pid if worker.alive() else None,
                    "engine": worker.slot + 1 if worker.slot is not None else None,
                    "charts": len(worker.charts),
                }
                for worker in self._workers
            ]

    def run(self, slot: int, source: str, bars: list[dict], timeframe: str, inputs: dict | None, instrument: str, label: str, cancel, start_line: str, full: bool = False, sessions: int | None = None) -> dict:
        """Queue one backtest on this engine and wait for its result."""
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
            full=bool(full),
            sessions=sessions,
        )

    def hold(self, slot: int, seconds: float) -> dict:
        """Occupy one engine and a worker without using the CPU. Tests use this."""
        return self._dispatch(slot, op="hold", seconds=seconds, label="hold", timeframe="", instrument="", start_line="START  PineTS  hold", source="", bars=None, inputs=None, cancel=None)

    def burn(self, slot: int, seconds: float) -> dict:
        """Occupy one engine and a worker with CPU work. Tests use this to show the desk stays responsive."""
        return self._dispatch(slot, op="burn", seconds=seconds, label="burn", timeframe="", instrument="", start_line="START  PineTS  burn", source="", bars=None, inputs=None, cancel=None)

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

    # ---------- the pool ----------

    def _spawn(self, worker: _Worker) -> None:
        """Start (or start again) this worker's process. It holds no chart yet."""
        self._end(worker)
        inbound = self._ctx.Queue()
        outbound = self._ctx.Queue()
        proc = self._ctx.Process(
            target=_engine_process,
            args=(worker.number, inbound, outbound, self._stop),
            daemon=True,
            name=f"engine-worker-{worker.number}",
        )
        proc.start()
        worker.inbound = inbound
        worker.outbound = outbound
        worker.proc = proc
        worker.charts.clear()

    @staticmethod
    def _end(worker: _Worker) -> None:
        proc = worker.proc
        worker.charts.clear()
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)

    def _pick(self, key, limit: int) -> _Worker | None:
        """A free worker for this chart, or a new one while the pool is under its size. Called under the pool lock."""
        busy = sum(1 for worker in self._workers if worker.slot is not None)
        if busy >= limit:
            return None
        free = [worker for worker in self._workers if worker.slot is None]
        holding = [worker for worker in free if worker.holds(key)]
        if holding:
            return holding[0]
        roomy = [worker for worker in free if len(worker.charts) < CHARTS_PER_WORKER and worker.alive()]
        if roomy:
            return min(roomy, key=lambda worker: len(worker.charts))
        if len(self._workers) < limit:
            self._numbers += 1
            worker = _Worker(self._numbers)
            self._workers.append(worker)
            return worker
        if free:
            # Every free worker is full: the one used longest ago drops its oldest chart.
            return min(free, key=lambda worker: worker.used)
        return None

    def _take_worker(self, slot: int, key, cancel) -> _Worker | None:
        """Wait for a worker. None when Skip, a cancel or the desk stopping ends the wait."""
        from futuresfund.backtrader_engine import set_engine_notice, skipped

        waited = False
        try:
            with self._pool:
                while True:
                    if self._closed.is_set() or skipped(slot) or (cancel is not None and cancel.is_set()):
                        return None
                    limit = at_once()
                    worker = self._pick(key, limit)
                    if worker is not None:
                        worker.slot = slot
                        worker.used = time.monotonic()
                        running = sum(1 for item in self._workers if item.slot is not None)
                        self.peak = max(self.peak, running)
                        return worker
                    if not waited:
                        waited = True
                        set_engine_notice(slot, waiting_notice(limit))
                    self._pool.wait(timeout=0.4)
        finally:
            if waited:
                set_engine_notice(slot, "")

    def _give_back(self, worker: _Worker) -> None:
        """The job is done. A worker beyond a smaller pool size is ended."""
        with self._pool:
            worker.slot = None
            worker.used = time.monotonic()
            extra = len(self._workers) - at_once()
            if extra > 0 and worker in self._workers:
                self._workers.remove(worker)
                ending = worker
            else:
                ending = None
            self._pool.notify_all()
        if ending is not None:
            self._end(ending)

    def _worker_of(self, slot: int) -> _Worker | None:
        with self._pool:
            return next((worker for worker in self._workers if worker.slot == slot), None)

    def restart(self, slot: int) -> None:
        """Cut the backtest engine N is running: its worker process is ended and started again on the next job.

        A job still waiting for a worker is dropped by the skip flag instead.
        """
        if not self.online or slot < 0 or slot >= _SLOTS:
            return
        with self._pool:
            # The worker, its process and its job are read together under the pool lock, and the process is
            # signalled under it too, so a job that finished and freed the worker for another engine meanwhile
            # is never the one ended.
            worker = next((item for item in self._workers if item.slot == slot), None)
            if worker is None:
                return
            proc = worker.proc
            worker.charts.clear()
            if proc is None or not proc.is_alive():
                return
            proc.terminate()
        proc.join(timeout=2)

    # ---------- one engine ----------

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
            worker = None
            try:
                cancel = job.get("cancel")
                from futuresfund.backtrader_engine import skipped, skipped_measurement

                if skipped(slot):
                    _engine_log(slot, f"SKIP   PineTS  {job.get('label') or 'script'}  skipped")
                    box["result"] = skipped_measurement(job.get("timeframe") or "")
                    continue
                if cancel is not None and cancel.is_set():
                    _engine_log(slot, f"STOP   PineTS  {job.get('label') or 'script'}  cancelled")
                    box["result"] = _stopped(job.get("timeframe") or "")
                    continue
                payload = self._payload(slot, job)
                worker = self._take_worker(slot, payload.get("bars_key"), cancel)
                if worker is None:
                    if skipped(slot):
                        _engine_log(slot, f"SKIP   PineTS  {job.get('label') or 'script'}  skipped while waiting for a worker")
                        box["result"] = skipped_measurement(job.get("timeframe") or "")
                    elif self._closed.is_set():
                        box["error"] = RuntimeError("The engine stopped.")
                    else:
                        _engine_log(slot, f"STOP   PineTS  {job.get('label') or 'script'}  cancelled")
                        box["result"] = _stopped(job.get("timeframe") or "")
                    continue
                if not worker.alive():
                    self._spawn(worker)
                _acquire_engine(job.get("label") or "script", job.get("timeframe") or "", job.get("instrument") or "", slot)
                held = True
                _engine_log(slot, job.get("start_line") or "START  PineTS")
                message = self._exchange(worker, slot, job, payload)
                if not message.get("ok"):
                    _engine_log(slot, f"STOP   PineTS  {job.get('label') or 'script'}  {message.get('error')}")
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
                else:
                    for line in message.get("logs") or []:
                        _engine_log(slot, line)
                    box["result"] = message.get("result") or {}
                    from futuresfund.backtrader_engine import record_engine_result

                    record_engine_result(slot, {**box["result"], "script": job.get("label") or "", "source": job.get("source") or "", "inputs": job.get("inputs"), "instrument": job.get("instrument") or "ES", "timeframe": job.get("timeframe") or ""})
            except Exception as exc:
                from futuresfund.backtrader_engine import skipped, skipped_measurement

                if skipped(slot):
                    box["result"] = skipped_measurement(job.get("timeframe") or "")
                else:
                    box["error"] = exc
                if worker is not None and not self._closed.is_set():
                    # A worker that failed mid-job is started again on its next job.
                    self._end(worker)
            finally:
                if worker is not None:
                    self._give_back(worker)
                if held:
                    _release_engine(slot)
                done.set()

    def _payload(self, slot: int, job: dict) -> dict:
        """What the worker needs, without the bars. A chart engine's run sends only the window it uses."""
        payload = {
            "id": job["id"],
            "op": job.get("op") or "run",
            "slot": slot,
            "source": job.get("source") or "",
            "timeframe": job.get("timeframe") or "",
            "inputs": job.get("inputs"),
            "instrument": job.get("instrument") or "ES",
            "label": job.get("label") or "",
            "seconds": job.get("seconds") or 0,
            "full": bool(job.get("full")),
            "sessions": job.get("sessions"),
        }
        if payload["op"] == "run":
            bars = job.get("bars") or []
            if not payload["full"]:
                from futuresfund.backtrader_engine import research_window

                bars = research_window(bars, payload["sessions"])
            job["_send"] = bars
            payload["bars_key"] = _bars_key(bars, payload["timeframe"], payload["instrument"])
        return payload

    def _exchange(self, worker: _Worker, slot: int, job: dict, payload: dict) -> dict:
        key = payload.get("bars_key")
        message = self._ask(worker, slot, job, dict(payload, bars=_pack(job.get("_send"))) if key is not None and not worker.holds(key) else payload)
        if message.get("need_bars") and key is not None:
            # The worker dropped that chart: send it once more.
            message = self._ask(worker, slot, job, dict(payload, bars=_pack(job.get("_send"))))
        if key is not None:
            if message.get("ok"):
                worker.keep(key)
            else:
                worker.charts.pop(key, None)
        return message

    def _ask(self, worker: _Worker, slot: int, job: dict, payload: dict) -> dict:
        worker.inbound.put(payload)
        outbound = worker.outbound
        proc = worker.proc
        while not self._closed.is_set():
            from futuresfund.backtrader_engine import skipped, skipped_measurement

            if skipped(slot):
                # Skip cuts this backtest: the worker is ended and starts again on its next job.
                self._end(worker)
                return {
                    "ok": True,
                    "result": skipped_measurement(job.get("timeframe") or ""),
                    "logs": [f"SKIP   PineTS  {job.get('label') or 'script'}  skipped"],
                }
            if proc is None or not proc.is_alive():
                worker.charts.clear()
                if skipped(slot):
                    return {
                        "ok": True,
                        "result": skipped_measurement(job.get("timeframe") or ""),
                        "logs": [f"SKIP   PineTS  {job.get('label') or 'script'}  skipped"],
                    }
                raise RuntimeError(f"Engine {slot + 1} stopped.")
            try:
                message = outbound.get(timeout=0.4)
            except queue.Empty:
                continue
            if message.get("id") != job["id"]:
                continue
            return message
        worker.charts.clear()
        raise RuntimeError(f"Engine {slot + 1} stopped.")


def start_engines() -> EngineDesk:
    """Start the engine threads once. A second call leaves them running. Workers start when a backtest needs one."""
    global _desk
    with _desk_lock:
        if _desk is None:
            _desk = EngineDesk()
        _desk.start()
        return _desk


def stop_engines() -> None:
    """Stop the engines and their workers. The next backtest in this process runs locally."""
    with _desk_lock:
        if _desk is not None:
            _desk.stop()


def active_desk() -> EngineDesk | None:
    desk = _desk
    if desk is not None and desk.online:
        return desk
    return None
