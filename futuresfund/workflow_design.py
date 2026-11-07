"""The research settings behind the Research workflow Design page.

Each knob is one module value that research reads while it runs. The value written in the code is
the default. Only the values the owner changed are saved, next to the diagram layout, in
~/.futuresfund/research_workflow.json (config.WORKFLOW_DESIGN_PATH):

    {"knobs": {"stage_7_attempts": 120}, "layout": {"stage7": [80, 710]}, "updated": "2026-11-06 15:30:00"}

A key the page no longer has, such as the old shared "stage_attempts", is ignored when the file is
applied and dropped the next time the owner saves. Each stage now has its own count.

Saved values are applied with setattr in two places only: at the top of learn._run_learning, when
research starts, and when the owner saves or resets them on the page. They are never applied when a
module is imported or in learn.begin_research, so the tests always see the values in the code. The
defaults are read from the modules the first time this module needs them, before anything is applied.

The page also shows live counts (the strategy folders, the waiting list, the proving ledger) and the
recent change-impact rows read from the research notes. Inside the desk container every stat on the
bind mount costs about 7 ms, so each folder is listed once, and the notes are read at most once a minute.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path

_BOOL, _INT, _FLOAT = "bool", "int", "float"


def _knob(key: str, label: str, node: str, kind: str, module: str, attr: str, help_text: str, *,
          low: float | None = None, high: float | None = None, step: float | None = None,
          unit: str = "", days: int | None = None) -> dict:
    row = {
        "key": key,
        "label": label,
        "node": node,
        "nodes": node.split(","),
        "type": kind,
        "min": low,
        "max": high,
        "step": step if step is not None else (1 if kind == _INT else None),
        "unit": unit,
        "help": help_text,
        "module": f"futuresfund.{module}",
        "attr": attr,
    }
    if days is not None:
        row["days"] = int(days)
    return row


KNOBS: tuple[dict, ...] = (
    _knob(
        "use_alert_bars", "Add the TradingView alert bars", "bars", _BOOL, "learn", "USE_ALERT_BARS",
        "On: a study adds the TradingView alert candles that came in after the last Databento download, "
        "so the 7-day stage tests the latest week. Off: a study uses only the downloaded and exported bars.",
    ),
    _knob(
        "live_refresh", "Read the alert bars again every", "bars", _INT, "research_bars", "LIVE_REFRESH",
        "How often the alert candles after the download are read again. A study loads its bars once, when it starts, "
        "so a refresh never changes the bars under a search that is running.",
        low=60, high=3600, step=60, unit="seconds",
    ),
    _knob(
        "compile_repair_tries", "Compile repairs before parking", "compile", _INT, "learn", "COMPILE_REPAIR_TRIES",
        "A script that does not compile goes to its developer for a repair, and the chart researcher emails the error "
        "while the repair runs. After this many repairs that still do not compile, the researcher asks the owner and the "
        "script waits in awaitingAnswerStrategies. 0 asks at once. The count is kept in memory, so it starts again "
        "when the desk restarts.",
        low=0, high=10, unit="repairs",
    ),
    _knob(
        "read_before_testing", "Read the script before testing", "read", _BOOL, "learn", "READ_BEFORE_TESTING",
        "On: before the 7-day stage, the desk measures how the script responds to each input, leaves out the inputs "
        "that change nothing, and the researcher reads the script once. A researcher's answer that is waiting is "
        "used here too. Off: the stages start at once from the script's own settings, no read, repair or "
        "why-no-trades question is asked, and a waiting answer is not used. Because no chart would use an answer, "
        "finished charts are not run again for one either.",
    ),
    _knob(
        "response_sessions", "Sessions for the response test", "read", _INT, "script_diagnosis", "RESPONSE_SESSIONS",
        "How many recent trading sessions the response test runs on. A researcher's rewrite is checked on the same "
        "window before it is kept.",
        low=5, high=64, unit="sessions",
    ),
    _knob(
        "empty_charts", "Empty charts before asking why", "read", _INT, "learn", "EMPTY_CHARTS",
        "The researcher waits until the script has taken no trades on this many charts, then asks why its entry never passes.",
        low=1, high=21, unit="charts",
    ),
    _knob(
        "allow_repair_rewrites", "Let a repair answer rewrite the script", "read", _BOOL, "learn", "ALLOW_REPAIR_REWRITES",
        "On: when the researcher is asked to repair a stop, take profit or input that does not work, the answer may "
        "rewrite the script, even though research otherwise changes input values only. Off: a repair answer changes "
        "input values only, like every other answer.",
    ),
    _knob(
        "stage_7_on", "Run the 7-day stage", "stage7", _BOOL, "learn", "STAGES_OFF",
        "Off: the 7-day stage is skipped. The next stage starts from the same settings, and no ledger mark is written for it.",
        days=7,
    ),
    _knob(
        "stage_30_on", "Run the 30-day stage", "stage30", _BOOL, "learn", "STAGES_OFF",
        "Off: the 30-day stage is skipped. The next stage starts from the settings the stage before passed with, "
        "and no ledger mark is written for it.",
        days=30,
    ),
    _knob(
        "stage_90_on", "Run the 90-day stage", "stage90", _BOOL, "learn", "STAGES_OFF",
        "Off: the 90-day stage is skipped. The one-year search starts from the settings the stage before passed with, "
        "and no ledger mark is written for it.",
        days=90,
    ),
    _knob(
        "stage_7_attempts", "7-day stage attempts", "stage7", _INT, "learn", "STAGE_7_ATTEMPTS",
        "Different results the 7-day stage gets on one chart. A pass does not end the stage: it keeps searching for "
        "more profit with less drawdown until these are spent or the attempts without a better pass run out. A stage "
        "that never passes spends them all. Its engine runs, repeats included, are capped at twice this number.",
        low=1, high=1000, unit="results",
    ),
    _knob(
        "stage_30_attempts", "30-day stage attempts", "stage30", _INT, "learn", "STAGE_30_ATTEMPTS",
        "Different results the 30-day stage gets on one chart, starting from the best 7-day pass. It keeps searching "
        "after a pass, like the 7-day stage. Its engine runs are capped at twice this number.",
        low=1, high=1000, unit="results",
    ),
    _knob(
        "stage_90_attempts", "90-day stage attempts", "stage90", _INT, "learn", "STAGE_90_ATTEMPTS",
        "Different results the 90-day stage gets on one chart, starting from the best 30-day pass. Its best pass "
        "starts the one-year search. Its engine runs are capped at twice this number.",
        low=1, high=1000, unit="results",
    ),
    _knob(
        "stage_patience", "Attempts without a better pass", "stage7,stage30,stage90", _INT, "learn", "STAGE_PATIENCE",
        "After a stage passes, it stops when this many attempts in a row, repeats included, found no pass with more "
        "profit after drawdown. The best pass is the one with the most profit minus its largest drawdown, and on a tie "
        "the lower drawdown. It carries to the next stage and is saved on the proving ledger.",
        low=1, high=1000, unit="attempts",
    ),
    _knob(
        "trade_cut_attempts", "Trade cut attempts", "tradecut", _INT, "learn", "TRADE_CUT_ATTEMPTS",
        "A run with more than 900 trades, or on a stage a pace over 600 trades a year, starts the trade cut on that chart: "
        "the chart researcher turns on the script's own daily limits, doubles the cooldown, adds the desk daily loss and "
        "profit limits and most trades a day, one step per attempt. After this many cut attempts in a row still over the "
        "cap, the researcher asks the owner instead of parking the script.",
        low=1, high=50, unit="attempts",
    ),
    _knob(
        "stage_trades_per_week", "Extra floor of trades a week", "goal", _FLOAT, "objective", "STAGE_TRADES_PER_WEEK",
        "0 is off: the fewest trades come from the trade count on the strategy library page, pro-rated to each test "
        "(60 a year needs 2 on the 7-day test, 5 on 30 days and 15 on 90). Above 0, every test and the year also "
        "need this many trades for each five-session week, when that is more. It counts trades, not the days that traded.",
        low=0, high=10, step=0.5, unit="trades a week",
    ),
    _knob(
        "stage_review", "Review failed stages", "review", _BOOL, "learn", "STAGE_REVIEW_ON",
        "On: a script that fails a 7, 30 or 90-day stage, or the year, goes to the researcher, who looks for why it "
        "fails. The answer is used on the script's next chart. Off: no failed-stage review is asked.",
    ),
    _knob(
        "stage_reviews", "Failed-stage reviews per script", "review", _INT, "learn", "STAGE_REVIEWS",
        "The most failed-stage reviews the researcher does on one script, one for each stage and chart.",
        low=0, high=21, unit="reviews",
    ),
    _knob(
        "review_backlog", "Review backlog limit", "review", _INT, "learn", "REVIEW_BACKLOG",
        "A new failed-stage review is left out while this many stage questions wait on the one model. The next failed chart asks again.",
        low=1, high=50, unit="questions",
    ),
    _knob(
        "reopen_for_answers", "Run finished charts again for an answer", "review", _BOOL, "learn", "REOPEN_FOR_ANSWERS",
        "On: when the researcher answers with new settings or a rewrite, the script's finished charts run again, and "
        "their old results move to researchNotes/before_repair. Off: the answer waits for the next chart that opens "
        "the script, which uses it in its read before testing. Charts run again only while the read before testing "
        "is on, because that read is what uses the answer.",
    ),
    _knob(
        "reopen_limit", "Reopens per script", "review", _INT, "learn", "REOPEN_LIMIT",
        "The most times a finished script's charts run again for a researcher's answer.",
        low=0, high=10, unit="times",
    ),
    _knob(
        "attempt_limit", "Year search attempts", "year", _INT, "learn", "ATTEMPT_LIMIT",
        "The most different results the one-year search tries on one chart, after the 7, 30 and 90-day stages pass. "
        "The search does not stop at 10 successful runs; it keeps going until this limit or another stop. The loss "
        "stop belongs to the year search whatever this is set to. Engine 4's drafts have their own count.",
        low=10, high=1000, unit="results",
    ),
    _knob(
        "loss_look", "Leave a losing script after", "year", _INT, "learn", "LOSS_LOOK",
        "A chart engine leaves the script when its best one-year result is still a loss after this many different results.",
        low=5, high=400, unit="results",
    ),
    _knob(
        "impact_percent", "Profit drop that ends a climb", "year", _FLOAT, "learn", "IMPACT_PERCENT",
        "When moving one input lowers the profit by this share or more (0.05 is 5 percent), the search stops moving that input that way.",
        low=0.01, high=0.5, step=0.01, unit="share of profit",
    ),
    _knob(
        "creation_attempts", "Draft search attempts", "engine4", _INT, "strategy_team", "CREATION_ATTEMPTS",
        "The most different results engine 4 tries on one new draft, on the last year of the chart it was written for. "
        "It stops sooner at the first accepted run. This is its own number, so a longer year search on the charts "
        "does not make each draft take longer.",
        low=10, high=1000, unit="results",
    ),
    _knob(
        "flat_runs", "No-trade runs before asking the owner", "zero", _INT, "learn", "FLAT_RUNS",
        "After this many one-year runs in a row with no trades and no profit, the chart researcher asks the owner by email "
        "and the script waits in awaitingAnswerStrategies, unless it traded in this search or passed a stage. The same "
        "number ends a run of no-trade repeats in any search.",
        low=3, high=100, unit="runs",
    ),
    _knob(
        "recheck_before_filing", "Recheck the last 7, 30 and 90 days before filing", "recheck", _BOOL, "learn",
        "RECHECK_BEFORE_FILING",
        "On: a one-year run that meets acceptance is run again on the last 7, 30 and 90 days at the same settings, "
        "and it goes on the Not tested on full dataset list only when every window passes. Off, as the code is "
        "written today: filing does not check the recent windows again.",
    ),
    _knob(
        "email_questions", "Email the researchers' questions", "awaiting", _BOOL, "questions", "EMAIL_QUESTIONS",
        "On: when a script does not compile after its repairs, gets no entry rule, stops in PineTS, never trades, or still "
        "takes too many trades after the trade cut, the chart researcher writes a question, the Portfolio Manager emails it, "
        "and the script waits in awaitingAnswerStrategies while the engine takes the next one. Off: those scripts are "
        "parked in cannotRunStrategies the old way and no question is sent.",
    ),
    _knob(
        "inbox_poll_seconds", "Read the mailbox for answers every", "awaiting", _INT, "questions", "INBOX_POLL_SECONDS",
        "How often the Portfolio Manager reads the mailbox for replies to the questions it emailed. Only replies from the "
        "report address (or QUESTION_FROM) that pass DKIM, SPF or DMARC are used.",
        low=30, high=3600, step=30, unit="seconds",
    ),
    _knob(
        "engines_at_once", "Backtests at once", "claim", _INT, "engine_workers", "ENGINES_AT_ONCE",
        "The 21 chart engines (three per instrument) and the creation engine share this many PineTS worker "
        "processes, so at most this many backtests run at the same time. An engine whose turn has not come shows "
        "\"Waiting for a free worker\". The code value is one for every core but one, and at least 2. A worker starts "
        "when a backtest needs one and keeps the bars of two charts. Applies from the next backtest.",
        low=1, high=64, unit="workers",
    ),
)
_BY_KEY = {knob["key"]: knob for knob in KNOBS}
_STAGE_KEYS = tuple(knob["key"] for knob in KNOBS if "days" in knob)

_LOCK = threading.RLock()
# The value each module attribute had before anything was applied, keyed "module.attr".
_RAW: dict[str, object] = {}


def _module(knob: dict):
    return importlib.import_module(knob["module"])


def _name(knob: dict) -> str:
    return f"{knob['module']}.{knob['attr']}"


def _capture() -> dict[str, object]:
    """Read the code values once, the first time they are needed, before any saved value is applied."""
    with _LOCK:
        for knob in KNOBS:
            name = _name(knob)
            if name not in _RAW:
                _RAW[name] = getattr(_module(knob), knob["attr"])
        return _RAW


def _raw_default(knob: dict):
    return _capture()[_name(knob)]


def _shown(knob: dict, raw):
    """A module value as the page shows it. A stage switch is on when its days are not in STAGES_OFF."""
    if "days" in knob:
        return int(knob["days"]) not in {int(day) for day in (raw or ())}
    if knob["type"] == _BOOL:
        return bool(raw)
    if knob["type"] == _INT:
        return int(raw)
    return float(raw)


def defaults() -> dict:
    """Each knob's code value."""
    return {knob["key"]: _shown(knob, _raw_default(knob)) for knob in KNOBS}


