"""Run cloudflared for the desk's public address, and stop when Cloudflare has dropped that address.

A quick tunnel can be deleted on Cloudflare's side. cloudflared then retries forever with
"Unauthorized: Tunnel not found", still reports the old address, and nothing answers there.
On 2026-11-04 the emailed address was dead for a day that way. This process passes every
cloudflared line through to the container log, and exits when Cloudflare keeps refusing the
tunnel with no connection in between. Docker starts the service again, which opens a new
address, and the desk reads it from /quicktunnel, checks it, and emails it.
"""

from __future__ import annotations

import signal
import subprocess
import sys

# Refusals in a row before the address counts as gone. cloudflared retries within about 30 seconds.
GONE_AFTER = 4
COMMAND = [
    "/usr/local/bin/cloudflared", "--no-autoupdate", "tunnel",
    "--url", "http://desk:8788",
    "--proxy-keepalive-timeout", "60s",
    "--metrics", "0.0.0.0:2000",
]


def gone(line: str) -> bool:
    return "Tunnel not found" in line


def connected(line: str) -> bool:
    return "Registered tunnel connection" in line


def main() -> int:
    proc = subprocess.Popen(COMMAND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")

    def stop(signum, frame):
        proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    refused = 0
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if connected(line):
            refused = 0
        elif gone(line):
            refused += 1
            if refused >= GONE_AFTER:
                print(f"Cloudflare refused this tunnel {refused} times in a row. Stopping so the service opens a new address.", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return 1
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
