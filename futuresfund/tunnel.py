"""The public address for this desk.

The desk listens on this machine only. cloudflared opens a temporary public address to it.
That address is replaced at 5:30pm New York, during the 5:00pm to 6:00pm session close,
and whenever the tunnel stops. The portfolio manager emails the new one through the desk
mail service. An address already emailed is not emailed again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL = "http://127.0.0.1:8788"
ET = ZoneInfo("America/New_York")
ISSUE_HOUR = 17
ISSUE_MINUTE = 30
CLOSE_START = 17 * 60
CLOSE_END = 18 * 60
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_STATE = Path.home() / ".futuresfund" / "public_url.json"
_LOG = Path.home() / ".futuresfund" / "cloudflared.log"
_BINARY = Path(__file__).resolve().parent.parent / ".tools" / "cloudflared.exe"

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB.
# The tunnel has to outlive a desk restart. A restart must not count as the 5:30pm expiry.
_DETACH = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000

_lock = threading.Lock()
_started = False
_dead_checks = 0


def address_from(text: str) -> str:
    """The last public address in a cloudflared log."""
    found = _URL.findall(text or "")
    return found[-1] if found else ""


def current_url() -> str:
    """The address the desk is serving, or empty when none has been opened."""
    with _lock:
        return str(_load().get("url") or "")


def view_line() -> str:
    """One line for an email. Empty until an address exists."""
    url = current_url()
    if not url:
        return ""
    return f"View the desk on your phone: {url}"


def webhook_line() -> str:
    """The TradingView alert address. Empty until the public address and the token both exist."""
    url = current_url()
    if not url:
        return ""
    from futuresfund.config import load_env, webhook_token

    load_env()
    token = webhook_token()
    if not token:
        return ""
    return f"TradingView price alerts: {url}/hooks/tradingview/{token}"


def service_lines() -> list[str]:
    """The address, the TradingView alert URL, and when the next one is due."""
    lines = [line for line in (view_line(), webhook_line()) if line]
    lines.append("This address is replaced at 5:30pm New York, during the session close. The portfolio manager emails the next one when it expires.")
    return lines


def expires_at(opened: float) -> float:
    """The 5:30pm New York after this address was opened. An address opened at 5:30pm lasts until the next day."""
    when = datetime.fromtimestamp(float(opened), ET)
    issue = when.replace(hour=ISSUE_HOUR, minute=ISSUE_MINUTE, second=0, microsecond=0)
    if when >= issue:
        issue += timedelta(days=1)
    return issue.timestamp()


def in_close_hour(now: float | None = None) -> bool:
    """True from 5:00pm New York until 6:00pm. That is the session close, before the 6:00pm open."""
    moment = time.time() if now is None else now
    when = datetime.fromtimestamp(moment, ET)
    minutes = when.hour * 60 + when.minute
    return CLOSE_START <= minutes < CLOSE_END


def expired(state: dict | None = None, now: float | None = None) -> bool:
    """True when the saved address has passed its 5:30pm New York issue time, or never recorded one."""
    state = _load() if state is None else state
    if not state.get("url"):
        return False
    opened = state.get("opened")
    if not opened:
        return True
    moment = time.time() if now is None else now
    try:
        due = expires_at(float(opened))
    except (TypeError, ValueError, OSError):
        return True
    return moment >= due


def issue_due(state: dict | None = None, now: float | None = None) -> bool:
    """Replace the address at 5:30pm, and only while the 5:00pm to 6:00pm close is still open."""
    moment = time.time() if now is None else now
    return expired(state, moment) and in_close_hour(moment)


def alert_link_ok(base_url: str, timeout: float = 20) -> bool:
    """The public alert address answers a read and a post. A failed check is not emailed."""
    import urllib.error
    import urllib.request

    from futuresfund.config import load_env, webhook_token

    load_env()
    token = webhook_token()
    if not token or not _URL.fullmatch(base_url or ""):
        return False
    hook = f"{base_url.rstrip('/')}/hooks/tradingview/{token}"
    try:
        with urllib.request.urlopen(hook, timeout=timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
            if response.status != 200 or "is live" not in page:
                return False
        request = urllib.request.Request(hook, data=b"desk-link-check", method="POST")
        request.add_header("Content-Type", "text/plain")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = response.read().decode("utf-8", errors="replace")
            return response.status == 200 and "desk-link-check" in answer
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False


def note_url(url: str, *, board=None, pid: int | None = None) -> bool:
    """Remember an address. The portfolio manager emails it once, and again when a different one replaces it.

    Returns True when the address changed.
    """
    found = address_from(url) or (url if _URL.fullmatch(url or "") else "")
    if not found:
        return False
    with _lock:
        state = _load()
        previous = str(state.get("url") or "")
        changed = found != previous
        already = found == state.get("emailed")
        state["url"] = found
        if changed or not state.get("opened"):
            state["opened"] = time.time()
        if pid:
            state["pid"] = int(pid)
        _save(state)
        pending = not already
    if pending and not alert_link_ok(found):
        _complain(board, "The TradingView alert address did not answer. It was not sent.")
        return changed
    if pending:
        with _lock:
            state = _load()
            if state.get("url") == found and state.get("emailed") != found:
                state["emailed"] = found
                _save(state)
            else:
                pending = False
    if pending:
        _announce(board, found, renewed=bool(previous) and changed)
    return changed


def renew(board=None) -> str:
    """Open a new public address and have the portfolio manager email it."""
    binary = _binary()
    if binary is None:
        raise RuntimeError("cloudflared is not on this machine, so there is no public address.")
    with _lock:
        state = _load()
        _stop(int(state.get("pid") or 0))
        pid, offset = _launch(binary)
    url = _wait_url(offset, 45)
    if not url:
        raise RuntimeError("The new public address did not come up.")
    note_url(url, board=board, pid=pid)
    return url


def supervise(board) -> None:
    """Watch the tunnel. A dead one is replaced, and the new address is emailed."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_watch, args=(board,), daemon=True, name="futures-tunnel").start()