def _plain(number: float) -> str:
    return f"{number:g}"


def validate(key, value):
    """The value as the knob stores it. Raises ValueError for an unknown key, a wrong type, or a value out of range."""
    knob = _BY_KEY.get(str(key))
    if knob is None:
        raise ValueError(f"{key} is not a research setting on this page.")
    label = knob["label"]
    if knob["type"] == _BOOL:
        if not isinstance(value, bool):
            raise ValueError(f"{label} is on or off, so it takes true or false.")
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} needs a number.")
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise ValueError(f"{label} needs a number, not {value!r}.") from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ValueError(f"{label} needs a number.")
    if not math.isfinite(number):
        raise ValueError(f"{label} needs a number.")
    if knob["type"] == _INT and not number.is_integer():
        raise ValueError(f"{label} takes a whole number, not {_plain(number)}.")
    low, high = knob["min"], knob["max"]
    if (low is not None and number < low) or (high is not None and number > high):
        unit = f" {knob['unit']}" if knob["unit"] else ""
        raise ValueError(f"{label} must be from {_plain(low)} to {_plain(high)}{unit}. {_plain(number)} is outside that.")
    if knob["type"] == _INT:
        return int(number)
    return round(number, 6)


def _native(knob: dict, value, raw):
    """The value set on the module. A whole number stays a whole number where the code wrote one, so text reads the same."""
    if knob["type"] == _FLOAT and isinstance(raw, int) and not isinstance(raw, bool) and float(value).is_integer():
        return int(value)
    return value


