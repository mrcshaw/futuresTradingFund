"""The three standing PineTS engines, plus any engine the client adds."""

from __future__ import annotations

from dataclasses import dataclass, field


ORIGINAL = (
    (1, "2m"),
    (2, "5m"),
    (3, "1m"),
)


@dataclass
class Engine:
    id: int
    timeframe: str
    script: str = ""
    instrument: str = ""
    result: dict | None = None
    free: bool = True
    original: bool = False
    status: str = ""

    def card(self) -> dict:
        return {
            "id": self.id,
            "timeframe": self.timeframe,
            "instrument": self.instrument,
            "script": self.script,
            "result": self.result,
            "free": self.free,
            "original": self.original,
            "status": self.status,
        }


@dataclass
class EngineList:
    engines: list[Engine] = field(default_factory=list)
    aside: list[str] = field(default_factory=list)
    _next_id: int = 4

    def __post_init__(self) -> None:
        if not self.engines:
            self.engines = [Engine(engine_id, timeframe, original=True) for engine_id, timeframe in ORIGINAL]

    def cards(self) -> list[dict]:
        return [engine.card() for engine in self.engines]

    def add(self, timeframe: str) -> Engine:
        engine = Engine(self._next_id, timeframe, original=False)
        self._next_id += 1
        self.engines.append(engine)
        return engine

    def remove(self, engine_id: int) -> str:
        engine = self._get(engine_id)
        if engine.original:
            return "refused"
        self.engines = [item for item in self.engines if item.id != engine_id]
        return "removed"

    def start(self, engine_id: int, script: str, instrument: str = "") -> None:
        engine = self._get(engine_id)
        engine.script = script
        engine.instrument = instrument
        engine.result = None
        engine.free = False
        engine.status = "Working"

    def complete(self, engine_id: int, result: dict) -> None:
        engine = self._get(engine_id)
        engine.result = dict(result)
        engine.free = True
        engine.status = "Finished"

    def did_not_finish(self, engine_id: int) -> None:
        engine = self._get(engine_id)
        if engine.script:
            self.aside.append(engine.script)
        engine.script = ""
        engine.result = None
        engine.free = True
        engine.status = "Did not finish"

    def _get(self, engine_id: int) -> Engine:
        for engine in self.engines:
            if engine.id == engine_id:
                return engine
        raise KeyError(engine_id)
