"""Konfigurace z prostředí. Vše ostatní (poskytovatelé, klíče, retence) žije v DB."""

import os

from . import __version__

VERSION = __version__

# kam se posílá holé Ollama API (kořen proxy)
UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://open-webui:11434").rstrip("/")
DB_PATH = os.environ.get("OLLAMA_LOG_DB", "/data/ollama_log.db")
# binární výsledky úloh (audio z TTS…) — vedle databáze, ať jsou ve stejném volume
JOBS_DIR = os.environ.get("OLLAMA_JOBS_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "jobs"))

# výchozí hodnoty nastavení; po prvním startu se dají měnit v GUI / API
LOG_BODIES_DEFAULT = "1" if os.environ.get("OLLAMA_LOG_BODIES", "1") == "1" else "0"

# počáteční admin — založí se jen do prázdné tabulky users
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin") or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DEFAULT_ADMIN_PASSWORD = "admin123"

# commit, ze kterého kontejner běží (nastavuje entrypoint)
GIT_COMMIT = os.environ.get("GIT_COMMIT", "")

# hlavičky, které se nikdy nepřenáší 1:1 mezi klientem a upstreamem
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding",
}