def _path() -> Path:
    from futuresfund import config

    return Path(config.WORKFLOW_DESIGN_PATH)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load() -> dict:
    """The saved file as {knobs, layout, updated}. A missing or unreadable file is an empty one."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    knobs = data.get("knobs")
    updated = data.get("updated")
    return {
        "knobs": dict(knobs) if isinstance(knobs, dict) else {},
        "layout": _clean_layout(data.get("layout"), strict=False),
        "updated": str(updated) if updated else None,
    }


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            # Windows refuses the replace while another process has the file open for a moment.
            if attempt == 4:
                raise
            time.sleep(0.05)


def _overrides(data: dict) -> dict:
    """The saved values that are valid and differ from the code value. Anything else in the file is ignored."""
    shown = defaults()
    found = {}
    for key, value in (data.get("knobs") or {}).items():
        try:
            clean = validate(key, value)
        except ValueError:
            continue
        if clean != shown[str(key)]:
            found[str(key)] = clean
    return found


def _apply(keys, overrides: dict) -> None:
    """setattr each knob in keys: the saved value, or the code value when nothing is saved."""
    stages = False
    for key in keys:
        knob = _BY_KEY[key]
        if "days" in knob:
            stages = True
            continue
        raw = _raw_default(knob)
        value = _native(knob, overrides[key], raw) if key in overrides else raw
        setattr(_module(knob), knob["attr"], value)
    if stages:
        first = _BY_KEY[_STAGE_KEYS[0]]
        raw = _raw_default(first)
        if any(key in overrides for key in _STAGE_KEYS):
            listed = {int(_BY_KEY[key]["days"]) for key in _STAGE_KEYS}
            # A day the code turned off that has no switch here stays off.
            off = {int(day) for day in (raw or ())} - listed
            for key in _STAGE_KEYS:
                knob = _BY_KEY[key]
                if not overrides.get(key, _shown(knob, raw)):
                    off.add(int(knob["days"]))
            value = frozenset(off)
        else:
            value = raw
        setattr(_module(first), first["attr"], value)


def apply_saved() -> dict:
    """Apply the saved file to the modules. Returns the values that differ from the code. Research calls this when it starts."""
    with _LOCK:
        _capture()
        overrides = _overrides(load())
        _apply([knob["key"] for knob in KNOBS], overrides)
        return overrides


def describe(changed: dict) -> str:
    """One plain line naming the settings that differ from the code values."""
    shown = defaults()
    parts = []
    for knob in KNOBS:
        key = knob["key"]
        if key not in changed:
            continue
        parts.append(f"{knob['label']}: {_said(knob, changed[key])} (the code value is {_said(knob, shown[key])})")
    if not parts:
        return "Research starts with the settings written in the code."
    return "Research starts with settings changed on the Research workflow Design page. " + "; ".join(parts) + "."


def _said(knob: dict, value) -> str:
    if knob["type"] == _BOOL:
        return "on" if value else "off"
    return _plain(float(value))


def knob_rows(overrides: dict | None = None) -> list[dict]:
    """Every knob with its value, its code value, whether it is changed, and the value research reads right now."""
    if overrides is None:
        overrides = _overrides(load())
    shown = defaults()
    rows = []
    for knob in KNOBS:
        key = knob["key"]
        try:
            current = _shown(knob, getattr(_module(knob), knob["attr"]))
        except Exception:
            current = None
        row = {name: value for name, value in knob.items()}
        row["nodes"] = list(knob["nodes"])
        row.update({
            "value": overrides.get(key, shown[key]),
            "default": shown[key],
            "changed": key in overrides,
            "current": current,
        })
        rows.append(row)
    return rows


def save_knobs(changes) -> dict:
    """Validate and save the owner's values, then apply them. Nothing is saved when any value is wrong."""
    if not isinstance(changes, dict):
        raise ValueError("The settings must be sent as names with their values.")
    with _LOCK:
        _capture()
        clean = {str(key): validate(key, value) for key, value in changes.items()}
        data = load()
        overrides = _overrides(data)
        shown = defaults()
        for key, value in clean.items():
            if value == shown[key]:
                overrides.pop(key, None)
            else:
                overrides[key] = value
        data["knobs"] = overrides
        data["updated"] = _now()
        _write(data)
        _apply(list(clean), overrides)
    return state()


