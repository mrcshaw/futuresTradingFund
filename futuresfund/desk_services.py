"""Actions the desk performs itself. The Portfolio Manager runs these without the model."""

from __future__ import annotations

from futuresfund.book import BOOK_LOCK, _account_book, _refresh_paper, account_rows, load, save
from futuresfund.live_trading import PAPER, PAPER_KRONOS, _handle, _slots


def _paper_account(name: str) -> bool:
    return str(name or "") in {PAPER, PAPER_KRONOS}


def reset_paper_account() -> str:
    """Clear the paper account after a liquidation. Live accounts are left as they are."""
    with BOOK_LOCK:
        book = load()
        positions = dict(book.get("positions") or {})
        cleared = []
        kept = {}
        for key, row in positions.items():
            if not isinstance(row, dict):
                kept[key] = row
                continue
            account = str(row.get("account") or "")
            mode = str(row.get("mode") or "")
            if _paper_account(account) or mode == PAPER:
                cleared.append(row)
                continue
            kept[key] = row
        book["positions"] = kept
        book["trades"] = [
            row for row in (book.get("trades") or [])
            if not isinstance(row, dict) or (not _paper_account(row.get("account")) and str(row.get("mode") or "") != PAPER)
        ]
        size = 50000.0
        for name in (PAPER, PAPER_KRONOS):
            state = _account_book(book, name)
            size = float(state.get("size") or size)
            state["realized"] = 0.0
            state["peak"] = size
        paper_slots = [slot for slot in _slots(book) if _paper_account(slot.get("account"))]
        for row in cleared:
            wanted = str(row.get("strategy_id") or "")
            targets = [slot for slot in paper_slots if str(slot.get("id") or "") == wanted] if wanted else []
            if not targets:
                targets = paper_slots
            entry = row.get("entry_ms")
            for slot in targets:
                if entry is None:
                    continue
                try:
                    _handle(book, slot["id"], entry)
                except (TypeError, ValueError):
                    continue
        for slot in paper_slots:
            slot["trades"] = 0
            slot["wins"] = 0
            slot["realized"] = 0.0
            slot["note"] = "Paper account reset. The open paper trade was cleared."
        _refresh_paper(book, PAPER, None)
        save(book)
        rows = [row for row in account_rows(book) if _paper_account(row.get("account"))]
        equity = float(rows[0]["equity"]) if rows else size
    contracts = sum(abs(int(row.get("contracts") or 0)) for row in cleared)
    if contracts:
        word = "contract" if contracts == 1 else "contracts"
        return (
            f"Paper account reset. Equity is ${equity:,.0f}. "
            f"Cleared {contracts} open paper {word} and the paper trade history. Live accounts were not changed."
        )
    return f"Paper account reset. Equity is ${equity:,.0f}. There was no open paper position. Live accounts were not changed."
