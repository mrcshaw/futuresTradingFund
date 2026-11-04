"""What each input did when the search moved it, and the inputs it stops moving.

The search moves one input at a time. When a move leaves the result exactly as it was (the same
profit, drawdown, trade count, and win rate) the input had no effect in that direction at those
settings, so the rest of that search does not move it that way again. A number that changed
nothing in both directions, and a switch or choice that changed nothing when it was flipped, is
taken out of that search: it stays where it was and the attempts go to inputs that do something.
The next proving stage has more trades, so it tries every input again.

Every move is kept per script in researchNotes/input_effects/<script>.json, written once at the end
of each search. Across stages, charts, and instruments that file shows which inputs move results
and which never do. The creation team and the researchers read the summary.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

# None keeps the files beside the research notes, wherever learn.NOTES points, so a test that
# moves the notes moves these too. A path here puts them there instead.
HOME: Path | None = None
_lock = threading.Lock()
_current = threading.local()


def _signature(row: dict) -> tuple:
    return (row.get("net_profit"), row.get("max_drawdown"), row.get("trades"), row.get("win_rate"))


def _changed(before: dict, after: dict) -> list[str]:
    return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))


def _way(old, new):
    """The direction of one move: up or down for a number, the new value for a switch or a choice."""
    if isinstance(old, bool) or isinstance(new, bool) or not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return ("to", new)
    if new > old:
        return 1
    if new < old:
        return -1
    return None


def _usable(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("error") or row.get("fatal") or row.get("stopped") or row.get("skipped") or row.get("blocked"):
        return False
    try:
        return int(row.get("trades") or 0) > 0 and row.get("net_profit") is not None
    except (TypeError, ValueError):
        return False


class Tracker:
    """One search. It sees every finished run, including the ones that repeat a result."""

    def __init__(self, path: Path | None = None, chart: str = "", window: str = "", core: bool = False) -> None:
        self.path = path
        self.chart = chart
        self.window = window
        self.core = core
        # Set by the first run that names its instrument and chart, as every engine run does.
        self.charted = False
        self.stamp = 0
        self.history: list[tuple[dict, tuple, dict]] = []
        self.dead: dict[str, set] = {}
        self.removed: dict[str, dict] = {}
        self.moves: dict[str, dict] = {}

    # The search asks these two while it chooses the next settings.
    def is_removed(self, name: str) -> bool:
        return name in self.removed

    def dead_move(self, name: str, old, new) -> bool:
        way = _way(old, new)
        return way is not None and way in self.dead.get(name, set())

    def observe(self, stamp: int, params: dict, measured: dict) -> list[str]:
        """Record one finished run. Returns the inputs this run showed to do nothing in a direction not known before."""
        if stamp != self.stamp:
            # A rewrite is a different script. What the old one ignored says nothing about it.
            self.stamp = stamp
            self.history = []
            self.dead = {}
            self.removed = {}
        if not _usable(measured):
            return []
        root, frame = str(measured.get("instrument") or ""), str(measured.get("timeframe") or "")
        if root and frame:
            self.charted = True
            if not self.chart:
                self.chart = f"{root} {frame}"
        params = dict(params or {})
        signature = _signature(measured)
        numbers = {key: measured.get(key) for key in ("net_profit", "max_drawdown", "trades", "win_rate")}
        learned: list[str] = []
        same = [
            (index, earlier, _changed(earlier, params))
            for index, (earlier, seen, _numbers) in enumerate(self.history)
            if seen == signature
        ]
        same = [item for item in same if item[2]]
        if same:
            index, earlier, names = min(same, key=lambda item: (len(item[2]), -item[0]))
            # One input moved, or a few moved together from the run just before. A result that came back
            # after other moves in between is not evidence that these inputs do nothing.
            if len(names) == 1 or (len(names) <= 3 and index == len(self.history) - 1):
                for name in names:
                    if self._no_effect(name, earlier.get(name), params.get(name), numbers):
                        learned.append(name)
        else:
            for earlier, seen, before in reversed(self.history):
                names = _changed(earlier, params)
                if len(names) == 1:
                    self._effect(names[0], earlier.get(names[0]), params.get(names[0]), before, numbers)
                    break
        self.history.append((params, signature, numbers))
        return learned

    def _entry(self, name: str) -> dict:
        return self.moves.setdefault(name, {"moved": 0, "no_effect": 0, "changed": 0, "profit_changes": [], "last": {}})

    def _no_effect(self, name: str, old, new, numbers: dict) -> bool:
        """Record a move that changed nothing. True when that direction was not known to do nothing."""
        way = _way(old, new)
        if way is None:
            return False
        entry = self._entry(name)
        entry["moved"] += 1
        entry["no_effect"] += 1
        entry["last"] = {"from": old, "to": new, "effect": "none", "trades": numbers.get("trades")}
        dead = self.dead.setdefault(name, set())
        new_way = way not in dead
        dead.add(way)
        numeric = isinstance(way, int)
        if name not in self.removed and ((numeric and {1, -1} <= dead) or not numeric):
            self.removed[name] = {"from": old, "to": new, "trades": numbers.get("trades")}
        return new_way

    def _effect(self, name: str, old, new, before: dict, after: dict) -> None:
        if _way(old, new) is None:
            return
        entry = self._entry(name)
        entry["moved"] += 1
        entry["changed"] += 1
        try:
            change = round(float(after.get("net_profit") or 0) - float(before.get("net_profit") or 0), 2)
        except (TypeError, ValueError):
            change = 0.0
        entry["profit_changes"].append(change)
        entry["last"] = {"from": old, "to": new, "effect": "changed", "profit_change": change, "trades": after.get("trades")}

    def finish(self) -> None:
        """Write what this search learned onto the script's file.

        Nothing is written for a search that moved nothing, or whose runs did not come from an engine chart.
        """
        if self.path is None or not self.moves or not self.charted:
            return
        try:
            _record(self.path, self)
        except (OSError, ValueError, TypeError):
            pass


def current() -> Tracker | None:
    """The search running on this thread, if any."""
    return getattr(_current, "tracker", None)


def begin(tracker: Tracker) -> Tracker | None:
    previous = current()
    _current.tracker = tracker
    return previous


def end(previous: Tracker | None) -> None:
    _current.tracker = previous


def _home() -> Path:
    if HOME is not None:
        return Path(HOME)
    from futuresfund import learn

    return learn.NOTES / "input_effects"


def _file(path: Path) -> Path:
    return _home() / f"{path.stem.replace(' ', '_')}.json"


def load(path: Path) -> dict:
    try:
        data = json.loads(_file(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(path: Path, tracker: Tracker) -> None:
    with _lock:
        data = load(path)
        inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for name, seen in tracker.moves.items():
            kept = inputs.get(name) if isinstance(inputs.get(name), dict) else {}
            changes = [float(value) for value in kept.get("profit_changes") or [] if isinstance(value, (int, float))]
            changes = (changes + [float(value) for value in seen["profit_changes"]])[-60:]
            kept = {
                "moved": int(kept.get("moved") or 0) + seen["moved"],
                "changed": int(kept.get("changed") or 0) + seen["changed"],
                "no_effect": int(kept.get("no_effect") or 0) + seen["no_effect"],
                "removed": int(kept.get("removed") or 0) + (1 if name in tracker.removed else 0),
                "profit_changes": changes,
                "biggest_profit_change": round(max((abs(value) for value in changes), default=0.0), 2),
                "average_profit_change": round(sum(abs(value) for value in changes) / len(changes), 2) if changes else 0.0,
                "last": {**seen["last"], "chart": tracker.chart, "window": tracker.window, "at": stamp},
            }
            inputs[name] = kept
        data.update(
            file=path.name,
            core=bool(tracker.core or data.get("core")),
            inputs=inputs,
            searches=int(data.get("searches") or 0) + 1,
            updated=stamp,
        )
        _home().mkdir(parents=True, exist_ok=True)
        target = _file(path)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, default=str), encoding="utf-8")
        temporary.replace(target)


def _ranked(inputs: dict) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    moving = [(name, row) for name, row in inputs.items() if int(row.get("changed") or 0) > 0]
    moving.sort(key=lambda item: (-float(item[1].get("average_profit_change") or 0), item[0]))
    idle = [(name, row) for name, row in inputs.items() if int(row.get("changed") or 0) == 0 and int(row.get("no_effect") or 0) > 0]
    idle.sort(key=lambda item: (-int(item[1].get("no_effect") or 0), item[0]))
    return moving, idle


def script_lines(path: Path | None, limit: int = 6) -> list[str]:
    """What this script's inputs did across its searches, for the researcher."""
    if path is None:
        return []
    inputs = load(path).get("inputs") or {}
    if not isinstance(inputs, dict) or not inputs:
        return []
    moving, idle = _ranked(inputs)
    lines = []
    if moving:
        lines.append(
            "Inputs that moved the result most when the search changed them: "
            + ", ".join(f"{name} (profit moved ${float(row.get('average_profit_change') or 0):,.0f} on average over {int(row.get('changed') or 0)} moves)" for name, row in moving[:limit])
            + "."
        )
    if idle:
        lines.append(
            "Inputs that never changed a result when moved: "
            + ", ".join(f"{name} ({int(row.get('no_effect') or 0)} moves)" for name, row in idle[:limit])
            + "."
        )
    return lines


