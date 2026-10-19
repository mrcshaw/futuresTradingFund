"""The indicator library every desk can read.

It starts with the ta.* indicators the engine runs. When a researcher takes a
script, or the creation tester runs a new one, the script's own indicators are
added with their Pine source: each user function, and each section the script
marks with a "// ===== NAME =====" header that calculates something. The
strategy developer builds new scripts from this library.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from futuresfund.learn import NOTES

LIBRARY_PATH = NOTES / "indicator_library.json"
FOLDERS = ("learningStrategies", "learnedStrategies", "createdStrategies", "checkScripts")
_LOCK = threading.Lock()

BUILT_IN = {
    "ta.sma": ("ta.sma(source, length)", "Simple moving average: the mean of the last length values."),
    "ta.ema": ("ta.ema(source, length)", "Exponential moving average. Recent bars weigh more."),
    "ta.rma": ("ta.rma(source, length)", "Wilder's moving average, the average RSI and ATR use."),
    "ta.wma": ("ta.wma(source, length)", "Weighted moving average. The weight rises toward the current bar."),
    "ta.vwma": ("ta.vwma(source, length)", "Volume-weighted moving average."),
    "ta.atr": ("ta.atr(length)", "Average true range: the bar's range including gaps, smoothed with Wilder's average."),
    "ta.tr": ("ta.tr(handle_na)", "True range of the current bar."),
    "ta.rsi": ("ta.rsi(source, length)", "Relative strength index, from 0 to 100."),
    "ta.stdev": ("ta.stdev(source, length)", "Standard deviation of the last length values."),
    "ta.highest": ("ta.highest(source, length)", "Highest value over the last length bars."),
    "ta.lowest": ("ta.lowest(source, length)", "Lowest value over the last length bars."),
    "ta.highestbars": ("ta.highestbars(source, length)", "How many bars back the highest value is."),
    "ta.lowestbars": ("ta.lowestbars(source, length)", "How many bars back the lowest value is."),
    "ta.crossover": ("ta.crossover(a, b)", "True on the bar a crosses above b."),
    "ta.crossunder": ("ta.crossunder(a, b)", "True on the bar a crosses below b."),
    "ta.cross": ("ta.cross(a, b)", "True on the bar a and b cross in either direction."),
    "ta.change": ("ta.change(source, length)", "The change from length bars ago."),
    "ta.mom": ("ta.mom(source, length)", "Momentum: the source minus its value length bars ago."),
    "ta.roc": ("ta.roc(source, length)", "Rate of change, in percent."),
    "ta.pivothigh": ("ta.pivothigh(source, leftbars, rightbars)", "A pivot high, known rightbars bars after it forms."),
    "ta.pivotlow": ("ta.pivotlow(source, leftbars, rightbars)", "A pivot low, known rightbars bars after it forms."),
    "ta.valuewhen": ("ta.valuewhen(condition, source, occurrence)", "The source's value when the condition was last true."),
    "ta.barssince": ("ta.barssince(condition)", "Bars since the condition was last true."),
    "ta.cum": ("ta.cum(source)", "Running total since the first bar."),
    "ta.rising": ("ta.rising(source, length)", "True when the source rose on each of the last length bars."),
    "ta.falling": ("ta.falling(source, length)", "True when the source fell on each of the last length bars."),
    "ta.vwap": ("ta.vwap(source)", "Volume-weighted average price since the session opened."),
    "ta.macd": ("ta.macd(source, fastlen, slowlen, siglen)", "MACD line, signal line, and histogram."),
    "ta.bb": ("ta.bb(series, length, mult)", "Bollinger bands: basis, upper band, lower band."),
    "ta.stoch": ("ta.stoch(source, high, low, length)", "Stochastic %K, from 0 to 100."),
    "math.sum": ("math.sum(source, length)", "Rolling sum of the last length values."),
}

# A section with one of these words sets up the script instead of measuring the market.
_SETUP_WORDS = (
    "INPUT", "SESSION", "RISK", "ENTR", "EXIT", "PLOT", "TABLE", "DISPLAY", "LABEL", "ALERT", "COOLDOWN",
    "BREAKEVEN", "TRAILING STOP", "COMPUTED", "VAR STATE", "RESET", "FLIP", "REVERSAL", "STOP", "TAKE PROFIT",
    "DAILY", "FILL COLOR", "COLOR", "LINE", "CRITERIA", "ORDER", "POSITION", "VISUAL", "DRAW", "DEBUG", "BOX",
    "TIME", "LOGGING", "LEGACY", "TRAILING", "TRADE CLOSED", "CUT ON",
)
_HEADER = re.compile(r"^\s*//\s*=+\s*(.+?)\s*=*\s*$")
_FUNCTION = re.compile(r"^(\w+)\s*\(([^)]*)\)\s*=>")


def all_indicators() -> list[dict]:
    with _LOCK:
        data = _load()
    rows = list(data.values())
    rows.sort(key=lambda row: (row["kind"] != "built-in", row["name"].lower()))
    return rows


def indicator(indicator_id: str) -> dict | None:
    with _LOCK:
        return _load().get(indicator_id)


def record_script(name: str, source: str, by: str = "") -> list[dict]:
    """Add the indicators in one script. Returns the ones the library did not have."""
    found = scan_script(name, source)
    added = []
    with _LOCK:
        data = _load()
        changed = False
        for item in found:
            current = data.get(item["id"])
            if current is None:
                item["added_by"] = by or "Chart researcher"
                item["added_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                data[item["id"]] = item
                added.append(item)
                changed = True
                continue
            if name not in current["scripts"]:
                current["scripts"].append(name)
                changed = True
            if item.get("source") and item["hash"] not in {variant["hash"] for variant in current.get("variants", [])} and item["hash"] != current.get("hash"):
                current.setdefault("variants", []).append({"script": name, "hash": item["hash"], "source": item["source"]})
                changed = True
            if not current.get("example") and item.get("example"):
                current["example"] = item["example"]
                changed = True
        if changed:
            _save(data)
    return added


def seed(folders: list[Path] | None = None) -> int:
    """Read every script already on file. Returns how many indicators the library holds."""
    base = Path(__file__).resolve().parent
    for folder in folders or [base / name for name in FOLDERS]:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.pine")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            record_script(path.name, text, "Systems Administrator")
    with _LOCK:
        data = _load()
        if _ensure_built_ins(data):
            _save(data)
        return len(data)


def scan_script(name: str, source: str) -> list[dict]:
    """The built-in indicators a script calls, its functions, and its calculating sections."""
    lines = [_strip(line) for line in (source or "").splitlines()]
    found: dict[str, dict] = {}
    for line in lines:
        for match in re.finditer(r"\b(ta\.\w+|math\.sum)\s*\(", line):
            key = match.group(1)
            if key in BUILT_IN and key not in found:
                found[key] = _built_in(key, name, line.strip())
    runs = _engine_runs(source)
    for item in _functions(source, name) + _sections(source, name):
        item["engine"] = runs
        found.setdefault(item["id"], item)
    return list(found.values())


def agent_catalog(limit: int = 1200) -> str:
    """A short list for every desk's model."""
    rows = all_indicators()
    if not rows:
        return "Indicator library: none recorded yet."
    built = ", ".join(row["name"] for row in rows if row["kind"] == "built-in")
    custom = "; ".join(f"{row['name']} ({row['scripts'][0] if row['scripts'] else 'library'})" for row in rows if row["kind"] != "built-in")
    text = f"Indicator library: built-in {built}. From our scripts: {custom or 'none yet'}."
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def creation_context(limit: int = 3500) -> str:
    """What the strategy developer can build from, with the Pine source of the script indicators."""
    rows = all_indicators()
    lines = ["Indicator library. Build the new script from these. The engine runs each one listed."]
    lines.append("Built-in: " + "; ".join(f"{row['signature']} - {row['summary']}" for row in rows if row["kind"] == "built-in" and row.get("engine", True)))
    for row in rows:
        if row["kind"] == "built-in" or not row.get("engine"):
            continue
        snippet = row["source"] if len(row["source"]) <= 600 else row["source"][:600].rstrip() + "\n// ..."
        lines.append(f"{row['name']} from {row['scripts'][0]}:\n{snippet}")
        if sum(len(line) for line in lines) > limit:
            lines.pop()
            break
    return "\n".join(lines)[:limit]


