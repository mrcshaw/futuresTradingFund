"""One thread and one model per agent. They share a single backtest engine."""

from __future__ import annotations

import queue
import threading

from futuresfund.roster import ROSTER

_crew = None
_crew_lock = threading.Lock()


class Crew:
    """Each agent runs jobs on their own thread. The engine lock is separate."""

    def __init__(self, board) -> None:
        self.board = board
        self.engine = threading.Lock()
        self._queues: dict[str, queue.Queue] = {}
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
            thread.start()

    def run(self, agent: str, message: str, work):
        """Run work on that agent's thread and wait for it. The log line is theirs alone."""
        if agent not in self._queues:
            self.board.log(agent, message)
            return work()
        if self.board.cancel.is_set():
            return None
        done = threading.Event()
        box: dict = {}

        def job() -> None:
            from futuresfund.llm import use_agent

            with use_agent(agent):
                self.board.log(agent, message)
                try:
                    box["result"] = work()
                except Exception as exc:
                    box["error"] = exc
                    self.board.log(agent, f"Stopped: {exc}")
                finally:
                    done.set()

        self._queues[agent].put(job)
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
