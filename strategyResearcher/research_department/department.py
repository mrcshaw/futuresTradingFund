"""In-memory department. The HTTP routes call this. Meetings and PineTS stay outside."""

from __future__ import annotations

from dataclasses import dataclass, field

from research_department.engines import EngineList
from research_department.stages import DEFAULT_ATTEMPTS, DEFAULT_SUCCESS_RATE, DAILY_LOSS, DAILY_TARGET


SETTING_KEYS = ("success_rate", "attempts", "daily_target", "daily_loss", "baseline_risk")


def default_settings() -> dict:
    return {
        "success_rate": DEFAULT_SUCCESS_RATE,
        "attempts": DEFAULT_ATTEMPTS,
        "daily_target": DAILY_TARGET,
        "daily_loss": DAILY_LOSS,
        "baseline_risk": 250,
    }


@dataclass
class Job:
    job_id: str
    source: str
    instrument: str
    states: list[str] = field(default_factory=list)
    feed: dict = field(default_factory=dict)
    result_script: str = ""
    best: dict | None = None
    index: int = 0

    def next_state(self) -> str:
        if not self.states:
            return "meeting"
        state = self.states[min(self.index, len(self.states) - 1)]
        if self.index < len(self.states) - 1:
            self.index += 1
        return state


class Department:
    """The routes the worker and the client share."""

    def __init__(self) -> None:
        self.engines = EngineList()
        self.settings = default_settings()
        self.jobs: dict[str, Job] = {}
        self.open_job: str | None = None
        self._next_job = 1
        self._prepared: tuple[list[str], dict, str] | None = None

    def prepare(self, states: list[str], feed: dict, result_script: str = "", best: dict | None = None) -> None:
        """Script the next loaded job. The ordinary run never calls a model."""
        self._prepared = (list(states), dict(feed), result_script)
        self._prepared_best = best

    def post_script(self, source: str, instrument: str) -> tuple[int, dict]:
        if not str(source or "").strip() or not str(instrument or "").strip():
            return 400, {"refused": True, "reason": "The file or the instrument is missing."}
        if self.open_job:
            return 409, {"refused": True, "reason": "A job is still open."}
        job_id = f"job-{self._next_job}"
        self._next_job += 1
        states, feed, result_script = self._prepared or ([], {}, "")
        best = getattr(self, "_prepared_best", None)
        self._prepared = None
        self._prepared_best = None
        self.jobs[job_id] = Job(job_id, source, instrument, states, feed, result_script, best)
        self.open_job = job_id
        return 200, {"job_id": job_id, "refused": False}

    def get_job(self, job_id: str) -> tuple[int, dict]:
        job = self.jobs.get(job_id)
        if job is None:
            return 404, {"reason": "That job is not loaded."}
        state = job.next_state()
        if state == "stopped":
            self.open_job = None
        body = {"job_id": job_id, "state": state, "instrument": job.instrument}
        if state == "stopped":
            body["best"] = job.best
        return 200, body

    def get_feed(self, job_id: str) -> tuple[int, dict]:
        job = self.jobs.get(job_id)
        if job is None:
            return 404, {"reason": "That job is not loaded."}
        return 200, job.feed

    def get_result(self, job_id: str) -> tuple[int, dict]:
        job = self.jobs.get(job_id)
        if job is None:
            return 404, {"reason": "That job is not loaded."}
        if not job.states or job.states[min(job.index, len(job.states) - 1)] != "passed_all":
            return 409, {"reason": "The job has not passed every stage."}
        self.open_job = None
        return 200, {"job_id": job_id, "script": job.result_script}

    def get_engines(self) -> tuple[int, dict]:
        return 200, {"engines": self.engines.cards()}

    def post_engine(self, timeframe: str) -> tuple[int, dict]:
        engine = self.engines.add(timeframe)
        return 200, engine.card()

    def delete_engine(self, engine_id: int) -> tuple[int, dict]:
        try:
            outcome = self.engines.remove(engine_id)
        except KeyError:
            return 404, {"reason": "That engine is not on the desk."}
        if outcome == "refused":
            return 409, {"refused": True, "reason": "The original three engines stay."}
        return 200, {"removed": engine_id}

    def get_settings(self) -> tuple[int, dict]:
        return 200, dict(self.settings)

    def put_settings(self, values: dict) -> tuple[int, dict]:
        for key in SETTING_KEYS:
            if key in values and values[key] is not None:
                self.settings[key] = values[key]
        return 200, dict(self.settings)