def _built_in(key: str, script: str, line: str) -> dict:
    signature, summary = BUILT_IN[key]
    return {
        "id": key,
        "name": key,
        "kind": "built-in",
        "signature": signature,
        "summary": summary,
        "example": line[:200],
        "source": "",
        "hash": key,
        "scripts": [script] if script else [],
        "engine": True,
    }


def _functions(source: str, script: str) -> list[dict]:
    raw = (source or "").replace("\t", "    ").splitlines()
    found = []
    k = 0
    while k < len(raw):
        line = _strip(raw[k])
        match = _FUNCTION.match(line)
        if not match or line.startswith(" "):
            k += 1
            continue
        block = [raw[k].rstrip()]
        k += 1
        while k < len(raw) and (not raw[k].strip() or raw[k].startswith(" ")):
            block.append(raw[k].rstrip())
            k += 1
        body = "\n".join(block).strip()
        name = match.group(1)
        draws = re.search(r"\b(label|line|box|table)\.(new|set_\w+|delete)\s*\(", _strip_block(body))
        if not _measures(body) or (draws and not re.search(r"\b(ta|math)\.\w+\(", _strip_block(body))):
            continue
        found.append(_custom(f"function:{name}", f"{name}()", "function", f"{name}({match.group(2).strip()})", _comment_before(raw, block[0]), body, script))
    return found