def reset(keys=None) -> dict:
    """Remove these keys from the file, or every key when the list is empty, and put the code values back."""
    if keys is None:
        keys = []
    if not isinstance(keys, (list, tuple)):
        raise ValueError("Send the names of the settings to reset, or an empty list for all of them.")
    names = [str(key) for key in keys]
    unknown = [name for name in names if name not in _BY_KEY]
    if unknown:
        raise ValueError(f"{', '.join(unknown)} is not a research setting on this page.")
    chosen = names or [knob["key"] for knob in KNOBS]
    with _LOCK:
        _capture()
        data = load()
        overrides = _overrides(data)
        for key in chosen:
            overrides.pop(key, None)
        data["knobs"] = overrides
        data["updated"] = _now()
        _write(data)
        _apply(chosen, overrides)
    return state()


_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LAYOUT_MAX = 200
_PLACE_MAX = 100_000.0


def _tidy(number: float):
    return int(number) if float(number).is_integer() else round(float(number), 1)


def _clean_layout(layout, strict: bool = True) -> dict:
    """{node: [x, y]}. With strict, a wrong entry raises ValueError; without it, the entry is left out."""
    if layout is None:
        return {}
    if not isinstance(layout, dict):
        if strict:
            raise ValueError("The layout must give each node its place as [x, y].")
        return {}
    if strict and len(layout) > _LAYOUT_MAX:
        raise ValueError(f"The layout has {len(layout)} nodes. The diagram keeps at most {_LAYOUT_MAX}.")
    clean = {}
    for node, point in layout.items():
        try:
            if not isinstance(node, str) or not _NODE_ID.match(node):
                raise ValueError(f"{node!r} is not a node name.")
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in point)
            ):
                raise ValueError(f"The place of {node} must be two numbers, [x, y].")
            x, y = float(point[0]), float(point[1])
            if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > _PLACE_MAX or abs(y) > _PLACE_MAX:
                raise ValueError(f"The place of {node} is off the diagram.")
        except ValueError:
            if strict:
                raise
            continue
        clean[node] = [_tidy(x), _tidy(y)]
    return clean


