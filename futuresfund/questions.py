"""Questions for the owner, asked by the chart researcher and carried by email by the Portfolio Manager.

Who does what:
- The chart researcher studying the script writes the question (ask_owner). Code builds the numbered
  options so a reply stays parseable; the researcher's model writes the words around them on the
  researcher's decision queue (learn.researcher_job), with a plain template when the model is slow or
  unreachable. The question is posted on the researcher's channel.
- The Portfolio Manager collects the open questions and emails them on its own outbox thread: one
  email a minute at most, a digest when several wait (send_pending). It reads the replies (poll_inbox,
  the /hooks/mail webhook, or the desk's answer box) and hands each answer to the researcher who asked.
- The researcher who asked applies the answer on its own worker (apply_answer): new input values, the
  desk trade limits, a pasted script, a retry, or park. The chart developer reruns the backtests.

A blocking question moves the script and its Python twin to awaitingAnswerStrategies (project root,
next to cannotRunStrategies) with <stem>.question.txt beside it, and the engine takes the next script.
cannotRunStrategies is now only where the owner's "park" answer sends a script.

Records live in researchNotes/questions/<qid>.json (a subfolder, because several modules read every
top-level researchNotes/*.json as script notes) and are kept in memory once loaded: desk file I/O is slow.

Nothing is emailed unless start(board) turned mail on, FUTURES_MAIL_OFF is not set (mailer.mail_blocked),
and the process is not a test run (mailer.test_run): a test run never starts the outbox or inbox thread.
The inbox is never opened from a test run with the real imaplib.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
from datetime import datetime
from email import policy
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

EMAIL_QUESTIONS = True        # the researchers' questions go to the owner by email (off: blocked scripts are parked the old way)
INBOX_POLL_SECONDS = 120      # how often the Portfolio Manager reads the mailbox for answers
SEND_GAP_SECONDS = 60         # at most one question email a minute; more waiting go out together as a digest
SEND_RETRY_MAX = 3600         # a failed question email is tried again after 2, 4, 8 ... minutes, at most an hour apart
WORDING_WAIT_SECONDS = 90     # a question waits this long for the researcher's own words before the template goes out
MODEL_SECONDS = 120           # the researcher's model has this long to word a question or read a free-text answer

_HOME = Path(__file__).resolve().parent
LIVE_STORE = _HOME / "researchNotes" / "questions"
LIVE_AWAITING = _HOME.parent / "awaitingAnswerStrategies"
FOLDER_NAME = "awaitingAnswerStrategies"
# Patched by tests. None means the desk's own folders: researchNotes/questions, and the awaiting folder
# next to learn.CANNOT_RUN (so a test that moves cannotRunStrategies to a temp folder moves this one too).
STORE: Path | None = None
AWAITING: Path | None = None

_REAL_IMAP = imaplib.IMAP4_SSL
QID = re.compile(r"\bFFQ-[0-9a-f]{6}\b", re.IGNORECASE)
_ET = ZoneInfo("America/New_York")

PM = "Portfolio Manager"
ADMIN = "Systems Administrator"

KINDS: dict[str, dict] = {
    "compile": {
        "title": "it still does not compile",
        "blocking": True,
        "need": "a fixed script, a hint for the developer, retry for more repairs, or park",
    },
    "repair": {
        "title": "a compile repair started",
        "blocking": False,
        "need": "a fixed script or a hint for the developer; the desk's own repair runs meanwhile",
    },
    "entry_rule": {
        "title": "no entry rule could be added",
        "blocking": True,
        "need": "a fixed script, retry, or park",
    },
    "unrunnable": {
        "title": "it stops in PineTS",
        "blocking": True,
        "need": "input values that let it run (name = value), a fixed script, retry, or park",
    },
    "no_trades": {
        "title": "it never takes a trade",
        "blocking": True,
        "need": "session or filter values that let it enter (name = value), a fixed script, retry, or park",
    },
    "over_trades": {
        "title": "it takes too many trades",
        "blocking": True,
        "need": "trade limits such as desk_max_trades_day = 2 or desk_daily_loss = 300, retry, or park",
    },
}

STATUSES = ("open", "answered", "applied", "parked")

_lock = threading.RLock()
_index: dict[str, dict] | None = None
_index_dir: str = ""
_state = {"board": None, "mail_on": False, "last_sent": 0.0, "login_posted": False, "threads": []}
_hints: dict[str, list[str]] = {}


# ---------- folders and storage ----------

def store_dir() -> Path:
    return Path(STORE) if STORE is not None else LIVE_STORE


def awaiting_dir() -> Path:
    if AWAITING is not None:
        return Path(AWAITING)
    from futuresfund import learn

    return Path(learn.CANNOT_RUN).parent / FOLDER_NAME


def _same(left: Path, right: Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def _protected(folder: Path) -> bool:
    """A test run never writes into the desk's live folders. It keeps those records in memory only."""
    from futuresfund.mailer import test_run

    if not test_run():
        return False
    folder = Path(folder)
    live = (LIVE_STORE, LIVE_AWAITING, _HOME / "researchNotes", _HOME.parent / "cannotRunStrategies",
            _HOME / "learningStrategies", _HOME / "createdStrategies", _HOME / "learnedStrategies")
    for place in live:
        if _same(folder, place):
            return True
        try:
            Path(folder).resolve().relative_to(Path(place).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _now() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S")


def _load() -> dict[str, dict]:
    """The question index, read once per store folder."""
    global _index, _index_dir
    folder = store_dir()
    key = str(folder)
    if _index is not None and _index_dir == key:
        return _index
    found: dict[str, dict] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json") or name.startswith("_"):
            continue
        try:
            record = json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and QID.fullmatch(str(record.get("qid") or "")):
            found[record["qid"]] = record
    _index, _index_dir = found, key
    return _index


def clear_memory() -> None:
    """Forget the loaded index and hints (tests, or a store folder that changed)."""
    global _index, _index_dir
    with _lock:
        _index, _index_dir = None, ""
        _hints.clear()
        _state["login_posted"] = False


def _save(record: dict) -> None:
    with _lock:
        _load()[record["qid"]] = record
        folder = store_dir()
        if _protected(folder):
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{record['qid']}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            traceback.print_exc()


def _inbox_state() -> dict:
    folder = store_dir()
    try:
        data = json.loads((folder / "_inbox.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("processed", [])
    return data


def _save_inbox_state(data: dict) -> None:
    folder = store_dir()
    if _protected(folder):
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        data["processed"] = list(data.get("processed") or [])[-500:]
        (folder / "_inbox.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def new_qid() -> str:
    with _lock:
        known = _load()
        while True:
            qid = f"FFQ-{secrets.token_hex(3)}"
            if qid not in known:
                return qid


def norm(qid) -> str:
    """FFQ- in capitals, then six lowercase hex digits. "" when the text holds no question id."""
    found = QID.search(str(qid or ""))
    return f"FFQ-{found.group(0)[4:].lower()}" if found else ""


def get(qid: str) -> dict | None:
    with _lock:
        return _load().get(norm(qid))


def all_questions() -> list[dict]:
    with _lock:
        return sorted(_load().values(), key=lambda row: str(row.get("asked_at") or ""))


def open_questions() -> list[dict]:
    return [row for row in all_questions() if row.get("status") == "open"]


def _open_for(file: str, kind: str) -> dict | None:
    for record in all_questions():
        if record.get("status") == "open" and record.get("file") == file and record.get("kind") == kind:
            return record
    return None


def view() -> dict:
    """What GET /api/questions returns: the open questions and the last answered ones. No secrets."""
    rows = all_questions()
    shown = lambda row: {key: row.get(key) for key in (
        "qid", "file", "kind", "title", "reason", "status", "asked_at", "sent_at", "answered_at", "applied_at",
        "applied_default", "blocking", "asked_by", "chart", "options", "answer_text", "answer_parsed", "result",
    )}
    recent = [row for row in rows if row.get("status") != "open"][-20:]
    return {
        "open": [shown(row) for row in rows if row.get("status") == "open"],
        "recent": [shown(row) for row in reversed(recent)],
        "email": EMAIL_QUESTIONS,
        "mail_on": bool(_state["mail_on"]),
        "inbox_configured": inbox_configured(),
    }


def live_rows() -> list[dict]:
    """The open questions the Research workflow page lists on the awaiting node."""
    return [
        {
            "qid": row.get("qid"),
            "file": row.get("file"),
            "kind": row.get("kind"),
            "title": row.get("title"),
            "asked_at": row.get("asked_at"),
            "sent_at": row.get("sent_at"),
            "asked_by": row.get("asked_by"),
            "chart": row.get("chart"),
            "blocking": bool(row.get("blocking")),
            "applied_default": row.get("applied_default"),
            "options": row.get("options"),
        }
        for row in open_questions()
    ]


# ---------- asking ----------

def _options(kind: str) -> list[dict]:
    """The numbered answers code understands. Each one is an action."""
    rows: list[dict] = []
    if kind == "repair":
        rows.append({"action": "keep", "text": "Let the developer's own repair go on"})
    elif kind == "over_trades":
        rows.append({"action": "params", "params": {"desk_max_trades_day": 2}, "text": "Allow at most 2 trades a day (desk_max_trades_day = 2) and test it again"})
        rows.append({"action": "params", "params": {"desk_max_trades_day": 1}, "text": "Allow at most 1 trade a day (desk_max_trades_day = 1) and test it again"})
    if kind == "compile":
        rows.append({"action": "retry", "text": "Give the developer another round of repairs"})
    elif kind != "repair":
        rows.append({"action": "retry", "text": "Put it back in the queue as it is"})
    rows.append({"action": "park", "text": "Park it in cannotRunStrategies"})
    for number, row in enumerate(rows, start=1):
        row["n"] = number
    return rows


def _meanwhile(kind: str) -> str:
    if KINDS[kind]["blocking"]:
        return f"The script waits in {FOLDER_NAME} and the engine took the next script."
    return "The developer's own repair is running and the script stays in its folder."


def _chart_line(chart: dict) -> str:
    parts = [str(chart.get(key) or "") for key in ("root", "timeframe")]
    text = " ".join(part for part in parts if part)
    if chart.get("engine"):
        text = f"{text} chart, engine {chart['engine']}" if text else f"engine {chart['engine']}"
    elif text:
        text = f"{text} chart"
    return text


def _template(record: dict) -> str:
    chart = _chart_line(record.get("chart") or {})
    where = f" on the {chart}" if chart else ""
    return (
        f"I am the {record.get('asked_by') or 'Chart researcher'}. {record['file']}{where}: {record['title']}. "
        f"{record['reason']} {record['applied_default']} I need {KINDS[record['kind']]['need']}."
    )


def ask_owner(board, path, author: str, reason: str, kind: str, *, context: dict | None = None, chart: dict | None = None) -> str:
    """The chart researcher asks the owner. Returns "awaiting" for a blocking question, "" otherwise.

    A blocking question moves the script and its twin to awaitingAnswerStrategies. An open question on the
    same file and kind is reused, so one script never sends two emails about one problem.
    """
    from futuresfund import learn

    path = Path(path)
    kind = kind if kind in KINDS else "unrunnable"
    blocking = bool(KINDS[kind]["blocking"])
    author = author or "Chart researcher"
    reason = " ".join(str(reason or "").split()) or "The desk could not continue with this script."
    if not EMAIL_QUESTIONS:
        if blocking:
            return learn.leave_unrunnable(board, path, author, reason)
        return ""
    chart = dict(chart or learn._chart_context() or {})
    with _lock:
        record = _open_for(path.name, kind)
        fresh = record is None
        if fresh:
            record = {
                "qid": new_qid(),
                "file": path.name,
                "origin_folder": str(path.parent),
                "timeframe": chart.get("timeframe") or "",
                "root": chart.get("root") or "",
                "chart": {key: chart.get(key) for key in ("root", "timeframe", "engine") if chart.get(key)},
                "kind": kind,
                "title": KINDS[kind]["title"],
                "reason": reason[:4000],
                "context": dict(context or {}),
                "options": _options(kind),
                "default_action": "wait" if blocking else "repair",
                "applied_default": _meanwhile(kind),
                "blocking": blocking,
                "status": "open",
                "asked_by": author,
                "developer": chart.get("developer") or "",
                "asked_at": _now(),
                "asked_ts": time.time(),
                "explanation": "",
                "worded_by": "",
                "sent_at": None,
                "message_id": "",
                "answer_text": "",
                "answer_from": "",
                "answer_parsed": None,
                "answered_at": None,
                "applied_at": None,
                "followups": 0,
                "followup_pending": "",
                "result": "",
            }
            record["explanation"] = _template(record)
            _save(record)
    qid = record["qid"]
    if blocking:
        _move_to_awaiting(path, record)
        learn._stage_end.reason = f"It waits in {FOLDER_NAME} for the owner's emailed answer {qid}."
    if board is not None and fresh:
        chart_text = _chart_line(record.get("chart") or {})
        where = f" on the {chart_text}" if chart_text else ""
        board.post(
            author,
            f"Question {qid} for the owner about {path.name}{where}: {record['title']}. {reason} "
            f"{record['applied_default']} The Portfolio Manager emails it.",
            kind="report",
            channel=author,
        )
    if blocking and board is not None:
        developer = chart.get("developer") or author
        board.post(
            developer,
            f"{path.name} waits in {FOLDER_NAME} for your emailed answer {qid}. The engine moved on to the next script.",
            kind="log",
            channel=developer,
        )
    if fresh:
        _queue_wording(board, qid)
    return "awaiting" if blocking else ""


def _move_to_awaiting(path: Path, record: dict) -> None:
    from futuresfund import learn

    folder = awaiting_dir()
    if not path.is_file() or _protected(folder):
        return
    if not (learn._in_learning(path) or learn._in_created(path)):
        return
    folder.mkdir(parents=True, exist_ok=True)
    twin = learn.companion(path) if path.suffix.lower() == ".pine" else None
    try:
        target = learn._place_aside(path, folder)
    except FileNotFoundError:
        # Another engine asked about the same file a moment earlier and moved it. It already waits there.
        return
    if twin is not None and twin.is_file():
        record["twin"] = learn._place_aside(twin, folder).name
    record["origin_folder"] = str(path.parent)
    record["held_name"] = target.name
    lines = [
        f"{record['qid']}  {record['file']}: {record['title']}",
        f"Asked by the {record['asked_by']} at {record['asked_at']}.",
        "",
        record["reason"],
        "",
        record["applied_default"],
        "",
        "Options:",
        *[f"  {row['n']}. {row['text']}" for row in record["options"]],
        "",
        "Answer by email, or in the awaiting node on the Research workflow page.",
    ]
    (folder / f"{target.stem}.question.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with _lock:
        _save(record)


def _queue_wording(board, qid: str) -> None:
    """The researcher's model words the question on the researcher's decision queue, never on a chart thread."""
    from futuresfund import learn

    if board is None:
        return
    from futuresfund.mailer import test_run

    if test_run() and _model_reply is _MODEL_REPLY:
        # A test run never waits on the model: the template wording stands.
        record = get(qid)
        if record is not None and not record.get("worded_by"):
            record["worded_by"] = "template"
            _save(record)
        return
    learn.researcher_job(f"question|{qid}", lambda: word_question(qid))


def word_question(qid: str) -> str:
    """The researcher's own words for the question. The template stays when the model gives nothing usable."""
    record = get(qid)
    if record is None or record.get("worded_by"):
        return ""
    words = ""
    try:
        words = _model_reply(record.get("asked_by") or "Chart researcher", (
            "You are the chart researcher on a futures research desk. Write a short email paragraph to the owner, "
            "in plain sentences, in the first person. Say what happened to the script, what the desk is doing meanwhile, "
            "and what you need from the owner. Use only the facts below. Do not list options; they are added after your text. "
            "No greeting, no sign-off, at most 120 words."
        ), "\n".join([
            f"Script: {record['file']}",
            f"Chart: {_chart_line(record.get('chart') or {}) or 'not known'}",
            f"What happened: {record['title']}. {record['reason']}",
            f"Meanwhile: {record['applied_default']}",
            f"What you need: {KINDS[record['kind']]['need']}",
        ]))
    except Exception:
        traceback.print_exc()
    words = " ".join(str(words or "").split())
    usable = 20 <= len(words) <= 1500
    with _lock:
        record = get(qid) or record
        if usable:
            record["explanation"] = words
            record["worded_by"] = "model"
        else:
            record["worded_by"] = "template"
        _save(record)
    return record["explanation"]


def _model_reply(agent: str, system: str, user: str) -> str:
    """One reply from that agent's model, or "" when the model is slow or unreachable."""
    from futuresfund.llm import _request_parts, model_name

    parts = _request_parts(
        model_name(agent),
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        think=False,
        num_ctx=4096,
        timeout=MODEL_SECONDS,
    )
    if parts.get("error"):
        return ""
    return str(parts.get("content") or "")


_MODEL_REPLY = _model_reply


# ---------- the email out ----------

REPLY_HELP = (
    "How to reply: reply with a number, or lines like stop_dollars = 300, or park, or retry, "
    "or write it in your own words. You can paste a fixed Pine script (starting with //@version). "
    "When one email asks several questions, start each answer's line with its id, for example "
    "FFQ-1a2b3c: 2."
)


def _question_block(record: dict) -> str:
    lines = [f"{record['qid']}  {record['file']}: {record['title']}"]
    chart = _chart_line(record.get("chart") or {})
    if chart:
        lines.append(f"Chart: {chart}")
    if record.get("followup_pending"):
        lines.append(record["followup_pending"])
    else:
        lines.append(record.get("explanation") or _template(record))
        if record.get("explanation") and record["reason"] not in record["explanation"]:
            lines.append(f"What the desk measured: {record['reason']}")
    lines.append(f"Meanwhile: {record['applied_default']}")
    lines.append("Options:")
    lines.extend(f"  {row['n']}. {row['text']}" for row in record.get("options") or [])
    return "\n".join(lines)


def compose(records: list[dict]) -> tuple[str, str]:
    """The subject and body of one email. One question, or a digest of several."""
    if len(records) == 1:
        record = records[0]
        lead = "Follow-up: " if record.get("followup_pending") else ""
        subject = f"[{record['qid']}] {lead}{record['file']}: {record['title']}"
        body = "\n\n".join([
            f"The {record.get('asked_by') or 'Chart researcher'} has a question about {record['file']}.",
            _question_block(record),
            REPLY_HELP,
        ])
        return subject, body
    ids = " ".join(record["qid"] for record in records)
    subject = f"[{ids}] {len(records)} questions from the research desk"
    blocks = [f"The researchers have {len(records)} questions. Each one has its own id."]
    blocks.extend(_question_block(record) for record in records)
    blocks.append(REPLY_HELP)
    return subject, "\n\n".join(blocks)


def _ready(record: dict, now: float) -> bool:
    if record.get("status") != "open":
        return False
    if float(record.get("next_try") or 0) > now:
        # The last send failed. The next try waits longer each time (SEND_RETRY_MAX at most).
        return False
    if record.get("followup_pending"):
        return True
    if record.get("sent_at"):
        return False
    return bool(record.get("worded_by")) or now - float(record.get("asked_ts") or 0) >= WORDING_WAIT_SECONDS


def mail_allowed() -> bool:
    """Question mail goes out only after start(board) turned it on, never with FUTURES_MAIL_OFF, never from a test run."""
    from futuresfund.mailer import mail_blocked, test_run

    return bool(_state["mail_on"]) and EMAIL_QUESTIONS and not mail_blocked() and not test_run()


def send_pending(board=None, *, now: float | None = None) -> list[str]:
    """The Portfolio Manager emails the questions that are ready. Returns the ids it sent."""
    from futuresfund import mailer

    board = board if board is not None else _state["board"]
    now = time.time() if now is None else now
    with _lock:
        ready = [record for record in all_questions() if _ready(record, now)]
    if not ready or not mail_allowed():
        return []
    subject, body = compose(ready)
    result = mailer.send_question(subject, body, {}) or {}
    ids = [record["qid"] for record in ready]
    reason = str(result.get("reason") or "not sent")
    # A failure is posted once for each new reason, not on every retry.
    new_failure = not result.get("sent") and any((get(record["qid"]) or record).get("send_error") != reason for record in ready)
    with _lock:
        for record in ready:
            current = get(record["qid"]) or record
            if result.get("sent"):
                current["send_failures"] = 0
                current["next_try"] = 0
                current["sent_at"] = _now()
                current["message_id"] = result.get("message_id") or current.get("message_id") or ""
                ids_seen = list(current.get("message_ids") or [])
                if current["message_id"]:
                    ids_seen.append(current["message_id"])
                current["message_ids"] = ids_seen[-5:]
                current["followup_pending"] = ""
            else:
                current["send_error"] = reason
                failures = int(current.get("send_failures") or 0) + 1
                current["send_failures"] = failures
                current["next_try"] = now + min(SEND_RETRY_MAX, SEND_GAP_SECONDS * (2 ** min(failures, 12)))
            _save(current)
    _state["last_sent"] = now
    if board is not None:
        for record in ready:
            if result.get("sent"):
                board.post(PM, f"Emailed {record['qid']} for the {record.get('asked_by') or 'Chart researcher'} ({record['file']}).", kind="log", channel=PM)
        if not result.get("sent") and new_failure:
            board.post(
                PM,
                f"The questions {' '.join(ids)} were not emailed. {reason} The desk tries again later, waiting longer after each failure.",
                kind="report",
                channel=PM,
            )
    return ids if result.get("sent") else []


# ---------- the email in ----------

def _imap_settings() -> tuple[str, int, str, str, str]:
    host = os.environ.get("IMAP_HOST", "").strip() or "imap.gmail.com"
    try:
        port = int(os.environ.get("IMAP_PORT", "") or 993)
    except ValueError:
        port = 993
    user = os.environ.get("IMAP_USER", "").strip() or os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("IMAP_PASSWORD", "") or os.environ.get("SMTP_PASSWORD", "")
    folder = os.environ.get("IMAP_FOLDER", "").strip() or "INBOX"
    return host, port, user, password, folder


def inbox_configured() -> bool:
    _, _, user, password, _ = _imap_settings()
    return bool(user and str(password).strip())


def allowed_senders() -> set[str]:
    from futuresfund.mailer import report_recipients

    allowed = {address.strip().lower() for address in report_recipients() if address.strip()}
    for part in os.environ.get("QUESTION_FROM", "").split(","):
        if part.strip():
            allowed.add(part.strip().lower())
    return allowed


def authenticated(results: str | list | None, sender: str = "") -> bool:
    """Authentication-Results must show the reply passing when the header is there. A dmarc fail is refused.

    Only the topmost header counts: the receiving server (mx.google.com) adds its header on top, and a
    header the sender wrote lower down is not trusted. dmarc=pass passes. A dkim=pass or spf=pass
    passes when the domain it names (header.d, header.i, smtp.mailfrom) is the From address's domain,
    so a forger's own server passing for its own domain does not count.
    """
    if not results:
        return True
    if isinstance(results, (list, tuple)):
        results = next((str(item) for item in results if str(item or "").strip()), "")
    lowered = str(results).lower()
    if not lowered.strip():
        return True
    if re.search(r"\bdmarc=fail\b", lowered):
        return False
    if re.search(r"\bdmarc=pass\b", lowered):
        return True
    domain = str(sender or "").rsplit("@", 1)[-1].strip().lower() if "@" in str(sender or "") else ""
    for found in re.finditer(r"\b(dkim|spf)=pass\b([^;]*)", lowered):
        named = re.findall(r"\b(?:header\.d|header\.i|smtp\.mailfrom)=@?(?:[^@\s;]*@)?([a-z0-9.-]+)", found.group(2))
        if not named or not domain:
            # A result with no domain on it, or no From address to hold it to.
            return True
        if any(item == domain or item.endswith("." + domain) for item in named):
            return True
    return False


_WROTE = re.compile(r"(?im)^[ \t]*On\b[^\n]*(?:\n[^\n]*){0,2}?wrote:[ \t]*$")
_ORIGINAL = re.compile(r"(?im)^[ \t]*(-{2,}\s*Original Message\s*-{2,}|-{2,}\s*Forwarded message\s*-{2,}|From:\s.+\nSent:\s)")


def strip_quoted(text: str) -> str:
    """The answer the owner wrote, without the quoted email under it."""
    body = str(text or "").replace("\r\n", "\n")
    for pattern in (_WROTE, _ORIGINAL):
        found = pattern.search(body)
        if found:
            body = body[: found.start()]
    lines = [line for line in body.split("\n") if not line.lstrip().startswith(">")]
    # A signature line ("-- ") ends the answer.
    kept = []
    for line in lines:
        if line.rstrip() == "--":
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _plain_text(message) -> str:
    part = message.get_body(preferencelist=("plain", "html")) if hasattr(message, "get_body") else None
    if part is None:
        payload = message.get_payload(decode=True) if not message.is_multipart() else b""
        return (payload or b"").decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload or "")
    try:
        text = part.get_content()
    except (LookupError, KeyError, ValueError):
        text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
    return str(text)


def _split_answers(text: str, ids: list[str]) -> dict[str, str]:
    """A digest reply: each answer's lines start with its id. One id takes the whole text."""
    parts: dict[str, list[str]] = {}
    current = None
    loose: list[str] = []
    for line in text.split("\n"):
        found = QID.match(line.strip())
        if found:
            current = norm(found.group(0))
            rest = line.strip()[found.end():].lstrip(" :-—–\t")
            parts.setdefault(current, [])
            if rest:
                parts[current].append(rest)
            continue
        if current is None:
            loose.append(line)
        else:
            parts[current].append(line)
    answers = {qid: "\n".join(lines).strip() for qid, lines in parts.items()}
    if not answers and len(ids) == 1:
        answers[ids[0]] = "\n".join(loose).strip()
    return answers


def _known_id(value: str) -> str | None:
    record = get(value)
    return record["qid"] if record else None


def accept_answer(payload: dict, board=None, *, source: str = "email") -> dict:
    """A reply from the mailbox or the mail webhook. Checks the sender, strips the quote, and passes each answer on."""
    sender = parseaddr(str(payload.get("from") or payload.get("sender") or ""))[1].strip().lower()
    if source != "desk" and sender not in allowed_senders():
        return {"ok": False, "reason": f"{sender or 'An unknown sender'} is not allowed to answer the desk's questions.", "answered": []}
    if source != "desk" and not authenticated(payload.get("authentication_results"), sender):
        return {"ok": False, "reason": "The reply did not pass DKIM, SPF or DMARC.", "answered": []}
    text = strip_quoted(str(payload.get("text") or payload.get("body") or ""))
    subject = str(payload.get("subject") or "")
    ids: list[str] = []
    for found in QID.findall(subject) + QID.findall(text):
        qid = _known_id(found)
        if qid and qid not in ids:
            ids.append(qid)
    refs = " ".join(str(payload.get(key) or "") for key in ("in_reply_to", "references"))
    if refs.strip():
        for record in all_questions():
            for message_id in [record.get("message_id"), *(record.get("message_ids") or [])]:
                if message_id and message_id in refs and record["qid"] not in ids:
                    ids.append(record["qid"])
    if not ids:
        return {"ok": False, "reason": "The reply names no open question.", "answered": []}
    answers = _split_answers(text, ids)
    done = []
    for qid, answer_text in answers.items():
        record = get(qid)
        if record is None or not answer_text:
            continue
        answer(qid, answer_text, sender or source, board=board)
        done.append(record["qid"])
    return {"ok": bool(done), "reason": "" if done else "The reply had no answer text.", "answered": done}


def poll_inbox(board=None) -> int:
    """Read the mailbox once. Returns how many replies answered a question."""
    from futuresfund.mailer import mail_blocked, test_run

    board = board if board is not None else _state["board"]
    if imaplib.IMAP4_SSL is _REAL_IMAP and (test_run() or mail_blocked()):
        # A test run never opens the real mailbox. Tests replace imaplib.IMAP4_SSL.
        return 0
    host, port, user, password, folder = _imap_settings()
    if not user or not str(password).strip():
        return 0
    state = _inbox_state()
    processed = set(state.get("processed") or [])
    answered = 0
    try:
        client = imaplib.IMAP4_SSL(host, port)
    except (OSError, imaplib.IMAP4.error) as exc:
        _login_trouble(board, f"The mailbox {host} could not be reached: {exc}")
        return 0
    try:
        try:
            client.login(user, password)
        except (imaplib.IMAP4.error, OSError) as exc:
            _login_trouble(board, f"{host} did not accept the login for {user}: {exc}. Turn IMAP on in Gmail, or set IMAP_PASSWORD.")
            return 0
        _state["login_posted"] = False
        client.select(folder)
        uids: list[bytes] = []
        status, data = client.uid("SEARCH", None, '(UNSEEN SUBJECT "FFQ-")')
        if status == "OK" and data:
            uids.extend(part for part in (data[0] or b"").split() if part)
        for record in open_questions():
            message_id = record.get("message_id")
            if not message_id:
                continue
            status, data = client.uid("SEARCH", None, f'(UNSEEN HEADER In-Reply-To "{message_id}")')
            if status == "OK" and data:
                uids.extend(part for part in (data[0] or b"").split() if part and part not in uids)
        for uid in uids:
            status, data = client.uid("FETCH", uid, "(RFC822)")
            raw = b""
            for item in data or []:
                if isinstance(item, tuple) and len(item) > 1:
                    raw = item[1]
                    break
            if status != "OK" or not raw:
                continue
            message = email.message_from_bytes(raw, policy=policy.default)
            message_id = str(message.get("Message-ID") or f"uid-{uid.decode(errors='replace')}")
            if message_id not in processed:
                result = accept_answer({
                    "from": str(message.get("From") or ""),
                    "subject": str(message.get("Subject") or ""),
                    "text": _plain_text(message),
                    "in_reply_to": str(message.get("In-Reply-To") or ""),
                    "references": str(message.get("References") or ""),
                    "authentication_results": message.get_all("Authentication-Results") or [],
                }, board)
                if result.get("answered"):
                    answered += len(result["answered"])
                elif board is not None:
                    board.post(PM, f"A reply about the desk's questions was not used. {result.get('reason')}", kind="log", channel=PM)
                processed.add(message_id)
            client.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        state["processed"] = sorted(processed)
        _save_inbox_state(state)
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return answered


def _login_trouble(board, text: str) -> None:
    """Told once to the Systems Administrator. The reader tries again at its next turn."""
    if _state["login_posted"]:
        return
    _state["login_posted"] = True
    if board is not None:
        board.post(ADMIN, f"The Portfolio Manager cannot read answers to the desk's questions. {text} It tries again later.", kind="report", channel=ADMIN)


# ---------- answers ----------

def answer(qid: str, text: str, sender: str = "desk", *, board=None, run_now: bool = False) -> dict:
    """An answer came in. The Portfolio Manager passes it to the researcher who asked, who applies it."""
    board = board if board is not None else _state["board"]
    with _lock:
        record = get(qid)
        if record is None:
            return {"ok": False, "reason": f"{qid} is not a question the desk asked."}
        if record.get("status") not in {"open", "answered"}:
            return {"ok": False, "reason": f"{record['qid']} was already {record.get('status')}."}
        record["answer_text"] = str(text or "").strip()[:60000]
        record["answer_from"] = sender
        record["answered_at"] = _now()
        record["status"] = "answered"
        record["answer_parsed"] = summary(parse_answer(record, record["answer_text"]))
        _save(record)
    author = record.get("asked_by") or "Chart researcher"
    if board is not None:
        board.post(PM, f"{record['qid']} answered — passing it to the {author}.", kind="report", channel=PM)
    if run_now or board is None:
        return apply_answer(record["qid"], board)
    from futuresfund import learn

    learn.researcher_job(f"answer|{record['qid']}", lambda: apply_answer(record["qid"], board), urgent=True)
    return {"ok": True, "qid": record["qid"], "status": "answered", "reason": f"Passed to the {author}."}


def summary(parsed: dict) -> dict:
    keep = {key: value for key, value in parsed.items() if key != "script"}
    if parsed.get("script"):
        keep["script_lines"] = len(str(parsed["script"]).splitlines())
    return keep


_PARAM = re.compile(r"^\s*(?:SETTING:\s*)?([A-Za-z_]\w*)\s*[=:]\s*(.+?)\s*$", re.IGNORECASE)
_PARK = re.compile(r"^\s*(park)\b(?:\s+it\b)?[\s.:,-]*(.*)$", re.IGNORECASE | re.DOTALL)
_RETRY = re.compile(r"^\s*(retry|try again|re-?run|again)\b", re.IGNORECASE)
_KEEP = re.compile(r"^\s*(keep|go on|carry on|continue)\b", re.IGNORECASE)
_NUMBER = re.compile(r"^\s*(?:option\s*)?#?(\d{1,2})\s*[.)]?\s*$", re.IGNORECASE)


def _pasted_script(text: str) -> str | None:
    at = text.find("//@version")
    if at < 0:
        return None
    script = text[at:].strip()
    return script if re.search(r"^\s*strategy\s*\(", script, re.MULTILINE) else None


def parse_answer(record: dict, text: str) -> dict:
    """The deterministic reading of an answer. Free text is left for the researcher's model."""
    body = strip_quoted(text)
    if not body:
        return {"action": "unclear"}
    script = _pasted_script(body)
    if script:
        return {"action": "script", "script": script}
    lines = [line.strip() for line in body.split("\n") if line.strip()]
    first = lines[0]
    number = _NUMBER.match(first)
    if number:
        chosen = next((row for row in record.get("options") or [] if int(row.get("n") or 0) == int(number.group(1))), None)
        if chosen is None:
            return {"action": "unclear", "text": body}
        picked = {"action": chosen["action"], "option": int(chosen["n"])}
        if chosen.get("params"):
            picked["raw"] = {key: str(value) for key, value in chosen["params"].items()}
            picked["action"] = "params"
        return picked
    parked = _PARK.match(first)
    if parked:
        rest = " ".join([(parked.group(2) or "").strip(), *lines[1:]]).strip()
        return {"action": "park", "reason": rest}
    if _RETRY.match(first) and len(first.split()) <= 3:
        return {"action": "retry"}
    if _KEEP.match(first) and len(first.split()) <= 3:
        return {"action": "keep"}
    raw: dict[str, str] = {}
    for line in lines:
        found = _PARAM.match(line)
        if found and " " not in found.group(1):
            raw[found.group(1)] = found.group(2).strip().strip("\"'").rstrip(".,;")
    if raw and len(raw) == len(lines):
        return {"action": "params", "raw": raw}
    if raw:
        return {"action": "params", "raw": raw, "text": body}
    return {"action": "free", "text": body}


def _locate(record: dict) -> Path | None:
    """Where the script is now: the awaiting folder, its origin folder, or a library folder."""
    names = [record.get("held_name"), record.get("file")]
    places = [awaiting_dir()]
    if record.get("origin_folder"):
        places.append(Path(record["origin_folder"]))
    for folder in places:
        for name in names:
            if name and (folder / name).is_file():
                return folder / name
    return None


def _script_inputs(text: str) -> tuple[dict, dict, dict]:
    from futuresfund import learn

    facts = learn.pine_facts(text or "")
    return learn._pine_params(facts), learn._input_bounds(facts), learn._input_choices(facts)


def validate(record: dict, raw: dict) -> tuple[dict, list[str]]:
    """The owner's values that fit the script's inputs, and a sentence for each one that does not.

    A desk_ trade limit is allowed on any script: the desk installs its trade-limit block when it is used.
    """
    from futuresfund.attempt_reason import _typed
    from futuresfund.learn import FROZEN

    located = _locate(record)
    text = ""
    if located is not None:
        try:
            text = located.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if any(str(name).startswith("desk_") for name in raw):
        from futuresfund.strategy_team import ensure_trade_limits

        try:
            text = ensure_trade_limits(text) if text else text
        except Exception:
            traceback.print_exc()
    current, bounds, choices = _script_inputs(text)
    from futuresfund.pine_report import SESSION_VALUE, text_inputs

    # A session or a plain text input is not searched, so it is not among the tunable inputs above.
    text_named = text_inputs(text)
    params: dict = {}
    rejected: list[str] = []
    for name, value in raw.items():
        if name in FROZEN or name.startswith("_"):
            rejected.append(f"{name} is fixed by the desk")
            continue
        if name not in current and name in text_named:
            item = text_named[name]
            shown = str(value).strip().strip("\"'")
            if item["kind"] == "session" and not SESSION_VALUE.match(shown):
                rejected.append(f"{name} = {value} is not a session like 0930-1600")
                continue
            if not shown:
                rejected.append(f"{name} cannot be empty")
                continue
            params[name] = shown
            continue
        if name not in current:
            if name.startswith("desk_"):
                try:
                    number = float(value)
                except ValueError:
                    rejected.append(f"{name} = {value} is not a number")
                    continue
                if number < 0:
                    rejected.append(f"{name} cannot be below 0")
                    continue
                params[name] = int(number) if number == int(number) else number
                continue
            rejected.append(f"the script has no input named {name}")
            continue
        typed = _typed(current[name], str(value), choices.get(name) or [])
        if typed is None:
            options = ", ".join(choices.get(name) or [])
            rejected.append(f"{name} = {value} does not fit that input" + (f" (choices: {options})" if options else ""))
            continue
        low, high = bounds.get(name, (None, None))
        if isinstance(typed, (int, float)) and not isinstance(typed, bool):
            if low is not None and typed < low:
                rejected.append(f"{name} = {value} is below its minimum {low}")
                continue
            if high is not None and typed > high:
                rejected.append(f"{name} = {value} is above its maximum {high}")
                continue
        params[name] = typed
    return params, rejected


def _researcher_reads(record: dict, text: str) -> dict:
    """The researcher's model turns a free-text answer into SETTING lines, PARK, RETRY or KEEP."""
    located = _locate(record)
    script = ""
    if located is not None:
        try:
            script = located.read_text(encoding="utf-8", errors="replace")
        except OSError:
            script = ""
    current, bounds, _choices = _script_inputs(script)
    inputs = "; ".join(
        f"{name} = {value}" + (f" ({bounds[name][0]} to {bounds[name][1]})" if name in bounds else "")
        for name, value in list(current.items())[:60]
    )
    reply = ""
    try:
        reply = _model_reply(record.get("asked_by") or "Chart researcher", (
            "You are the chart researcher. The owner answered your question about a script in their own words. "
            "Turn the answer into lines the desk can apply, nothing else: one SETTING: name = value line per input the "
            "owner wants changed (only inputs listed below, or desk_daily_loss, desk_daily_profit, desk_max_trades_day, "
            "desk_cooldown_bars), or a single line PARK, RETRY or KEEP. If the answer asks for none of these, reply UNCLEAR."
        ), "\n".join([
            f"Your question: {record['title']}. {record['reason']}",
            f"Inputs now: {inputs or 'none'}",
            f"The owner's answer: {text}",
        ]))
    except Exception:
        traceback.print_exc()
    lines = [line.strip() for line in str(reply or "").split("\n") if line.strip()]
    raw: dict[str, str] = {}
    for line in lines:
        upper = line.upper().strip(" .")
        if upper == "PARK":
            return {"action": "park", "reason": text, "read_by": "model"}
        if upper == "RETRY":
            return {"action": "retry", "read_by": "model"}
        if upper == "KEEP":
            return {"action": "keep", "read_by": "model"}
        found = re.match(r"^SETTING:\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$", line, re.IGNORECASE)
        if found:
            raw[found.group(1)] = found.group(2).strip().strip("\"'")
    if raw:
        return {"action": "params", "raw": raw, "read_by": "model"}
    return {"action": "unclear", "text": text}


def apply_answer(qid: str, board=None) -> dict:
    """The researcher who asked applies the owner's answer. Runs on the researcher's worker, never on a chart thread."""
    board = board if board is not None else _state["board"]
    record = get(qid)
    if record is None:
        return {"ok": False, "reason": f"{qid} is not a question the desk asked."}
    parsed = parse_answer(record, record.get("answer_text") or "")
    if parsed["action"] == "free":
        if record["kind"] in {"compile", "repair"}:
            # Words for a compile problem are the developer's hint for the next repair.
            parsed = {"action": "hint", "text": parsed["text"]}
        else:
            parsed = _researcher_reads(record, parsed["text"])
    return apply_parsed(record, board, parsed)


def apply_parsed(record: dict, board, parsed: dict) -> dict:
    """Carry out one parsed answer: params, a script, a hint, park, keep or retry."""
    if parsed["action"] == "keep" and record["kind"] != "repair":
        parsed = {"action": "retry"}
    action = parsed["action"]
    if action == "unclear":
        return _follow_up(record, board, "I could not tell what to change from your answer.")
    if action == "params":
        params, rejected = validate(record, parsed.get("raw") or {})
        if not params and parsed.get("text") and not parsed.get("read_by"):
            # Words around a name: value line. The researcher reads the whole answer instead.
            reread = _researcher_reads(record, parsed["text"])
            if reread["action"] not in {"params", "unclear"}:
                return apply_parsed(record, board, reread)
            if reread["action"] == "params":
                params, rejected = validate(record, reread.get("raw") or {})
        if not params:
            return _follow_up(record, board, "None of those values fit the script: " + "; ".join(rejected) + ".")
        return _apply_params(record, board, params, rejected)
    if action == "script":
        return _apply_script(record, board, parsed["script"])
    if action == "hint":
        return _apply_hint(record, board, parsed["text"])
    if action == "park":
        return _park(record, board, parsed.get("reason") or "")
    if action == "keep":
        return _finish(record, board, "applied", "The owner let the developer's own repair go on.")
    return _retry(record, board)


def _follow_up(record: dict, board, why: str) -> dict:
    """One follow-up email asking for a number or name = value. After that the question waits in the awaiting node."""
    author = record.get("asked_by") or "Chart researcher"
    with _lock:
        record = get(record["qid"]) or record
        record["status"] = "open"
        if int(record.get("followups") or 0) < 1:
            record["followups"] = int(record.get("followups") or 0) + 1
            record["followup_pending"] = (
                f"{why} Please reply with a number from the options, or lines like stop_dollars = 300, or park, or retry."
            )
            sent = "I asked the Portfolio Manager to send one follow-up."
        else:
            sent = "It waits for an answer in the awaiting node on the Research workflow page."
        record["result"] = why
        _save(record)
    if board is not None:
        board.post(author, f"{record['qid']} ({record['file']}): {why} {sent}", kind="report", channel=author)
    return {"ok": False, "qid": record["qid"], "status": "open", "reason": why}


def _store_owner_params(file: str, params: dict, qid: str) -> None:
    """Owner values are kept in the script's notes (owner_answers) and applied at the start of every chart's search."""
    from futuresfund import learn

    folder = Path(learn.NOTES)
    if _protected(folder):
        _owner_memory.setdefault(file, {}).update(params)
        return
    path = Path(file)
    with learn._notes_lock:
        notes = learn._read_notes(path)
        kept = notes.get("owner_answers") if isinstance(notes.get("owner_answers"), dict) else {}
        merged = {**dict(kept.get("params") or {}), **params}
        notes["owner_answers"] = {
            "params": merged,
            "questions": sorted({*(kept.get("questions") or []), qid}),
            "updated": _now(),
        }
        if not notes.get("file"):
            notes["file"] = path.name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{path.stem.replace(' ', '_')}.json").write_text(learn._notes_json(notes), encoding="utf-8")


_owner_memory: dict[str, dict] = {}


def owner_settings(path) -> dict:
    """The owner's values for this script, applied after prepare_research on every chart."""
    from futuresfund import learn

    path = Path(path)
    if _protected(Path(learn.NOTES)):
        return dict(_owner_memory.get(path.name) or {})
    notes = learn._read_notes(path)
    kept = notes.get("owner_answers") if isinstance(notes.get("owner_answers"), dict) else {}
    params = kept.get("params") if isinstance(kept.get("params"), dict) else {}
    return dict(params)


def _apply_params(record: dict, board, params: dict, rejected: list[str]) -> dict:
    author = record.get("asked_by") or "Chart researcher"
    _store_owner_params(record["file"], params, record["qid"])
    shown = ", ".join(f"{name} = {value}" for name, value in params.items())
    note = f" Not used: {'; '.join(rejected)}." if rejected else ""
    path = _resume(record, board)
    if not record.get("blocking"):
        _reopen(path or Path(record.get("origin_folder") or ".") / record["file"], board, author)
    text = f"From the owner's answer {record['qid']} I set {shown} on {record['file']}. The chart developer reruns its backtests with these values.{note}"
    return _finish(record, board, "applied", text, extra={"params": params, "rejected": rejected})


def _apply_script(record: dict, board, script: str) -> dict:
    from futuresfund.attempt_reason import keep_original

    author = record.get("asked_by") or "Chart researcher"
    path = _resume(record, board) or _locate(record)
    if path is None:
        return _finish(record, board, "applied", f"The owner's script for {record['file']} came in, but the file is no longer on the desk.")
    try:
        keep_original(path)
        path.write_text(script.rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        return _follow_up(record, board, f"The pasted script could not be saved: {exc}.")
    _reopen(path, board, author)
    _close_siblings(record, board)
    return _finish(record, board, "applied", f"I installed the owner's script from {record['qid']} in {path.name}. The original is kept in researchNotes/script_originals. The chart developer reruns its backtests.")


def _apply_hint(record: dict, board, text: str) -> dict:
    author = record.get("asked_by") or "Chart researcher"
    with _lock:
        _hints.setdefault(record["file"], []).append(text.strip()[:3000])
    if record.get("blocking"):
        _resume(record, board)
    return _finish(record, board, "applied", f"I passed the owner's note from {record['qid']} to the developer for the next repair of {record['file']}.")


def repair_hint(path) -> str:
    """The owner's notes for the developer's next compile repair of this script. Read once."""
    name = Path(path).name
    with _lock:
        notes = _hints.pop(name, [])
    if not notes:
        return ""
    return "The owner says: " + " ".join(notes)


def _retry(record: dict, board) -> dict:
    path = _resume(record, board)
    where = f"back in {path.parent.name}" if path is not None else "back in the queue"
    _close_siblings(record, board)
    return _finish(record, board, "applied", f"From the owner's answer {record['qid']}, {record['file']} is {where} as it is. The chart developer runs it again.")


def _park(record: dict, board, reason: str) -> dict:
    from futuresfund import learn

    located = _locate(record)
    why = f"The owner parked it ({record['qid']}). {reason}".strip()
    if located is not None and not _protected(Path(learn.CANNOT_RUN)):
        learn.set_aside(located, f"{why} {record['title']}: {record['reason']}")
        try:
            (awaiting_dir() / f"{located.stem}.question.txt").unlink()
        except OSError:
            pass
        learn.release_file(located)
    _close_siblings(record, board)
    return _finish(record, board, "parked", f"{record['file']} is parked in cannotRunStrategies, as the owner answered in {record['qid']}.")


def _close_siblings(record: dict, board) -> None:
    """Other open questions on the same file are settled by this answer."""
    with _lock:
        for other in open_questions():
            if other["qid"] != record["qid"] and other.get("file") == record.get("file"):
                other["status"] = "applied"
                other["applied_at"] = _now()
                other["result"] = f"Settled by the answer to {record['qid']}."
                _save(other)


def _resume(record: dict, board) -> Path | None:
    """Move the script back from the awaiting folder to the folder it came from, and let every chart take it again."""
    from futuresfund import learn

    folder = awaiting_dir()
    origin = Path(record.get("origin_folder") or learn.LEARNING)
    path = None
    held = folder / (record.get("held_name") or record["file"])
    if held.is_file() and not _protected(origin):
        origin.mkdir(parents=True, exist_ok=True)
        target = origin / record["file"]
        if target.exists():
            target = origin / held.name
        if target.exists():
            # The owner dropped a newer copy with the same name into the folder meanwhile. That copy is kept;
            # the held one is put aside in awaitingAnswerStrategies/replaced instead of being moved over it.
            aside = folder / "replaced"
            aside.mkdir(parents=True, exist_ok=True)
            kept = aside / held.name
            if kept.exists():
                kept = aside / f"{held.stem}_{int(time.time())}{held.suffix}"
            shutil.move(str(held), str(kept))
        else:
            shutil.move(str(held), str(target))
        path = target
        twin = record.get("twin")
        if twin and (folder / twin).is_file() and not (origin / twin).exists():
            shutil.move(str(folder / twin), str(origin / twin))
        try:
            (folder / f"{held.stem}.question.txt").unlink()
        except OSError:
            pass
    elif (origin / record["file"]).is_file():
        path = origin / record["file"]
    learn.release_file(path or origin / record["file"])
    return path


def _reopen(path: Path, board, author: str) -> None:
    """A finished chart never opens a script again. The owner's answer runs its stages again (old notes are archived)."""
    from futuresfund import learn

    if path is None or _protected(Path(learn.NOTES)):
        return
    try:
        learn._reopen_for_owner(board, path, author)
    except Exception:
        traceback.print_exc()


def _finish(record: dict, board, status: str, text: str, extra: dict | None = None) -> dict:
    author = record.get("asked_by") or "Chart researcher"
    with _lock:
        record = get(record["qid"]) or record
        record["status"] = status
        record["applied_at"] = _now()
        record["result"] = text
        record["followup_pending"] = ""
        if extra:
            record["applied"] = extra
        _save(record)
    if board is not None:
        board.post(author, text, kind="report", channel=author)
        board.post(PM, f"{record['qid']} is {status}: {text}", kind="log", channel=PM)
    return {"ok": True, "qid": record["qid"], "status": status, "reason": text}


# ---------- the Portfolio Manager's threads ----------

def start(board) -> bool:
    """Turn question mail on for this desk. A test run or FUTURES_MAIL_OFF never starts the mail threads."""
    from futuresfund.mailer import mail_blocked, test_run

    _state["board"] = board
    if mail_blocked() or test_run() or not EMAIL_QUESTIONS:
        _state["mail_on"] = False
        return False
    _state["mail_on"] = True
    alive = [thread for thread in _state["threads"] if thread.is_alive()]
    if alive:
        return True
    outbox = threading.Thread(target=_outbox_loop, daemon=True, name="portfolio-manager-outbox")
    inbox = threading.Thread(target=_inbox_loop, daemon=True, name="portfolio-manager-inbox")
    _state["threads"] = [outbox, inbox]
    outbox.start()
    inbox.start()
    return True


def _outbox_loop() -> None:
    from futuresfund.llm import use_agent

    while _state["mail_on"]:
        try:
            if time.time() - float(_state["last_sent"] or 0) >= SEND_GAP_SECONDS:
                with use_agent(PM):
                    send_pending(_state["board"])
        except Exception:
            traceback.print_exc()
        time.sleep(5)


def _inbox_loop() -> None:
    from futuresfund.llm import use_agent

    time.sleep(10)
    while _state["mail_on"]:
        try:
            if EMAIL_QUESTIONS and any(row.get("sent_at") for row in open_questions()):
                with use_agent(PM):
                    poll_inbox(_state["board"])
        except Exception:
            traceback.print_exc()
        time.sleep(max(15, int(INBOX_POLL_SECONDS or 120)))


def status_lines() -> list[str]:
    """Plain facts for the mail status: no secrets."""
    host, _, user, _, folder = _imap_settings()
    lines = []
    if inbox_configured():
        lines.append(f"The question inbox reader is configured: {user} on {host}, folder {folder}, every {INBOX_POLL_SECONDS} seconds.")
    else:
        lines.append("The question inbox reader is not configured: set IMAP_USER and IMAP_PASSWORD, or SMTP_USER and SMTP_PASSWORD.")
    waiting = open_questions()
    lines.append(f"Open questions for the owner: {len(waiting)}.")
    return lines
