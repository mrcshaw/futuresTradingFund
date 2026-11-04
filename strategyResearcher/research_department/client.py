"""The page client reads the department. It does not load scripts or take the finished one."""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ResearchClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.calls: list[tuple[str, str]] = []

    def feed(self, job_id: str) -> dict:
        return self._send("GET", f"/jobs/{job_id}/feed")

    def engines(self) -> dict:
        return self._send("GET", "/engines")

    def settings(self) -> dict:
        return self._send("GET", "/settings")

    def save_settings(self, **values) -> dict:
        return self._send("PUT", "/settings", values)

    def _send(self, method: str, path: str, payload: dict | None = None) -> dict:
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
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return json.loads(exc.read().decode("utf-8"))
