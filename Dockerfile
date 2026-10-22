# Long-running Wallex trader for Liara's Docker platform.
# The process listens on 8080 for /health and a read-only desk, and runs the loop beside it.
# Secrets are not baked in. Set them with `liara env set`.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tehran \
    TRADER_MODE=shadow \
    TRADER_DATA_DIR=/data \
    TRADER_HEALTH_PORT=8080 \
    TRADER_HEALTH_HOST=0.0.0.0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trader trader
COPY config config

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080

# Shadow is the default. It uses public market data and does not send orders.
# Live orders require TRADER_MODE=live and WALLEX_API_KEY from the environment.
CMD ["python", "-m", "trader", "--config", "config/default.yaml"]
