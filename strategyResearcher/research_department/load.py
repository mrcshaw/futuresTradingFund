"""Load one script into a department that is already running."""

from __future__ import annotations

import os
import sys
import time

from research_department.worker import Worker


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m research_department.load <file> <instrument>")
    worker = Worker(os.environ.get("RESEARCH_URL", "http://127.0.0.1:8790"))
    accepted = worker.submit(sys.argv[1], sys.argv[2])
    if accepted.get("refused"):
        raise SystemExit(accepted.get("reason") or "The department refused the file.")
    seen = None
    while not worker.finished:
        body = worker.poll()
        state = body.get("state")
        if state != seen:
            seen = state
            print(state, flush=True)
        if not worker.finished:
            time.sleep(1)
    if worker.accepted_script:
        print(worker.accepted_script, end="" if worker.accepted_script.endswith("\n") else "\n")


if __name__ == "__main__":
    main()
