# Management API `/mgmt/v1` — přehled pro ladění

Interaktivní dokumentace (Swagger) běží na `/mgmt/docs`, schéma na `/mgmt/openapi.json`.
Autentizace: `Authorization: Bearer opx_…` (klíč z GUI → API klíče) nebo session cookie z GUI.

| metoda | cesta | role | popis |
|---|---|---|---|
| GET | `/health` | client | load, RAM, umístění modelu v Ollamě, souběh, verze, commit, velikost DB |
| GET | `/requests` | client | log; `limit` (≤500), `offset`, `model`, `provider`, `placement`, `status` (číslo nebo `error`), `key`, `since` (`1h`/`24h`/`7d`/ISO), `q` (fulltext), `bodies=1` (i texty) |
| GET | `/requests/{id}` | client | jeden záznam včetně textů |
| GET | `/stats?since=24h` | client | součty a průměry: celkem, po poskytovatelích, modelech, umístění, klíčích |
| GET | `/models` | client | modely dostupné přes proxy (Ollama + zapnutí poskytovatelé) a jejich `base_url` |
| GET | `/providers` | client | poskytovatelé (klíč jen maskovaný) |
| POST | `/providers` | admin | `{slug, name, kind, base_url, api_key, pricing, inject_usage, enabled}` |
| PUT | `/providers/{slug}` | admin | totéž; `api_key` vynechat = neměnit, `""` = smazat |
| DELETE | `/providers/{slug}` | admin | |
| POST | `/providers/{slug}/test` | admin | zkusí vypsat modely upstreamu |
| GET | `/keys` | admin | seznam klíčů (bez hodnot) |
| POST | `/keys` | admin | `{name, role: client|admin, allowed_providers: []}` → vrátí `key` (jen jednou) |
| DELETE | `/keys/{id}` | admin | |
| GET / PUT | `/settings` | admin | `retention_days`, `log_bodies`, `ollama_require_key` |

Inference (loguje se automaticky):

| cesta | kam jde | klíč |
|---|---|---|
| `/api/*`, `/v1/*` (kořen) | `OLLAMA_UPSTREAM` | nepovinný (nastavení `ollama_require_key`) |
| `/providers/{slug}/…` | `base_url` poskytovatele + zbytek cesty | povinný; proxy ho nahradí klíčem poskytovatele |

Chybové odpovědi proxy: `401 {"error": "missing or invalid proxy API key"}`,
`403` (klíč nemá poskytovatele povoleného), `404` (poskytovatel neexistuje),
`502 {"error": "upstream unreachable: …"}` (upstream nedostupný; zaloguje se se `status=502`).
