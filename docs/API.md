# Management API `/mgmt/v1` — přehled pro ladění

Interaktivní dokumentace (Swagger) běží na `/mgmt/docs`, schéma na `/mgmt/openapi.json`.
Autentizace: `Authorization: Bearer opx_…` (klíč z GUI → API klíče) nebo session cookie z GUI.

| metoda | cesta | role | popis |
|---|---|---|---|
| GET | `/health` | client | load, RAM, umístění modelu v Ollamě, souběh, verze, commit, velikost DB |
| GET | `/requests` | client | log; `limit` (≤500), `offset`, `model`, `provider`, `placement`, `status` (číslo nebo `error`), `key`, `user` (uživatel Open WebUI), `since` (`1h`/`24h`/`7d`/ISO), `q` (fulltext), `bodies=1` (i texty) |
| GET | `/requests/{id}` | client | jeden záznam včetně textů |
| GET | `/stats?since=24h` | client | součty a průměry: celkem (`total`), `by_provider`, `by_model`, `by_placement`, `by_key`, `by_user` (hlavička Open WebUI), `by_key_model` (spotřeba aplikace na každém modelu) |
| GET | `/models` | client | modely dostupné přes proxy (Ollama + zapnutí poskytovatelé) a jejich `base_url`; ořezané na modely povolené klíči |
| GET | `/models/status` | client | plánovač lokální Ollamy: `loaded` (z `/api/ps`), `admitted` (kdo drží GPU), `in_flight`, `waiting` (fronta po modelech), `oldest_wait_s`, `draining`, `hold_s`, `max_wait_s`, `switches` |
| POST | `/jobs` | client | odložená úloha `{path, body, provider?, priority?, callback_url?, not_before?}` nebo dávka `{jobs: [...], priority?, callback_url?, not_before?}` → 202 `{id, batch_id}`; 429 při překročení `max_jobs` / `rate_per_min` klíče |
| GET | `/jobs` | client | seznam (client jen svoje); `status`, `batch`, `limit`, `offset`, `bodies=1` (i dotaz a výsledek), `finished` = kolik z vrácených je hotových |
| GET | `/jobs/{id}` | client | stav (`queued|running|done|error|cancelled`), `result` = celá odpověď upstreamu, `request_id` = záznam v logu, `callback_status` |
| DELETE | `/jobs/{id}` | client | zruší čekající i běžící úlohu |
| POST | `/models/load` | client | `{model, wait_s: 0–300, keep_alive?}` → `{loaded: bool, status: ready|queued|loading|error, admitted, hold_s, …}`; model musí být klíči povolený |
| GET | `/providers` | client | poskytovatelé (klíč jen maskovaný) |
| POST | `/providers` | admin | `{slug, name, kind, base_url, api_key, pricing, inject_usage, enabled}` |
| PUT | `/providers/{slug}` | admin | totéž; `api_key` vynechat = neměnit, `""` = smazat |
| DELETE | `/providers/{slug}` | admin | |
| POST | `/providers/{slug}/test` | admin | zkusí vypsat modely upstreamu |
| GET | `/keys` | admin | seznam klíčů (bez hodnot) |
| POST | `/keys` | admin | `{name, role: client|admin, allowed_providers: [], allowed_models: [], max_jobs: 0, rate_per_min: 0}` → vrátí `key` (jen jednou); `allowed_models` = glob vzory (`gemma4`, `qwen3*`), prázdné = všechny |
| PUT | `/keys/{id}` | admin | `{allowed_models?, max_jobs?, rate_per_min?}` — změna omezení existujícího klíče (0 = výchozí z nastavení) |
| PUT | `/keys/{id}/models` | admin | `{allowed_models: []}` — jen seznam modelů |
| DELETE | `/keys/{id}` | admin | |
| GET / PUT | `/settings` | admin | `retention_days`, `log_bodies`, `ollama_require_key`, `sched_enabled`, `sched_hold_s`, `sched_max_wait_s`, `jobs_enabled`, `jobs_max_wait_s`, `jobs_idle_s`, `jobs_preempt_s`, `jobs_max_queued`, `jobs_retention_days`, `rate_limit_per_min` |

Inference (loguje se automaticky):

| cesta | kam jde | klíč | plánovač |
|---|---|---|---|
| `/api/*`, `/v1/*` (kořen) | `OLLAMA_UPSTREAM` | nepovinný (nastavení `ollama_require_key`) | ano (POST na chat/generate/embed) |
| `/providers/{slug}/…` | `base_url` poskytovatele + zbytek cesty | povinný; proxy ho nahradí klíčem poskytovatele | ne |

Hlavička `X-Opx-Wait: <s>` omezí čekání v plánovači (0 = nečekat); do upstreamu se nepřeposílá.
Klíč s `allowed_models` vidí v `/api/tags`, `/v1/models` a `/v1beta/models` jen povolené modely.

Chybové odpovědi proxy: `401 {"error": "missing or invalid proxy API key"}`,
`403` (klíč nemá poskytovatele povoleného, nebo `model 'x' is not allowed for key 'y'` — zaloguje se se `status=403`),
`404` (poskytovatel neexistuje),
`502 {"error": "upstream unreachable: …"}` (upstream nedostupný; zaloguje se se `status=502`),
`503 {"error": "model 'x' is not loaded; GPU is held by 'y'", "scheduler": {…}}` + `Retry-After`
(vypršel `X-Opx-Wait`; zaloguje se se `status=503`),
`429 {"error": "rate limit N/min exceeded for key 'y'"}` + `Retry-After` (zaloguje se se `status=429`).
Čekání v plánovači je v logu jako `queue_ms`, dotaz vzniklý z úlohy má `job_id`.