def save_layout(layout) -> dict:
    """Save the diagram's node places. An empty layout clears them, and the catalog's places are used again."""
    clean = _clean_layout(layout or {}, strict=True)
    with _LOCK:
        data = load()
        data["layout"] = clean
        data["updated"] = _now()
        _write(data)
    return {"layout": clean}


def queue_first() -> list[str]:
    """The names studied first, from researchNotes/queue_first.json."""
    from futuresfund import learn

    return learn._queue_first()


_QUEUE_MAX = 500


def save_queue(names) -> dict:
    """Write researchNotes/queue_first.json. The queue reads it again at its next scan, with no restart."""
    if not isinstance(names, (list, tuple)):
        raise ValueError("The queue must be a list of script file names.")
    clean: list[str] = []
    for name in names:
        if not isinstance(name, str):
            raise ValueError("Each queue entry must be a script file name.")
        text = name.strip()
        if not text:
            continue
        if "/" in text or "\\" in text or "\x00" in text or text in {".", ".."} or len(text) > 255:
            raise ValueError(f"{text!r} is not a script file name.")
        if text not in clean:
            clean.append(text)
    if len(clean) > _QUEUE_MAX:
        raise ValueError(f"The queue can name at most {_QUEUE_MAX} scripts.")
    from futuresfund import learn

    path = Path(learn.NOTES) / "queue_first.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(temp, path)
    learn._QUEUE_FIRST.clear()
    with _FILES_LOCK:
        _LEARNING_FILES.clear()
    return {"queue_first": clean}


