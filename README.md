# Ollama logging proxy

Transparentní reverse proxy před Ollamou (a volitelně před komerčními LLM API),
která streamuje odpovědi beze změny a cestou loguje do SQLite tokeny, rychlost,
cenu a stav serveru. Má vlastní webové GUI a JSON API pro ladění.

```
Open WebUI / aplikace ──► ollama-proxy :11435 ──► open-webui:11434 (Ollama)
                              │   ├─ /providers/openai/…    ──► api.openai.com     (klíč uložený v proxy)
                              │   ├─ /providers/anthropic/… ──► api.anthropic.com
                              │   └─ /providers/<slug>/…    ──► cokoli dalšího
                              ├─ /ui          GUI (přihlášení)
                              ├─ /mgmt/v1     JSON API (Bearer opx_…), dokumentace /mgmt/docs
                              └─ SQLite /data/ollama_log.db
```

## Co umí

- **Log každého dotazu**: model, vstupní/výstupní tokeny, tok/s, trvání, HTTP stav,
  chyba, text dotazu i odpovědi (vypnutelné), kdo se ptal (název API klíče).
- **Telemetrie Ollamy** v okamžiku dotazu: umístění modelu (gpu/split/cpu/none),
  % modelu ve VRAM, load hostitele, volná RAM, počet souběžných dotazů.
  Rozliší, jestli „neodpovídá“ znamená reload modelu, frontu nebo propad na CPU.
- **Proxy pro komerční API**: klíče poskytovatelů (OpenAI, Anthropic, Gemini,
  OpenRouter, Groq, …) leží jen v proxy; aplikace dostanou proxy klíč `opx_…`.
  Tokeny se čtou z OpenAI, Anthropic i Gemini formátů, volitelně se počítá cena.
- **API klíče** s rolí `client` (jen dotazy) nebo `admin` (i log a správa),
  případně omezené na konkrétní poskytovatele.
- **Self-update z Gitu**: restart kontejneru = `git pull` (viz DEPLOY-TRUENAS.md).
- Retence logu, změna hesla, vše v GUI nebo přes API.

## Rychlý start

```bash
cp .env.example .env          # uprav ADMIN_PASSWORD a OLLAMA_UPSTREAM
docker build -t ghcr.io/hulek-ales/ollamaproxy:latest .
docker compose up -d
```

GUI: `http://server:11435/ui` — účet `admin`, heslo z `ADMIN_PASSWORD`; bez něj
**`admin123`** a GUI tě upozorní, dokud ho nezměníš (Nastavení → Změna hesla).

Nasazení na TrueNAS a aktualizace z Gitu: **[DEPLOY-TRUENAS.md](DEPLOY-TRUENAS.md)**.

## Použití

### Ollama přes proxy (jako dosud)

Open WebUI → Admin → Settings → Connections → Ollama API: `http://ollama-proxy:11435`.
Holé Ollama API klíč nevyžaduje (dá se zapnout v Nastavení, pak ho musí posílat i
Open WebUI).

## Kdo kolik spotřeboval

Každá aplikace dostane vlastní klíč (GUI → API klíče, role `client`) a posílá ho
jako `Authorization: Bearer opx_…`. Proxy klíč ověří, do logu zapíše jeho název a
do Ollamy ho nepřeposílá. Stránka **Spotřeba** (`/ui/usage`, API
`/mgmt/v1/stats` → `by_key_model`) pak ukáže tokeny a cenu po aplikacích a modelech.

- **Open WebUI**: Admin → Settings → Connections → Ollama API → u adresy proxy
  vyplň klíč do pole „Key“. Open WebUI ho posílá jako `Authorization: Bearer` na
  všechna volání Ollamy včetně `/api/tags`. U OpenAI API připojení
  (`/providers/openai/v1`) je klíč povinný vždy.
- **Kdo se v Open WebUI ptal**: nastav kontejneru Open WebUI
  `ENABLE_FORWARD_USER_INFO_HEADERS=true`. Pak posílá hlavičky
  `X-OpenWebUI-User-Name/-Id/-Email/-Role`; proxy si jméno uloží (`client_user`)
  a Spotřeba ho rozepíše po uživatelích. (Má-li Open WebUI nastavený
  `FORWARD_USER_INFO_HEADER_JWT_SECRET`, posílá místo nich podepsaný JWT, který
  proxy zatím nečte.) Bez klíče se dotazy počítají jako „bez klíče“ (v logu
  s IP adresou).
- **Vynucení klíče** pro holé Ollama API zapni v Nastavení až po ověření, že
  Open WebUI s vyplněným klíčem načte modely. Jinak by přestal fungovat.

### Komerční API přes proxy

1. GUI → Poskytovatelé → přidat (slug `openai`, typ OpenAI-kompatibilní,
   adresa `https://api.openai.com`, tvůj klíč). „Otestovat“ vypíše modely.
2. GUI → API klíče → nový klíč pro aplikaci (role `client`).
3. V aplikaci:

```python
from openai import OpenAI
client = OpenAI(base_url="http://server:11435/providers/openai/v1", api_key="opx_…")

from anthropic import Anthropic
client = Anthropic(base_url="http://server:11435/providers/anthropic", api_key="opx_…")
```

Open WebUI umí totéž: Connections → OpenAI API → URL
`http://ollama-proxy:11435/providers/openai/v1`, klíč `opx_…`. Každý dotaz se
objeví v logu s názvem klíče a cenou (když je vyplněný ceník).

Typy poskytovatelů a co dělají:

| typ | hlavička s klíčem | adresa (bez `/v1`) | base_url pro aplikaci |
|---|---|---|---|
| `openai` | `Authorization: Bearer` | `https://api.openai.com`, `https://openrouter.ai/api`, `https://api.groq.com/openai` | `…/providers/<slug>/v1` |
| `anthropic` | `x-api-key` + `anthropic-version` | `https://api.anthropic.com` | `…/providers/<slug>` |
| `google` | `x-goog-api-key` | `https://generativelanguage.googleapis.com` | `…/providers/<slug>` |
| `ollama` | `Authorization: Bearer` (nepovinné) | `http://jiny-server:11434` | `…/providers/<slug>` |

U typu `openai` proxy do streamovaných dotazů doplní
`stream_options.include_usage`, jinak OpenAI tokeny ve streamu neposílá
(vypnutelné u poskytovatele, kdyby to nějaké API odmítalo).

### JSON API pro ladění (`/mgmt/v1`)

Autentizace `Authorization: Bearer opx_…`. Interaktivní dokumentace na
`/mgmt/docs`. Klíč role `client` smí číst log a statistiky; správa vyžaduje `admin`.

```bash
H='Authorization: Bearer opx_…'
curl -H "$H" 'http://server:11435/mgmt/v1/requests?since=24h&limit=20'      # log; filtry: model, provider, placement, key, status=error, q, bodies=1
curl -H "$H" 'http://server:11435/mgmt/v1/requests/123'                     # detail včetně textů
curl -H "$H" 'http://server:11435/mgmt/v1/stats?since=7d'                   # součty po poskytovatelích, modelech, umístění, klíčích
curl -H "$H" 'http://server:11435/mgmt/v1/models'                           # modely přes Ollamu i poskytovatele
curl -H "$H" 'http://server:11435/mgmt/v1/health'                           # stav (placement, load, verze, commit)
curl -H "$H" -X POST 'http://server:11435/mgmt/v1/keys' -d '{"name":"app","role":"client"}' -H 'content-type: application/json'
curl -H "$H" -X PUT  'http://server:11435/mgmt/v1/settings' -d '{"retention_days":90}' -H 'content-type: application/json'
```

## Konfigurace (env)

| Proměnná | Výchozí | Význam |
|---|---|---|
| `OLLAMA_UPSTREAM` | `http://open-webui:11434` | kam jde holé Ollama API |
| `OLLAMA_LOG_DB` | `/data/ollama_log.db` | SQLite |
| `OLLAMA_LOG_BODIES` | `1` | výchozí pro „ukládat texty“ (dál se řídí v GUI) |
| `PROXY_PORT` | `11435` | port uvicornu |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / *(prázdné = `admin123`)* | počáteční účet, jen do prázdné DB |
| `UPDATE_ON_START` / `REPO_URL` / `REPO_BRANCH` | `true` / toto repo / `main` | self-update při startu |

Vše ostatní (poskytovatelé, klíče, retence, vyžadování klíče pro Ollamu) je v DB
a nastavuje se v GUI nebo přes `/mgmt/v1`.

## Databáze

Tabulka `requests` je kompatibilní s původní verzí (nové sloupce se přidají
`ALTER TABLE` při startu, stará data zůstanou). Log úplně první proxy (tabulka
`ollama_requests` s UUID klíčem) se při startu jednorázově překopíruje do
`requests` a stará tabulka se přejmenuje na `ollama_requests_migrated`:

```
id, ts, endpoint, model, status, prompt_tokens, completion_tokens,
total_duration_ms, eval_duration_ms, tokens_per_sec, wall_time_ms,
request_json, response_text,
placement, vram_pct, loaded_model, load1, load5, mem_avail_pct, concurrent,
provider, key_name, client_ip, cost_usd, error, client_user
```

Dále `users`, `api_keys` (jen hash klíče), `providers` (klíč poskytovatele
v plaintextu — DB je ve volume, chraň ho), `settings`.

## Vývoj

```bash
pip install -r requirements-dev.txt
pytest -q
OLLAMA_UPSTREAM=http://localhost:11434 OLLAMA_LOG_DB=./dev.db ADMIN_PASSWORD=dev \
  uvicorn ollamaproxy.main:app --reload --port 11435
```

Struktura: `ollamaproxy/proxy.py` (přeposílání a log), `collector.py` (čtení
tokenů ze streamů), `providers.py` (hlavičky, modely, ceník), `mgmt.py` (JSON
API), `ui.py` + `templates/` (GUI), `db.py`, `auth.py`, `telemetry.py`.
