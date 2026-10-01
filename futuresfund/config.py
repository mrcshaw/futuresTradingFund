"""Settings for the futures desk. Secrets stay in the environment."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_HOME = Path.home() / ".futuresfund"

BOOK_PATH = _HOME / "futures_fund.json"
BARS_PATH = _HOME / "futures_bars.json"
LIVE_BARS_PATH = _HOME / "futures_live_bars.json"
LAB_PATH = _HOME / "futures_lab.json"


def _read_env_file(path: Path, *, overwrite: bool) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key or (not overwrite and key in os.environ):
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def load_env() -> None:
    """Load this desk's own settings."""
    _read_env_file(ROOT / ".env", overwrite=True)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def dry_run() -> bool:
    return _flag("DRY_RUN", True)


def meeting_trades() -> bool:
    return _flag("MEETING_TRADES", False)


def max_qty() -> int:
    try:
        return max(1, int(os.environ.get("FUTURES_MAX_QTY", "5")))
    except ValueError:
        return 5


def prop_accounts() -> set[str]:
    raw = os.environ.get("PROP_ACCOUNTS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def webhook_token() -> str:
    return os.environ.get("TV_WEBHOOK_TOKEN", "").strip()


def crosstrade_url() -> str:
    return os.environ.get("CROSSTRADE_WEBHOOK_URL", "").strip()


def crosstrade_key() -> str:
    return os.environ.get("CROSSTRADE_KEY", "").strip()


def public_settings() -> dict:
    return {
        "dry_run": dry_run(),
        "meeting_trades": meeting_trades(),
        "max_qty": max_qty(),
        "accounts": sorted(prop_accounts()),
        "crosstrade_configured": bool(crosstrade_url() and crosstrade_key()),
        "webhook_configured": bool(webhook_token()),
        "nt_configured": bool(os.environ.get("CROSSTRADE_API_TOKEN", "").strip() or crosstrade_key()),
        "mail_webhook_configured": bool(os.environ.get("MAIL_WEBHOOK_TOKEN", "").strip()),
    }
