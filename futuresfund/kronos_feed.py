"""Echo the Kronos fine-tune log onto the Systems Administrator channel."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from futuresfund.config import ROOT

ADMIN = "Systems Administrator"
_LOG_DIR = ROOT / "kronosFinetune" / "futures_5y" / "logs"
_PHASE = {
    "tokenizer_training_rank_0.log": "tokenizer",
    "basemodel_training_rank_0.log": "predictor",
}
_STEP = re.compile(r"\[Epoch (\d+)/(\d+), Step (\d+)/(\d+)\].*?Loss:\s*([-+0-9.]+)")
_SUMMARY = re.compile(r"Epoch (\d+)/(\d+) Summary")
_EPOCHS = re.compile(r"Training epochs:\s*(\d+)")
_CONFIG = ROOT / ".tools" / "Kronos" / "finetune_csv" / "configs" / "config_futures_5y.yaml"


def start_kronos_feed(board) -> None:
    """Follow the training log and post each progress line on that desk."""
    threading.Thread(target=_watch, args=(board,), daemon=True, name="kronos-feed").start()


def _watch(board) -> None:
    offsets: dict[str, int] = {}
    announced = False
    while True:
        try:
            if not announced:
                board.log(ADMIN, "Watching the Kronos fine-tune. Each training step is posted on this desk.")
                announced = True
            for path in sorted(_LOG_DIR.glob("*_training_rank_0.log")):
                for line in _new_lines(path, offsets):
                    spoken = _speak(path.name, line)
                    if spoken:
                        board.log(ADMIN, spoken)
        except Exception:
            pass
        time.sleep(5)


def _new_lines(path: Path, offsets: dict[str, int]) -> list[str]:
    key = str(path)
    size = path.stat().st_size
    offset = offsets.get(key)
    with path.open(encoding="utf-8", errors="replace") as handle:
        if offset is None or offset > size:
            start = max(0, size - 80_000)
            handle.seek(start)
            if start:
                handle.readline()
            chunk = handle.read()
            offsets[key] = handle.tell()
            progress = [line for line in chunk.splitlines() if _speak(path.name, line)]
            return progress[-1:]
        if size == offset:
            return []
        handle.seek(offset)
        chunk = handle.read()
        offsets[key] = handle.tell()
    return [line for line in chunk.splitlines() if line.strip()]


def progress() -> dict:
    """Steps finished across the tokenizer and the predictor, out of every step in the run."""
    tokenizer_epochs, predictor_epochs = _planned_epochs()
    return overall(
        _read_log(_LOG_DIR / "tokenizer_training_rank_0.log"),
        _read_log(_LOG_DIR / "basemodel_training_rank_0.log"),
        tokenizer_epochs,
        predictor_epochs,
    )


def overall(tokenizer_text: str, predictor_text: str, tokenizer_epochs: int = 30, predictor_epochs: int = 20) -> dict:
    """One reading for the whole fine-tune. A finished epoch counts every step, not the last logged line."""
    tokenizer = _measure(tokenizer_text, "tokenizer", tokenizer_epochs)
    predictor = _measure(predictor_text, "predictor", predictor_epochs)
    steps_each = tokenizer["steps_each"] or predictor["steps_each"]
    if not steps_each:
        return _idle()
    tokenizer_total = tokenizer["epochs"] * steps_each
    predictor_total = predictor["epochs"] * steps_each
    tokenizer_done = _done_steps(tokenizer, steps_each, tokenizer_total)
    predictor_done = _done_steps(predictor, steps_each, predictor_total)
    whole_done = tokenizer_done + predictor_done
    whole_total = tokenizer_total + predictor_total
    finished = tokenizer["finished"] and predictor["finished"]
    if predictor["started"] and not predictor["finished"]:
        current = predictor
        phase = "predictor"
    elif not tokenizer["finished"]:
        current = tokenizer
        phase = "tokenizer"
    else:
        current = predictor if predictor["started"] else tokenizer
        phase = "predictor" if predictor["started"] else "tokenizer"
    running = (tokenizer["started"] or predictor["started"]) and not finished
    percent = (100.0 * whole_done / whole_total) if whole_total else 0.0
    return {
        "running": running,
        "finished": finished,
        "phase": phase,
        "epoch": current["epoch"],
        "epochs": current["epochs"],
        "step": current["step"],
        "steps": steps_each,
        "loss": current["loss"],
        "percent": percent,
        "whole_done": whole_done,
        "whole_total": whole_total,
        "whole_left": max(0, whole_total - whole_done),
        "tokenizer_finished": tokenizer["finished"],
        "predictor_started": predictor["started"],
    }


def _done_steps(phase: dict, steps_each: int, total: int) -> int:
    if phase["finished"]:
        return total
    if not phase["started"] or not steps_each:
        return 0
    return min(total, int(phase["done"]))


def _measure(text: str, phase: str, planned: int) -> dict:
    body = text or ""
    finished = _phase_finished(body, phase)
    epochs = planned
    header = _EPOCHS.search(body)
    if header:
        epochs = int(header.group(1))
    found = list(_STEP.finditer(body))
    summaries = [(int(match.group(1)), int(match.group(2))) for match in _SUMMARY.finditer(body)]
    if found:
        epoch, epoch_count, step, steps, loss = found[-1].groups()
        epochs = int(epoch_count)
        epoch_n = int(epoch)
        step_n = int(step)
        steps_each = int(steps)
        loss_n = float(loss)
    else:
        epoch_n = 0
        step_n = 0
        steps_each = 0
        loss_n = None
    if summaries:
        epochs = summaries[-1][1] or epochs
    summary_epoch = summaries[-1][0] if summaries else 0
    started = finished or bool(found) or bool(summaries) or _phase_started(body, phase)
    if steps_each and summary_epoch and epoch_n <= summary_epoch:
        done = summary_epoch * steps_each
    elif steps_each and epoch_n:
        done = summary_epoch * steps_each + step_n
    else:
        done = 0
    return {
        "started": started,
        "finished": finished,
        "epochs": epochs,
        "epoch": epoch_n,
        "step": step_n,
        "steps_each": steps_each,
        "loss": loss_n,
        "done": done,
    }


def _phase_finished(text: str, phase: str) -> bool:
    if phase == "tokenizer":
        return "Tokenizer training completed" in text
    return "Training completed!" in text


def _phase_started(text: str, phase: str) -> bool:
    if phase == "tokenizer":
        return "Starting tokenizer" in text
    return "Starting training" in text or "Basemodel Training Started" in text


def _planned_epochs() -> tuple[int, int]:
    """Epoch counts from the fine-tune config. The logs repeat them once training starts."""
    try:
        text = _CONFIG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 30, 20
    tokenizer = re.search(r"tokenizer_epochs:\s*(\d+)", text)
    predictor = re.search(r"basemodel_epochs:\s*(\d+)", text)
    return (
        int(tokenizer.group(1)) if tokenizer else 30,
        int(predictor.group(1)) if predictor else 20,
    )


def _read_log(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= 200_000:
        return text
    return text[:16_000] + "\n" + text[-80_000:]


def _idle() -> dict:
    return {
        "running": False,
        "finished": False,
        "phase": "",
        "epoch": 0,
        "epochs": 0,
        "step": 0,
        "steps": 0,
        "loss": None,
        "percent": 0.0,
        "whole_done": 0,
        "whole_total": 0,
        "whole_left": 0,
        "tokenizer_finished": False,
        "predictor_started": False,
    }


def snapshot(text: str, phase: str) -> dict | None:
    """One progress reading from a log tail."""
    found = list(_STEP.finditer(text or ""))
    if not found:
        return None
    epoch, epochs, step, steps, loss = found[-1].groups()
    epoch_n = int(epoch)
    epochs_n = int(epochs)
    step_n = int(step)
    steps_n = int(steps)
    percent = (100.0 * step_n / steps_n) if steps_n else 0.0
    return {
        "running": True,
        "phase": phase,
        "epoch": epoch_n,
        "epochs": epochs_n,
        "step": step_n,
        "steps": steps_n,
        "loss": float(loss),
        "percent": percent,
    }


def _speak(name: str, line: str) -> str:
    phase = _PHASE.get(name, "training")
    text = line.split(" - INFO - ", 1)[-1].strip()
    if not text or text.startswith("-") or "VQ Loss" in text or "Recon Loss" in text:
        return ""
    if text.startswith("[Epoch"):
        return f"Kronos {phase} {text}."
    if "Epoch" in text and "Summary" in text:
        return f"Kronos {phase} {text.strip('- ')}."
    if text.startswith("Training Loss:") or text.startswith("Validation Loss:") or text.startswith("Epoch Time:"):
        return f"Kronos {phase} {text}"
    if "Best model saved" in text:
        return f"Kronos {phase} saved a checkpoint. {text}"
    if "parameters:" in text.lower():
        return f"Kronos {phase} loaded. {text}"
    return ""
