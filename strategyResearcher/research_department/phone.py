"""A public address for this department.

The futures desk already has its own tunnel on port 8788. This one is only for the
research page, and it never stops that desk tunnel.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path


LOCAL_PORT = 8790
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_ROOT = Path(__file__).resolve().parents[1]
_STATE = _ROOT / "data" / "phone.json"
_LOG = _ROOT / "data" / "cloudflared.log"
_BINARY = Path(__file__).resolve().parents[2] / ".tools" / "cloudflared.exe"
_DETACH = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000
_lock = threading.Lock()


def phone_url() -> str:
    override = os.environ.get("RESEARCH_PUBLIC_URL", "").strip()
    if override:
        return override
    try:
        data = json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("url") or "")


def ensure(port: int = LOCAL_PORT) -> str:
    """Open a tunnel when this department does not already have a live one."""
    with _lock:
        saved = _read()
        if saved.get("url") and _alive(int(saved.get("pid") or 0)):
            return str(saved["url"])
        binary = _binary()
        if binary is None:
            return ""
        pid, offset = _launch(binary, port)
        url = _wait_url(offset, 45)
        if not url:
            return ""
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps({"url": url, "pid": pid}), encoding="utf-8")
        print(f"Research department on your phone {url}", flush=True)
        return url


def _read() -> dict:
    try:
        data = json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _binary() -> Path | None:
    if _BINARY.is_file() and _BINARY.stat().st_size > 1_000_000:
        return _BINARY
    return None


def _launch(binary: Path, port: int) -> tuple[int, int]:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    offset = _LOG.stat().st_size if _LOG.is_file() else 0
    handle = open(_LOG, "a", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            [
                str(binary),
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
                "--proxy-keepalive-timeout",
                "60s",
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_DETACH,
            close_fds=True,
        )
    finally:
        handle.close()
    return proc.pid, offset


def _wait_url(offset: int, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = _LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        found = _URL.findall(text[offset:])
        if found:
            return found[-1]
        time.sleep(0.5)
    return ""


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return str(pid) in (out.stdout or "")