_FILES_LOCK = threading.Lock()
_LEARNING_FILES: dict = {}
_LEARNING_SECONDS = 300.0


def learning_files() -> list[str]:
    """The scripts in learningStrategies in the order the engines take them.

    The order is the queue's own (learn.queue). It is worked out again when the folder or
    queue_first.json changes, and at least every five minutes, because it reads every file.
    """
    from futuresfund import learn

    folder = Path(learn.LEARNING)
    try:
        stamp = os.stat(folder).st_mtime_ns
    except OSError:
        return []
    key = (str(folder), stamp, tuple(learn._queue_first()))
    now = time.monotonic()
    with _FILES_LOCK:
        if _LEARNING_FILES.get("key") == key and now - float(_LEARNING_FILES.get("at") or 0.0) < _LEARNING_SECONDS:
            return list(_LEARNING_FILES["names"])
    names = [path.name for path in learn.queue(folder)]
    with _FILES_LOCK:
        _LEARNING_FILES.update({"key": key, "at": now, "names": names})
    return list(names)


_FOLDERS = (
    ("created", "CREATED"),
    ("learned", "LEARNED"),
    ("cannot_run", "CANNOT_RUN"),
    ("awaiting", "AWAITING"),
    ("drafts", "DRAFTS"),
    ("unproven", "UNPROVEN"),
)


def _scripts_in(folder) -> int:
    """Pine scripts in one folder, from one listing. The .reason.txt and .question.txt notes beside them do not count."""
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    count = 0
    for name in names:
        lower = name.lower()
        if lower.endswith((".pine", ".txt")) and not lower.endswith((".reason.txt", ".question.txt")):
            count += 1
    return count


def _empty_ledger() -> dict:
    return {str(days): {"testing": 0, "passed": 0, "failed": 0, "stopped": 0} for days in (7, 30, 90, 365)}


def live() -> dict:
    """The counts the diagram shows on its nodes."""
    from futuresfund import learn

    folders = {"learning": len(learning_files())}
    for name, attr in _FOLDERS:
        folder = getattr(learn, attr)
        if name == "awaiting":
            from futuresfund.questions import awaiting_dir

            # The awaiting folder sits next to cannotRunStrategies (questions.awaiting_dir).
            folder = awaiting_dir()
        folders[name] = _scripts_in(folder)
    questions = []
    try:
        from futuresfund.questions import live_rows

        questions = live_rows()
    except Exception:
        pass
    waiting = shelf = 0
    try:
        from futuresfund import library

        waiting = len(library._load_untested() or [])
        shelf = len(library._load_index_rows())
    except Exception:
        pass
    ledger = _empty_ledger()
    testing = []
    try:
        from futuresfund import proving

        ledger = {str(days): {"testing": 0, "passed": 0, "failed": 0, "stopped": 0} for days in proving.STAGES}
        for row in proving.rows():
            stage = str(int(row.get("stage") or 0))
            status = str(row.get("status") or "")
            passed = row.get("passed") if isinstance(row.get("passed"), dict) else {}
            for days in passed:
                if str(days) in ledger:
                    ledger[str(days)]["passed"] += 1
            bucket = ledger.get(stage)
            if bucket is None:
                continue
            if status in {"testing", "failed", "stopped"}:
                bucket[status] += 1
            elif status == "passed" and stage not in {str(days) for days in passed}:
                bucket["passed"] += 1
            if status == "testing":
                testing.append({"file": row.get("file"), "root": row.get("root"), "timeframe": row.get("timeframe"), "stage": int(stage)})
    except Exception:
        pass
    return {"folders": folders, "waiting": waiting, "shelf": shelf, "ledger": ledger, "testing": testing, "questions": questions}


