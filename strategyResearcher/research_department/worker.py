"""The worker loads one script, accepts each response, and waits for a final answer."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class Worker:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.job_id: str | None = None
        self.finished = True
        self.accepted_script = ""
        self.best = None
        self.calls: list[tuple[str, str]] = []

    def submit(self, path: str, instrument: str) -> dict:
        if self.job_id and not self.finished:
            raise RuntimeError("A job is still open.")
        source = Path(path).read_text(encoding="utf-8")
        status, body = self._send("POST", "/scripts", {"source": source, "instrument": instrument})
        if status != 200 or body.get("refused"):
            self.job_id = None
            self.finished = True
            self.accepted_script = ""
            return body
        self.job_id = body["job_id"]
        self.finished = False
        self.accepted_script = ""
        self.best = None
        return body

    def poll(self) -> dict:
        if self.finished or not self.job_id:
            raise RuntimeError("No open job.")
        _status, body = self._send("GET", f"/jobs/{self.job_id}")
        state = body.get("state")
        if state == "passed_all":
            _result_status, result = self._send("GET", f"/jobs/{self.job_id}/result")
            self.accepted_script = result.get("script") or ""
            self.finished = True
            self.job_id = None
        elif state == "stopped":
            self.best = body.get("best")
            self.finished = True
            self.job_id = None
        return body

    def _send(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        self.calls.append((method, path))
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))
