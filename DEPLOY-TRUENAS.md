# Nasazení na TrueNAS + self-update z Gitu

Stejný princip jako Výukový portál: image nese jen Python + git + závislosti,
vlastní kód si kontejner **naklonuje z Gitu do volume** (`/app/src`) a při
každém restartu udělá `git pull`. Nová verze = push do `main` + restart appky.

## 0. Repozitář

Kód žije v `https://github.com/hulek-ales/OllamaProxy` (musí být **public**,
kontejner klonuje bez přihlášení). Po pushi do `main` postaví GitHub Actions
image `ghcr.io/hulek-ales/ollamaproxy:latest`; aby ho TrueNAS stáhl, přepni
balíček na public: GitHub → profil → Packages → `ollamaproxy` → Package settings
→ Change visibility → Public. (Alternativa bez GHCR je v kroku 2.)

## 1. Odstranění staré appky

Původní proxy (Python zabase64ovaný v YAML + Caddy na 11435/11436) nahrazuje
tahle appka na **jednom portu 11435** — GUI má vlastní přihlášení, Caddy už
není potřeba.

1. Apps → stará ollama-proxy appka → **Stop**, pak **Delete**. Když se TrueNAS
   zeptá na volumes, můžeš je nechat smazat (začne se s prázdným logem). Pokud
   je necháš, nová appka použije stejný název `ollamaproxy_data` a historii si
   vezme: původní tabulku `ollama_requests` při prvním startu jednorázově
   překopíruje do `requests` (v logu `[db] migrováno N záznamů …`) a starou
   přejmenuje na `ollama_requests_migrated`, takže v souboru zůstane.
2. Ověř, že síť zůstala (visí na ní Open WebUI):
   ```bash
   docker network inspect ollamaNet --format '{{range .Containers}}{{.Name}} {{end}}'
   ```
   Kdyby síť chyběla: `docker network create ollamaNet` a v Open WebUI ji znovu
   přiřadit.

## 2. Instalace

**Apps → Discover Apps → ⋮ → Install via YAML**, vlož obsah **`TrueNasAPP.yaml`**
a uprav:

- `ADMIN_PASSWORD` — heslo do GUI. Prázdné = `admin` / `admin123` a GUI bude
  vyžadovat změnu.
- `OLLAMA_UPSTREAM` — `http://open-webui:11434` platí pro Open WebUI
  s vestavěnou Ollamou na síti `ollamaNet` (ověřeno v původní verzi).

Bez GHCR: vlož místo toho **`TrueNasAPP-build.yaml`** — image se postaví na
NASu (`dockerfile_inline`, trvá ~1 min). Nevýhoda: po změně `Dockerfile` nebo
`docker/entrypoint.sh` v repu se musí appka smazat a nainstalovat znovu; běžné
změny kódu se stahují restartem stejně jako u GHCR varianty.

Start sleduj v logu appky: `[init] klonuji …`, `[run] start proxy na :11435
(commit …)`, `proxy 2.0.0 ready`.

## 3. První přihlášení

1. `http://172.24.1.111:11435/ui` → `admin` + heslo.
2. Nastavení → Změna hesla (pokud jsi nechal výchozí).
3. API klíče → vytvořit klíč **claude-debug** s rolí `admin` (pro ladění
   z venku) a klíče pro jednotlivé aplikace s rolí `client`.
4. Poskytovatelé → přidat komerční API, „Otestovat“.
5. Open WebUI → Admin → Settings → Connections: Ollama API zůstává
   `http://ollama-proxy:11435`; pro komerční modely přidej OpenAI API
   `http://ollama-proxy:11435/providers/openai/v1` s proxy klíčem.

## 4. Aktualizace z Gitu

1. Push do `main` na GitHubu.
2. Apps → ollama-proxy → **Restart** (nebo `docker restart ollama-logging-proxy`).
3. Entrypoint udělá `git pull`, doinstaluje závislosti jen když se změnil
   `requirements.txt`, a nastartuje. Commit vidíš v Nastavení a v `/healthz`.

`UPDATE_ON_START: 'false'` self-update vypne; `REPO_BRANCH` přepne větev.
Změna `Dockerfile`/`entrypoint.sh`: u GHCR varianty stačí, aby doběhl workflow
a appka se restartovala (`pull_policy: always`); u build varianty přeinstalovat.

## 5. Přístup pro ladění zvenku (Claude, skripty)

Proxy poslouchá jen na LAN. Zvenku přes Cloudflare Tunnel: přidej public
hostname (např. `llm.tvoje-domena`) → `http://172.24.1.111:11435`. Zero Trust
politika s OAuth pro `/ui` je fajn, ale pro `/mgmt/*` a `/providers/*` musí
projít `Authorization: Bearer opx_…` bez přihlašovací obrazovky — buď pro tyhle
cesty udělej **Bypass** politiku (autentizaci řeší proxy klíčem), nebo
Service Token a posílej i hlavičky `CF-Access-Client-Id/Secret`. Cloudflare
utne spojení po 100 s, takže dlouhé generování streamuj (`stream: true`).

## Env proměnné

| Proměnná | Výchozí | Význam |
|---|---|---|
| `OLLAMA_UPSTREAM` | `http://open-webui:11434` | cíl holého Ollama API |
| `OLLAMA_LOG_DB` | `/data/ollama_log.db` | SQLite (volume `ollamaproxy_data`) |
| `OLLAMA_LOG_BODIES` | `1` | výchozí pro ukládání textů |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / — | poč. účet (jen do prázdné DB) |
| `UPDATE_ON_START` | `true` | `git pull` při startu |
| `REPO_URL` / `REPO_BRANCH` | repo / `main` | odkud se kód tahá |
| `PROXY_PORT` | `11435` | port uvicornu (musí sedět s `ports:`) |

## Zálohování a diagnostika

Stačí volume `ollamaproxy_data` (SQLite). Volume `ollamaproxy_code` je jen klon
repa — obnoví se sám.

```bash
docker logs --tail 100 ollama-logging-proxy
curl -s http://172.24.1.111:11435/healthz            # placement, load, verze, commit
docker run --rm -v ollamaproxy_data:/x python:3.12-slim python3 -c \
  "import sqlite3;print(sqlite3.connect('/x/ollama_log.db').execute('select count(*) from requests').fetchone())"
```

Otevřený problém s Ollamou na CPU (model v RAM místo VRAM) a jeho diagnostika
jsou v `docs/PREDANI-2026-09-07.md`; nová telemetrie (`placement`, `vram_pct`)
ho ukáže přímo v logu.
