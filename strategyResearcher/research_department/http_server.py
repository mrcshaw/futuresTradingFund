"""The department routes on a local port that the test closes."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from research_department.department import Department


STATIC = Path(__file__).resolve().parent / "static"
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class DepartmentServer:
    def __init__(self, department: Department, port: int = 0) -> None:
        handler = _handler(department)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.url = f"http://{host}:{port}"
        if hasattr(department, "bind"):
            department.bind(self.url)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


def _handler(department: Department):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/library/text" and hasattr(department, "script_text"):
                query = parse_qs(parsed.query)
                file_name = (query.get("file") or [""])[0]
                change = (query.get("change") or [""])[0]
                self._send(*department.script_text(file_name, change))
                return
            if path in {"/", "/index.html"}:
                self._file(STATIC / "index.html")
                return
            if path == "/library":
                self._file(STATIC / "library.html")
                return
            if path == "/researcher":
                self._file(STATIC / "researcher.html")
                return
            if path.startswith("/static/"):
                self._file(STATIC / path.removeprefix("/static/"))
                return
            if path == "/desk" and hasattr(department, "get_desk"):
                self._send(*department.get_desk())
                return
            if path == "/engines":
                self._send(*department.get_engines())
                return
            if path == "/settings":
                self._send(*department.get_settings())
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[0] == "jobs":
                self._send(*department.get_job(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "feed":
                self._send(*department.get_feed(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "result":
                self._send(*department.get_result(parts[1]))
                return
            self._send(404, {"reason": "That route is not in the department."})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._body()
            if path == "/scripts":
                self._send(*department.post_script(body.get("source", ""), body.get("instrument", "")))
                return
            if path == "/worker/start" and hasattr(department, "start_worker"):
                self._send(*department.start_worker())
                return
            if path == "/worker/stop" and hasattr(department, "stop_worker"):
                self._send(*department.stop_worker())
                return
            if path == "/engines":
                self._send(*department.post_engine(str(body.get("timeframe") or "")))
                return
            self._send(404, {"reason": "That route is not in the department."})

        def do_PUT(self) -> None:
            if urlparse(self.path).path == "/settings":
                self._send(*department.put_settings(self._body()))
                return
            self._send(404, {"reason": "That route is not in the department."})

        def do_DELETE(self) -> None:
            parts = [part for part in urlparse(self.path).path.split("/") if part]
            if len(parts) == 2 and parts[0] == "strategies" and hasattr(department, "delete_strategy"):
                self._send(*department.delete_strategy(parts[1]))
                return
            if len(parts) == 2 and parts[0] == "catalog" and hasattr(department, "delete_result"):
                self._send(*department.delete_result(parts[1]))
                return
            if len(parts) == 2 and parts[0] == "engines":
                self._send(*department.delete_engine(int(parts[1])))
                return
            self._send(404, {"reason": "That route is not in the department."})

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _file(self, path: Path) -> None:
            try:
                target = path.resolve()
                target.relative_to(STATIC.resolve())
                raw = target.read_bytes()
            except (OSError, ValueError):
                self._send(404, {"reason": "That page is not in the department."})
                return
            self.send_response(200)
            self.send_header("Content-Type", _TYPES.get(target.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler
