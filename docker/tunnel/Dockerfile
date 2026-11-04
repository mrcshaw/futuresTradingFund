# The desk's public address: cloudflared with a watchdog (watch.py) that stops it when
# Cloudflare has deleted the tunnel, so the service restarts with a new address.
FROM cloudflare/cloudflared:2026.9.3 AS cloudflared

FROM python:3.12.8-slim-bookworm
COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY watch.py /watch.py
CMD ["python", "-u", "/watch.py"]
