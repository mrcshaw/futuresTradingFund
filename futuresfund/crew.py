"""One thread per agent. The systems administrator also serves desk requests.

A chart study runs on that chart's own thread and waits only on that chart's
engine process. It does not queue behind another chart. Opening the library
is a desk request, so it runs on the systems administrator and does not wait
on a backtest. A job never waits on the thread that is already running it.
"""

from __future__ import annotations

import queue
import threading

from futuresfund.roster import ROSTER

SYSTEMS = "Systems Administrator"
_crew = None
_crew_lock = threading.Lock()


class Crew:
    """Each agent has a thread. Desk requests have the systems administrator's other thread."""

    def __init__(self, board) -> None:
        self.board = board
        self._queues: dict[str, queue.Queue] = {}
        self._owners: dict[int, threading.Thread] = {}
        for agent in ROSTER:
            name = agent["name"]
            jobs: queue.Queue = queue.Queue()
            self._queues[name] = jobs
            thread = threading.Thread(
                target=self._loop,
                args=(name, jobs),
                daemon=True,
                name=f"agent-{agent['id']}",
            )
            self._owners[id(jobs)] = thread
            thread.start()
        self._app: queue.Queue = queue.Queue()
        app_thread = threading.Thread(
            target=self._loop,
            args=(SYSTEMS, self._app),
            daemon=True,
            name="systems-administrator",
        )
        self._owners[id(self._app)] = app_thread
        app_thread.start()

    def run(self, agent: str, message: str, work, *, during_stop: bool = False):
        """Run work on that agent's thread and wait for it."""
        return self._submit(self._queues.get(agent), agent, message, work, during_stop=during_stop, announce=True)

    def submit(self, agent: str, message: str, work, *, during_stop: bool = False) -> None:
        """Queue work on that agent's thread. The caller does not wait."""
        jobs = self._queues.get(agent)
        if jobs is None:
            self.board.log(agent, message)
            try:
                work()
            except Exception as exc:
                self.board.log(agent, f"Stopped: {exc}")
            return
        if self.board.cancel.is_set() and not during_stop:
            return

        def job() -> None:
            from futuresfund.llm import use_agent

            with use_agent(agent):
                if message:
                    self.board.log(agent, message)
                try:
                    work()
                except Exception as exc:
                    self.board.log(agent, f"Stopped: {exc}")

        jobs.put(job)

    def application(self, message: str, work, *, announce: bool = False):
        """A request made of the desk. This thread never runs a backtest."""
        return self._submit(self._app, SYSTEMS, message, work, during_stop=True, announce=announce, rest=True)

    def _invoke(self, agent: str, message: str, work, announce: bool):
        from futuresfund.llm import use_agent

        with use_agent(agent):
            if announce and message:
                self.board.log(agent, message)
            return work()

    def _submit(self, jobs: queue.Queue | None, agent: str, message: str, work, *, during_stop: bool, announce: bool, rest: bool = False):
        if jobs is None:
            if announce and message:
                self.board.log(agent, message)
            return work()
        if self.board.cancel.is_set() and not during_stop:
            return None
        # The thread that owns this queue is already inside a job. Running the
        # next one inline is the only way it can finish; waiting would park it
        # on itself.
        if threading.current_thread() is self._owners.get(id(jobs)):
            try:
                return self._invoke(agent, message, work, announce)
            except Exception as exc:
                if not getattr(exc, "status_code", None):
                    self.board.log(agent, f"Stopped: {exc}")
                raise
        done = threading.Event()
        box: dict = {}

        def job() -> None:
            try:
                box["result"] = self._invoke(agent, message, work, announce)
            except Exception as exc:
                box["error"] = exc
                if not getattr(exc, "status_code", None):
                    self.board.log(agent, f"Stopped: {exc}")
            finally:
                if rest and jobs.qsize() == 0:
                    self.board.set_task(agent, "The desk is open for the next request", working=False)
                done.set()

        jobs.put(job)
        done.wait()
        if box.get("error"):
            raise box["error"]
        return box.get("result")

    def _loop(self, name: str, jobs: queue.Queue) -> None:
        while True:
            job = jobs.get()
            try:
                job()
            finally:
                jobs.task_done()


def get_crew(board) -> Crew:
    """The desk has one crew. A different board, such as a test, gets its own."""
    global _crew
    with _crew_lock:
        if _crew is None or _crew.board is not board:
            _crew = Crew(board)
        return _crew