def _sections(source: str, script: str) -> list[dict]:
    raw = (source or "").replace("\t", "    ").splitlines()
    found = []
    title = None
    block: list[str] = []
    notes: list[str] = []

    def close() -> None:
        if title is None:
            return
        code = [line for line in block if _strip(line).strip()]
        body = "\n".join(block).strip()
        if not code or _is_setup(title, code) or not _measures(body):
            return
        name = title.title()
        found.append(_custom(f"section:{_slug(title)}", name, "script section", name, " ".join(notes)[:300], body, script))

    for line in raw:
        header = _HEADER.match(line)
        if header and header.group(1).strip("= ").strip():
            close()
            title = header.group(1).strip("= ").strip()
            block = []
            notes = []
            continue
        if title is None:
            continue
        stripped = line.strip()
        if stripped.startswith("//") and not block:
            notes.append(stripped.lstrip("/ ").strip())
            continue
        block.append(line.rstrip())
    close()
    return found


def _custom(indicator_id: str, name: str, kind: str, signature: str, summary: str, body: str, script: str) -> dict:
    return {
        "id": indicator_id,
        "name": name,
        "kind": kind,
        "signature": signature,
        "summary": summary or f"Calculated in {script}.",
        "example": "",
        "source": body,
        "hash": hashlib.sha1(re.sub(r"\s+", " ", body).encode()).hexdigest()[:12],
        "scripts": [script],
        "engine": True,
    }


def _is_setup(title: str, code: list[str]) -> bool:
    upper = title.upper()
    if any(word in upper for word in _SETUP_WORDS):
        return True
    useful = [line for line in code if "input." not in line and not re.match(r"\s*(plot|plotshape|bgcolor|table|label|line|box|strategy)\b", _strip(line))]
    return not useful


def _measures(body: str) -> bool:
    """True when the code calculates from prices, volume, or other indicators."""
    text = _strip_block(body)
    if not re.search(r"\b(ta\.|math\.|close|open|high|low|volume|hl2|hlc3)\b|\[\w+\]", text):
        return False
    drawing = re.findall(r"\b(label|line|box|table|plot|plotshape|bgcolor|fill)\.\w+\(", text)
    lines = [line for line in text.splitlines() if line.strip()]
    return len(drawing) < len(lines)


def _engine_runs(source: str) -> bool:
    from futuresfund.pine_runtime import supported

    try:
        return supported(source)
    except Exception:
        return False


def _comment_before(raw: list[str], first: str) -> str:
    try:
        index = raw.index(first)
    except ValueError:
        return ""
    notes = []
    k = index - 1
    while k >= 0 and raw[k].strip().startswith("//") and not _HEADER.match(raw[k]):
        notes.insert(0, raw[k].strip().lstrip("/ ").strip())
        k -= 1
    return " ".join(notes)[:300]


def _strip(line: str) -> str:
    from futuresfund.pine_runtime import _strip_comment

    return _strip_comment(line)


def _strip_block(body: str) -> str:
    return "\n".join(_strip(line) for line in body.splitlines())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "section"


def _ensure_built_ins(data: dict) -> bool:
    changed = False
    for key in BUILT_IN:
        if key not in data:
            item = _built_in(key, "", "")
            item["added_by"] = "Systems Administrator"
            item["added_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            data[key] = item
            changed = True
    return changed


def _load() -> dict:
    if not LIBRARY_PATH.is_file():
        return {}
    try:
        data = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = data.get("indicators") if isinstance(data, dict) else None
    return {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")} if isinstance(rows, list) else {}


def _save(data: dict) -> None:
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_PATH.write_text(json.dumps({"indicators": list(data.values())}, indent=2), encoding="utf-8")
