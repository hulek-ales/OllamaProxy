#!/usr/bin/env bash
# Start kontejneru: klon/pull kódu z Gitu do volume → doinstalovat závislosti,
# pokud se změnily → spustit uvicorn.
# Self-update: restart kontejneru (nebo appky v TrueNAS) udělá `git pull`.
set -uo pipefail

REPO_URL="${REPO_URL:-https://github.com/hulek-ales/OllamaProxy.git}"
BRANCH="${REPO_BRANCH:-main}"
UPDATE_ON_START="${UPDATE_ON_START:-true}"
PORT="${PROXY_PORT:-11435}"
SRC=/app/src
mkdir -p "$SRC"
cd "$SRC"

if [ ! -d .git ]; then
  echo "[init] klonuji ${REPO_URL} (${BRANCH})"
  git clone --branch "${BRANCH}" "${REPO_URL}" /tmp/repo \
    && cp -a /tmp/repo/. "$SRC"/ && rm -rf /tmp/repo \
    || { echo "[init] klon selhal"; sleep 10; exit 1; }
fi
git config --global --add safe.directory "$SRC"

if [ "${UPDATE_ON_START}" = "true" ]; then
  before="$(git rev-parse HEAD 2>/dev/null || echo none)"
  git fetch origin "${BRANCH}" 2>&1 || echo "[git] fetch přeskočen (offline?)"
  git checkout "${BRANCH}" 2>/dev/null || true
  git pull --ff-only origin "${BRANCH}" 2>&1 || echo "[git] pull přeskočen"
  after="$(git rev-parse HEAD 2>/dev/null || echo none)"
  [ "$before" != "$after" ] && echo "[git] aktualizováno ${before:0:7} → ${after:0:7}"
fi

# závislosti: image je má z buildu; doinstalují se jen když se requirements.txt změnil
if [ -f requirements.txt ]; then
  sum="$(sha256sum requirements.txt | cut -c1-16)"
  if [ "$(cat /app/.req-stamp 2>/dev/null)" != "$sum" ]; then
    echo "[pip] requirements.txt se změnil, instaluji"
    pip install --no-cache-dir -q -r requirements.txt \
      && echo "$sum" > /app/.req-stamp \
      || echo "[pip] instalace selhala, jedu s tím, co je v image"
  fi
fi

export GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo '')"
echo "[run] start proxy na :${PORT} (commit ${GIT_COMMIT:-?}, upstream ${OLLAMA_UPSTREAM:-?})"
exec python -m uvicorn ollamaproxy.main:app --host 0.0.0.0 --port "${PORT}" \
  --proxy-headers --forwarded-allow-ips='*'