def desk_lines(core_only: bool = False, limit: int = 8) -> list[str]:
    """The same across scripts, by input name, so a new script keeps the inputs that matter.

    core_only keeps the delta volume breakout scripts.
    """
    totals: dict[str, dict] = {}
    home = _home()
    files = sorted(home.glob("*.json")) if home.is_dir() else []
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if core_only and not data.get("core"):
            continue
        for name, row in (data.get("inputs") or {}).items():
            if not isinstance(row, dict):
                continue
            total = totals.setdefault(name, {"changed": 0, "no_effect": 0, "profit_changes": [], "scripts": 0})
            total["changed"] += int(row.get("changed") or 0)
            total["no_effect"] += int(row.get("no_effect") or 0)
            total["scripts"] += 1
            total["profit_changes"].extend(float(value) for value in row.get("profit_changes") or [] if isinstance(value, (int, float)))
    for total in totals.values():
        changes = total.pop("profit_changes")
        total["average_profit_change"] = sum(abs(value) for value in changes) / len(changes) if changes else 0.0
    moving, idle = _ranked(totals)
    lines = []
    if moving:
        lines.append(
            "Across the desk's research, these inputs moved profit the most when changed: "
            + ", ".join(f"{name} (${float(row['average_profit_change']):,.0f} on average)" for name, row in moving[:limit])
            + "."
        )
    if idle:
        lines.append(
            "These inputs changed no result when moved, on every script that has them: "
            + ", ".join(name for name, _row in idle[:limit])
            + "."
        )
    return lines
