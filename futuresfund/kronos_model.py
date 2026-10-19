"""Kronos candlestick forecasts.

The forecast uses NeoQuasar/Kronos-base, the largest open Kronos model, with
NeoQuasar/Kronos-Tokenizer-base. Kronos-large is not published. A finished
fine-tune under kronosFinetune/futures_5y replaces those weights. The forecast
loads on CPU. Tests substitute predict_paths and do not reach this loader.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOOKBACK = 400
MAX_CONTEXT = 512
PREDICT_LEN = 6
SAMPLE_COUNT = 8
TEMPERATURE = 1.0
TOP_P = 0.9
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
MODEL_ID = "NeoQuasar/Kronos-small"
_FINETUNE = Path(__file__).resolve().parent.parent / "kronosFinetune" / "futures_5y"

_EASTERN = ZoneInfo("America/New_York")
_lock = threading.Lock()
_predictor = None


def warm() -> None:
    """Load the weights on the caller thread. A forecast that already holds the model is left alone.

    The chart must not wait on that forecast. A background thread is a poor place for the first import,
    so a free caller still loads the weights itself.
    """
    if _predictor is not None:
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        _ensure_loaded()
    finally:
        _lock.release()


def predict_paths(bars, pred_len: int = PREDICT_LEN, sample_count: int = SAMPLE_COUNT, temperature: float = TEMPERATURE, top_p: float = TOP_P, future_times=None) -> list[list[dict]]:
    """Sample forecast paths. Each path is pred_len candles of open, high, low, close, and volume.

    Kronos averages every path inside one predict() call, so each path is sampled on its own.
    """
    history = list(bars or [])[-LOOKBACK:]
    if not history or pred_len < 1 or sample_count < 1:
        return []
    predictor = _load()
    frame, stamps, mean, std = _prepare(history)
    ahead = list(future_times or [])
    if len(ahead) < pred_len:
        from futuresfund.kronos_replay import next_open_times

        anchor = history[-1].get("t")
        ahead = list(ahead) + next_open_times(anchor, "1m", pred_len - len(ahead))
    ahead = ahead[:pred_len]
    if len(ahead) < pred_len:
        return []
    y_stamp = _time_features(ahead)
    paths = []
    with _lock:
        for _ in range(sample_count):
            paths.append(_one_path(predictor, frame, stamps, y_stamp, mean, std, pred_len, temperature, top_p))
    return paths


def _weights(folder: Path, fallback: str) -> str:
    """The fine-tuned folder when training has saved one, otherwise the published model."""
    if (folder / "config.json").is_file():
        return str(folder)
    return fallback


def _load():
    with _lock:
        return _ensure_loaded()


def _ensure_loaded():
    global _predictor
    if _predictor is not None:
        return _predictor
    source = Path(__file__).resolve().parent.parent / ".tools" / "Kronos"
    if not (source / "model").is_dir():
        raise ImportError("Kronos is not loaded, so there is no prediction.")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    try:
        import torch
        from model import Kronos, KronosPredictor, KronosTokenizer
    except ImportError as exc:
        raise ImportError("Kronos is not loaded, so there is no prediction.") from exc
    tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID)
    tokenizer = KronosTokenizer.from_pretrained(_weights(_FINETUNE / "tokenizer" / "best_model", TOKENIZER_ID))
    model = Kronos.from_pretrained(_weights(_FINETUNE / "basemodel" / "best_model", MODEL_ID))
    model.eval()
    _predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=MAX_CONTEXT)
    return _predictor


def _prepare(bars: list[dict]):
    import numpy as np
    import pandas as pd
    from model.kronos import calc_time_stamps

    rows = []
    times = []
    for bar in bars:
        close = float(bar["c"])
        volume = float(bar.get("v") or 0)
        amount = bar.get("amount")
        if amount is None:
            amount = volume * close
        rows.append([float(bar["o"]), float(bar["h"]), float(bar["l"]), close, volume, float(amount)])
        times.append(bar.get("t"))
    values = np.asarray(rows, dtype="float32")
    mean = values.mean(axis=0)
    std = values.std(axis=0)
    normal = np.clip((values - mean) / (std + 1e-5), -5, 5).astype("float32")
    stamps = calc_time_stamps(_clock(times)).values.astype("float32")
    return normal, stamps, mean, std


def _one_path(predictor, values, stamps, y_stamp, mean, std, pred_len: int, temperature: float, top_p: float) -> list[dict]:
    import numpy as np
    import torch

    batch = values[np.newaxis, :]
    x_stamp = stamps[np.newaxis, :]
    y_batch = y_stamp[np.newaxis, :]
    with torch.no_grad():
        preds = predictor.generate(batch, x_stamp, y_batch, pred_len, temperature, 0, top_p, 1, False)
    preds = preds.squeeze(0) * (std + 1e-5) + mean
    candles = []
    for row in preds:
        open_, high, low, close = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
        volume = max(0.0, float(row[4]))
        candles.append({
            "o": open_,
            "h": max(open_, high, low, close),
            "l": min(open_, high, low, close),
            "c": close,
            "v": volume,
        })
    return candles


def _time_features(stamps):
    import numpy as np
    from model.kronos import calc_time_stamps

    return calc_time_stamps(_clock(stamps)).values.astype("float32")


def _clock(stamps):
    import pandas as pd

    parsed = [_parse(value) for value in stamps]
    return pd.Series(pd.to_datetime(parsed, utc=True)).dt.tz_convert(_EASTERN)


def _parse(value) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, timezone.utc)
    text = str(value or "").strip()
    if text.lstrip("-").isdigit():
        return _parse(float(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_EASTERN)
    return parsed.astimezone(timezone.utc)