def _announce(board, url: str, *, renewed: bool) -> None:
    lines = service_lines()
    if renewed:
        lines.insert(0, "The previous desk address expired.")
        subject = "Futures desk address changed"
    else:
        subject = "Futures desk address"
    text = "\n".join(lines)
    if board is not None:
        try:
            board.post("Portfolio Manager", text, kind="report", channel="Portfolio Manager")
        except Exception:
            pass
    try:
        from futuresfund.mailer import send_report

        send_report(text, subject)
    except Exception:
        pass


def _watch(board) -> None:
    failures = 0
    while True:
        try:
            if _holding(board):
                failures = 0
                time.sleep(20)
                continue
            binary = _binary()
            if binary is None:
                _complain(board, "cloudflared is not on this machine, so there is no phone address yet.")
                time.sleep(300)
                continue
            pid, offset = _launch(binary)
            url = _wait_url(offset, 45)
            if not url:
                failures += 1
                _complain(board, "The phone address did not come up. Another attempt follows.")
                time.sleep(min(300, 15 * failures))
                continue
            failures = 0
            note_url(url, board=board, pid=pid)
        except Exception as exc:
            failures += 1
            _complain(board, f"The phone address could not be opened: {exc}")
            time.sleep(min(300, 15 * failures))


def _holding(board) -> bool:
    """The tunnel that owns the saved address is still running.

    A quick address disappears when its process stops. Another cloudflared left
    from an older address does not keep this one alive. The saved process is
    stopped at 5:30pm New York, while the 5:00pm to 6:00pm close is still open,
    so the watch opens the next address.
    """
    global _dead_checks
    state = _load()
    if issue_due(state):
        _stop(int(state.get("pid") or 0))
        _dead_checks = 0
        return False
    pid = int(state.get("pid") or 0)
    if not _alive(pid):
        # One failed check is not a new address. The scheduled replacement is 5:30pm.
        _dead_checks += 1
        if _dead_checks < 3:
            return True
        return False
    _dead_checks = 0
    url = str(state.get("url") or "")
    if url and url != state.get("emailed"):
        note_url(url, board=board, pid=pid)
    return True


def _binary() -> Path | None:
    if _BINARY.is_file() and _BINARY.stat().st_size > 1_000_000:
        return _BINARY
    return None


def _launch(binary: Path) -> tuple[int, int]:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    offset = _LOG.stat().st_size if _LOG.is_file() else 0
    handle = open(_LOG, "a", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            [
                str(binary),
                "tunnel",
                "--url",
                LOCAL,
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
        url = address_from(_read_log()[offset:])
        if url:
            return url
        time.sleep(0.5)
    return ""


def _read_log() -> str:
    try:
        return _LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _stop(pid: int) -> None:
    """Stop the tunnel this desk opened. A process that is already gone is left alone."""
    if not _alive(pid):
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


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
    text = out.stdout or ""
    return str(pid) in text and "No tasks" not in text


def _other_cloudflared() -> int:
    """A cloudflared this module did not record. It is not stopped."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in (out.stdout or "").splitlines():
        parts = [part.strip('"') for part in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == "cloudflared.exe" and parts[1].isdigit():
            return int(parts[1])
    return 0


def _complain(board, text: str) -> None:
    if board is None:
        return
    try:
        board.post("Systems Administrator", text, kind="error", channel="Systems Administrator")
    except Exception:
        pass


def _load() -> dict:
    try:
        data = json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STATE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, _STATE)
