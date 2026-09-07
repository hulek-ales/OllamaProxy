# Obraz nese jen toolchain (Python + git + závislosti). Vlastní kód se za běhu
# naklonuje z Gitu do volume /app/src a aktualizuje přes `git pull` při každém
# restartu kontejneru (self-update stejně jako Výukový portál).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && sha256sum /app/requirements.txt | cut -c1-16 > /app/.req-stamp

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /app/src /data

ENV REPO_URL=https://github.com/hulek-ales/OllamaProxy.git \
    REPO_BRANCH=main \
    UPDATE_ON_START=true \
    OLLAMA_UPSTREAM=http://open-webui:11434 \
    OLLAMA_LOG_DB=/data/ollama_log.db \
    OLLAMA_LOG_BODIES=1 \
    PROXY_PORT=11435 \
    PYTHONUNBUFFERED=1

WORKDIR /app/src
EXPOSE 11435
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://localhost:11435/healthz > /dev/null || exit 1
ENTRYPOINT ["/entrypoint.sh"]
