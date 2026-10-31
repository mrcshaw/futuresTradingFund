# The futures desk on Linux. The code and data are mounted from the project folder,
# so this image holds only Python and Node for PineTS. The public address is the tunnel service in docker-compose.yml.
FROM node:22.11.0-bookworm-slim AS node

FROM python:3.12.8-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/New_York

WORKDIR /app
EXPOSE 8788

HEALTHCHECK --interval=60s --timeout=20s --start-period=600s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/api/settings', timeout=15)"

CMD ["python", "-m", "uvicorn", "futuresfund.server:app", "--host", "0.0.0.0", "--port", "8788", "--timeout-keep-alive", "120"]