def _quietly(work, fallback):
    try:
        return work()
    except Exception:
        return fallback


def state() -> dict:
    """What GET /api/workflow-design returns."""
    with _LOCK:
        _capture()
        data = load()
        overrides = _overrides(data)
    return {
        "knobs": knob_rows(overrides),
        "queue_first": _quietly(queue_first, []),
        "learning_files": _quietly(learning_files, []),
        "layout": data["layout"],
        "updated": data["updated"],
        "live": _quietly(live, {
            "folders": {name: 0 for name in ("learning", "created", "learned", "cannot_run", "awaiting", "drafts", "unproven")},
            "waiting": 0,
            "shelf": 0,
            "ledger": _empty_ledger(),
            "testing": [],
            "questions": [],
        }),
    }


# Change impact: what each recent attempt did to the result, against the attempt before it on the same chart and stage.
CHANGE_SECONDS = 60.0
CHANGE_HOURS = 72
CHANGE_FILES = 30
_CHANGE_MAX = 500
_NOT_NOTES = {
    "chart_patterns.json",
    "creation_lessons.json",
    "creation_queue.json",
    "creation_sources.json",
    "errors.json",
    "indicator_library.json",
    "library_index.json",
    "named_pattern.json",
    "queue_first.json",
    "risk_suggestions.json",
    "strategy_cards.json",
    "untested_library.json",
}
_NOT_NOTES_START = ("reopened_for_", "unproven_created_")
_NOTES_SIZE_MAX = 8_000_000
_CHANGES: dict = {}
_CHANGES_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()
_VERDICT = {"closer": "toward", "farther": "away"}


