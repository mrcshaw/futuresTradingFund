"""Meeting model. DeepSeek-R1 32B is allowed to think. The engine numbers stay the facts."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

MODEL = "deepseek-r1-desk"
_URL = "http://127.0.0.1:11434/api/chat"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def complete(prompt: str) -> str:
    """Ask the meeting model. A failure returns a short sentence instead of taking the desk down."""
    from futuresfund.prop_rules import describe
    from futuresfund.strategy import get_strategy

    from futuresfund.strategy import lead_script

    from datetime import datetime
    from zoneinfo import ZoneInfo

    clock = datetime.now(ZoneInfo("America/New_York")).strftime("%A %I:%M %p ET")
    size = float((get_strategy() or {}).get("account_size") or 50000)
    lead = lead_script()
    if lead:
        script = (
            f"The lead strategy on headquarters is {lead['title']}: "
            f"{lead['trades']} trades, profit {lead['net_profit']}, drawdown {lead['max_drawdown']}. "
            f"{lead['formula']}\n"
            "Pine script:\n"
            f"{lead['pine']}\n"
        )
    else:
        script = "No lead strategy is on headquarters yet.\n"
    full = (
        f"Desk clock: {clock}. The strategy meeting is at 8:00am. Do not hold a meeting before then. "
        f"{describe(size)} "
        "Reason from these limits and from the numbers in the prompt. "
        "Do not invent a profit, a drawdown, or a trade count. "
        "The engine's backtest is the fact.\n"
        f"{script}"
        f"{prompt}"
    )
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": full}],
        "stream": False,
        "think": True,
        "options": {"temperature": 0.6, "num_ctx": 8192},
    }).encode()
    request = urllib.request.Request(_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return f"The meeting model could not answer: {exc}"
    message = payload.get("message") or {}
    text = str(message.get("content") or "").strip()
    if not text:
        text = str(message.get("thinking") or "").strip()
    return _THINK.sub("", text).strip()