def changes(limit: int = 80) -> dict:
    """The newest attempts in the research notes of the last 72 hours, with what each change did. Kept for a minute."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 80
    limit = max(1, min(limit, _CHANGE_MAX))
    with _BUILD_LOCK:
        now = time.time()
        with _CHANGES_LOCK:
            kept = _CHANGES.get(limit)
            if kept is not None and now - kept[0] < CHANGE_SECONDS:
                return kept[1]
        payload = _build_changes(limit, now)
        with _CHANGES_LOCK:
            _CHANGES[limit] = (now, payload)
        return payload


def _recent_notes(now: float) -> list[tuple[float, Path]]:
    """Script notes changed in the last CHANGE_HOURS, newest first, at most CHANGE_FILES. One stat each."""
    from futuresfund import learn

    found = []
    try:
        with os.scandir(Path(learn.NOTES)) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(".json") or name in _NOT_NOTES or name.startswith(_NOT_NOTES_START):
                    continue
                try:
                    info = entry.stat()
                except OSError:
                    continue
                if now - info.st_mtime > CHANGE_HOURS * 3600 or info.st_size > _NOTES_SIZE_MAX:
                    continue
                found.append((info.st_mtime, Path(entry.path)))
    except OSError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return found[:CHANGE_FILES]


def _charts(record) -> list[tuple[str, list]]:
    """(timeframe, trials) for one instrument. Older notes keep one trials list, split here by timeframe."""
    if not isinstance(record, dict):
        return []
    charts = record.get("charts")
    if isinstance(charts, dict) and charts:
        return [
            (str(timeframe), chart.get("trials"))
            for timeframe, chart in charts.items()
            if isinstance(chart, dict) and isinstance(chart.get("trials"), list)
        ]
    trials = record.get("trials")
    if not isinstance(trials, list):
        return []
    grouped: dict[str, list] = {}
    for row in trials:
        if isinstance(row, dict):
            grouped.setdefault(str(row.get("timeframe") or ""), []).append(row)
    return list(grouped.items())


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return round(after - before, 2)


def _whole(number: float | None) -> int | None:
    return int(round(number)) if number is not None else None


def _stage_of(row: dict) -> int:
    days = _number(row.get("stage_days"))
    return int(days) if days and days > 0 else 365


def _measured(row: dict | None) -> dict | None:
    """The row as the objective reads it. A row without its session count must not make objective load five years of bars."""
    if not isinstance(row, dict):
        return row
    days = _number(row.get("days"))
    if days and days > 0:
        return row
    return {**row, "timeframe": ""}


def _chart_rows(name: str, root: str, timeframe: str, trials: list, when: str, dated: bool = True) -> list[dict]:
    from futuresfund.objective import trades_per_week

    out = []
    previous: dict[int, dict] = {}
    counts: dict[int, int] = {}
    for seq, row in enumerate(trials):
        if not isinstance(row, dict):
            continue
        stage = _stage_of(row)
        counts[stage] = counts.get(stage, 0) + 1
        prev = previous.get(stage)
        profit = _number(row.get("net_profit"))
        drawdown = _number(row.get("max_drawdown"))
        trades = _number(row.get("trades"))
        days = _number(row.get("days"))
        weekly = None
        if days and days > 0:
            try:
                weekly = trades_per_week(row)
            except Exception:
                weekly = None
        before = prev or {}
        out.append({
            "file": name,
            "root": root,
            "timeframe": timeframe,
            "stage": stage,
            "attempt": counts[stage],
            "change": str(row.get("change") or ""),
            "impact": str(row.get("impact") or ""),
            "net_profit": profit,
            "max_drawdown": drawdown,
            "trades": int(trades) if trades is not None else None,
            "trades_per_week": weekly,
            "win_rate": _number(row.get("win_rate")),
            "profit_factor": _number(row.get("profit_factor")),
            "d_profit": _delta(profit, _number(before.get("net_profit"))) if prev is not None else None,
            "d_drawdown": _delta(drawdown, _number(before.get("max_drawdown"))) if prev is not None else None,
            "d_trades": _whole(_delta(trades, _number(before.get("trades")))) if prev is not None else None,
            "verdict": "flat",
            "misses": [],
            "passes": False,
            # A chart the proving ledger no longer lists has only its notes file's time, which is when the
            # file was last written, not when this chart ran. It is shown as "before" that time.
            "noted": when[:16] if dated else "",
            "noted_before": "" if dated else when[:16],
            "_dated": dated,
            "_when": when,
            "_seq": seq,
            "_row": row,
            "_prev": prev,
        })
        previous[stage] = row
    return out


def _judge(item: dict, limits: dict, start: float, loss_cap: float, account_size: float) -> None:
    """Fill the verdict and the acceptance misses of one shown row."""
    from futuresfund import objective

    row, prev = item["_row"], item["_prev"]
    if not item["trades"]:
        item["verdict"] = "no_trades"
    elif prev is not None:
        try:
            move = objective.scale_move(_measured(prev), _measured(row), account_size)
        except Exception:
            move = "flat"
        item["verdict"] = _VERDICT.get(move, "flat")
    stage = item["stage"]
    try:
        missed = objective.acceptance_misses(
            _measured(row), days=stage if stage != 365 else None, limits=limits, start=start, loss_cap=loss_cap,
        )
    except Exception:
        missed = {"error": None}
    item["misses"] = list(missed)
    item["passes"] = not missed


def _build_changes(limit: int, now: float) -> dict:
    ledger: dict[str, str] = {}
    try:
        from futuresfund import proving

        for row in proving.rows():
            ledger[str(row.get("id") or "")] = str(row.get("updated") or "")
    except Exception:
        pass
    rows: list[dict] = []
    files = 0
    for mtime, path in _recent_notes(now):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        instruments = data.get("instruments") if isinstance(data, dict) else None
        if not isinstance(instruments, dict):
            continue
        files += 1
        name = str(data.get("file") or path.stem)
        written = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        for root, record in instruments.items():
            chart_root = str(root).upper()
            for timeframe, trials in _charts(record):
                stamped = ledger.get(f"{name}|{chart_root}|{timeframe}")
                rows.extend(_chart_rows(name, chart_root, timeframe, trials, stamped or written, dated=bool(stamped)))
    # Charts with a ledger time come first, newest first. The rest follow by their notes file's time.
    rows.sort(key=lambda item: (item["_dated"], item["_when"], item["file"], item["root"], item["timeframe"], item["_seq"]), reverse=True)
    shown = rows[:limit]
    if shown:
        from futuresfund import objective
        from futuresfund.book import held_reads, strategy_acceptance
        from futuresfund.learn import ACCOUNT_SIZE

        # objective.scale_move reads the acceptance boxes several times a row. One book read serves the build.
        with held_reads():
            limits = strategy_acceptance()
            start = objective.drawdown_start()
            loss_cap = objective.account_trade_loss_cap()
            for item in shown:
                _judge(item, limits, start, loss_cap, float(ACCOUNT_SIZE))
    clean = [{key: value for key, value in item.items() if not key.startswith("_")} for item in shown]
    summary = {"total": len(clean), "toward": 0, "away": 0, "flat": 0, "no_trades": 0, "passes": 0}
    for item in clean:
        summary[item["verdict"]] = summary.get(item["verdict"], 0) + 1
        if item["passes"]:
            summary["passes"] += 1
    return {
        "rows": clean,
        "summary": summary,
        "files": files,
        "hours": CHANGE_HOURS,
        "built": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
    }
