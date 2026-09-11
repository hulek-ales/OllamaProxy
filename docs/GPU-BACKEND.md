# GPU služba vedle Ollamy (TTS, Whisper, …) — kontrakt pro kontejner

Na jedné kartě (12 GB) se velký jazykový model a syntéza řeči nevejdou najednou.
Proxy proto o kartu **rozhoduje sama**: GPU služba je pro ni další *backend* se
svými modely, stejný plánovač řadí dotazy na Ollamu i na službu, a před přepnutím
z jednoho na druhý tomu starému řekne, ať uvolní VRAM, a počká, až ji opravdu uvolní.

```
agent ──► POST /v1/audio/speech {"model": "tts-cs", …} ──► ollama-proxy
                                                              │ model tts-cs patří službě "tts"
                                                              │ 1. dotazy na Ollamu se řadí do fronty
                                                              │ 2. Ollama: keep_alive 0, čekat na prázdné /api/ps
                                                              │ 3. ──► http://tts:8000/v1/audio/speech
                                                              │ 4. další dotaz na gemma4 → POST tts:8000/api/unload,
                                                              │    čekat na prázdné /api/ps, pak Ollama
```

Agent tedy nepotřebuje vědět nic o kartě: posílá dotazy na proxy jako na kteroukoli
jinou službu, jen s názvem modelu. Pořadí, uvolňování paměti a čekání řeší proxy.

## Co musí služba umět

Čtyři koncové body. Tvar je schválně stejný jako u Ollamy (`/api/ps`) a OpenAI
(`/v1/models`, `/v1/audio/speech`), ať jde použít i hotový server, když ho umí.

| metoda | cesta | tělo | odpověď | k čemu |
|---|---|---|---|---|
| GET | `/v1/models` | — | `{"data": [{"id": "tts-cs"}]}` | seznam modelů; tlačítko „Otestovat“ a `/mgmt/v1/models` |
| GET | `/api/ps` | — | `{"models": [{"name": "tts-cs"}]}` nebo `{"models": []}` | **pravdivý** stav VRAM; proxy podle něj pozná, že je karta volná |
| POST | `/api/unload` | `{}` nebo `{"model": "tts-cs"}` | `200` až po uvolnění | uvolni VRAM (bez modelu = všechno); idempotentní |
| POST | `/api/load` | `{"model": "tts-cs"}` | `200` po nahrání, nebo `404` | nepovinné; bez něj model nahraje první dotaz (`/mgmt/v1/models/load` pak vrátí `ready` i tak) |
| POST | `/v1/audio/speech` | `{"model", "input", "voice"?, "response_format"?, "speed"?}` | audio bytes, správný `content-type` (`audio/mpeg`, `audio/wav`, …) | vlastní práce |

Inference může být i jiná cesta (`/v1/audio/transcriptions` pro Whisper…) — proxy
ji zaloguje a řadí, pokud je v těle `model`. Pro frontu úloh je zatím povolená
`/v1/audio/speech` (JSON tělo; multipart do úlohy nejde).

### Chování, na kterém proxy stojí

- **Po startu nic v paměti.** Model se nahraje při prvním dotazu (nebo `/api/load`).
  Kontejner tak může běžet pořád (pár set MB RAM, 0 VRAM) a „probuzení“ je obyčejný
  HTTP dotaz — žádná správa kontejnerů.
- **`/api/unload` vrátí 200, až když je VRAM opravdu volná** (`del model`,
  `gc.collect()`, `torch.cuda.empty_cache()`), a `/api/ps` po něm hlásí prázdno.
  Proxy po unloadu čeká na prázdné `/api/ps` nejdéle `gpu_evict_timeout_s` (60 s);
  pak přepne i tak a chybu ukáže v Nastavení / `models/status.last_evict_error`.
  Lhát v `/api/ps` znamená OOM na straně Ollamy.
- **Uvolnění po nečinnosti** je na službě (obdoba `keep_alive` Ollamy): když ji
  nikdo nevolá, ať model po pár minutách pustí sama. Proxy si pak přepnutí ušetří.
- **Jeden díl = jeden dotaz.** Dlouhý text rozděl na odstavce, syntetizuj po
  kusech, vadné kusy zopakuj a slep to **uvnitř** — ven vrať hotový soubor.
  Sto dotazů po odstavci by mezi sebou pouštělo Ollamu a model by se přehazoval.
- **Ticho na lince** delší než `gpu_request_timeout_s` (900 s) proxy vyhodnotí jako
  mrtvou službu, dotaz ukončí a kartu uvolní. Když syntéza trvá dlouho, klidně
  streamuj (chunked), stačí, aby bajty chodily.
- **Klíč** (nepovinný): když u poskytovatele vyplníš API klíč, proxy ho posílá jako
  `Authorization: Bearer …`. Klienti mají vždy jen proxy klíč `opx_…`.

## Hotový server: Chatterbox-TTS-Server

[devnen/Chatterbox-TTS-Server](https://github.com/devnen/Chatterbox-TTS-Server) (web UI,
Docker s NVIDIA, port 8004, dělení dlouhého textu a slepení uvnitř, klonování hlasu)
se s proxy domluví **bez úprav**. Má jeden model a část kontraktu jinak, proxy to zná:

| kontrakt | Chatterbox-TTS-Server | co proxy dělá |
|---|---|---|
| `GET /api/ps` | nemá → `GET /api/model-info` `{"loaded": true/false, "type": "multilingual", …}` | `loaded` = všechny modely služby z konfigurace jsou v paměti |
| `GET /v1/models` | nemá (jen `/v1/audio/voices`) | seznam modelů = ten z poskytovatele („Otestovat“ ho vrátí) |
| `POST /api/unload` | má, vrací `{"status": "unloaded"}` | stejné |
| `POST /api/load` | nemá | model nahraje první dotaz; `/mgmt/v1/models/load` vrátí `ready` |
| `POST /v1/audio/speech` | `model, input, voice, response_format (wav\|opus\|mp3), speed, seed, language` | přeposílá |

Registrace: typ gpu, adresa `http://chatterbox:8004`, modely třeba `tts-cs` (název je jen
tvůj štítek pro směrování; server pole `model` nečte). V dotazu posílej `"language": "cs"`,
`"voice": "<soubor v predefined_voices nebo reference_audio>"`, `"response_format": "mp3"`.

Pozor na češtinu: multilingual model má 23 jazyků a čeština mezi nimi není; české
váhy ([Thomcles/Chatterbox-TTS-Czech](https://huggingface.co/Thomcles/Chatterbox-TTS-Czech))
se nahrávají do multilingual modelu a server umí v `config.yaml` jen předdefinované
`model.repo_id` (`original` / `turbo` / `multilingual`) — načtení vlastních vah je
pár řádků v jeho `engine.py`. Uvolnění po nečinnosti server nemá; nevadí, o `/api/unload`
si proxy říká sama při přepnutí.

Samotná služba (kontejner s motorem) i podcastový agent žijí ve vlastním repu;
tady je jen rozhraní, které proxy vyžaduje. Rada na začátek: nejdřív motor, který
vrací ticho ve WAV, ať se cesta agent → proxy → služba → soubor odzkouší dřív,
než tam přijde Chatterbox nebo Piper.

## Registrace v proxy

GUI → Poskytovatelé → typ **Lokální GPU služba**, adresa `http://tts:8000`, modely
`tts-cs` (názvy nebo vzory, čárkou). Nebo API:

```bash
curl -H "$H" -H 'content-type: application/json' http://server:11435/mgmt/v1/providers \
  -d '{"slug":"tts","kind":"gpu","base_url":"http://tts:8000","models":["tts-cs"]}'
```

Seznam modelů je důležitý: podle něj proxy pozná, že dotaz s `"model": "tts-cs"` patří
sem, a ne do Ollamy. Do `/api/tags` se modely služby nepřidávají (Open WebUI by je
nabídlo jako chat), v `/mgmt/v1/models` jsou.

Klíč agenta: `allowed_models` musí model služby obsahovat (např. `["gemma4", "tts-cs"]`),
`allowed_providers` buď prázdné, nebo se slugem služby.

## Jak to volá agent

```python
from opx_client import OpxClient
opx = OpxClient("http://ollama-proxy:11435", "opx_…")

# hned (drží spojení; proxy mezitím uvolní Ollamu a pak zase službu)
mp3 = opx.speak("tts-cs", "Dobré ráno, tady je přehled zpráv…", voice="jirka")

# odloženě: úloha → proxy ji vyřídí, až je karta volná; výsledek je soubor
jid = opx.submit("/v1/audio/speech", {"model": "tts-cs", "input": "…"})
job = opx.wait(jid)                      # result = {"file", "content_type", "bytes"}, result_url
opx.download(jid, "/podcast/2026-09-10.mp3")
```

Průchozí dotaz: `POST http://server:11435/v1/audio/speech` (stejná adresa jako
Ollama) nebo `POST …/providers/tts/v1/audio/speech`; obojí jde přes plánovač.
V logu má dotaz `provider = tts`, do `prompt_tokens` se zapíše **počet znaků vstupu**,
takže ceník „USD za 1M znaků“ u komerčního TTS dá správnou cenu.

Síť: kontejner služby stačí pověsit na `ollamaNet` vedle proxy, port ven není
potřeba — volá ji jen proxy. Adresu (`http://tts:8000`) zadáš u poskytovatele.

## Co se děje při přepnutí (pro ladění)

`GET /mgmt/v1/models/status`:

- `admitted` + `backend` — kdo drží kartu (`gemma4:12b` / `ollama`, nebo `tts-cs` / `tts`),
- `evicting` — na který model se přepíná; starý backend zrovna uvolňuje VRAM, na kartu nejde nic,
- `loaded` — co je v paměti napříč backendy, `backends.<slug>.loaded` — po službách,
- `evictions`, `last_evict_error` — kolikrát se backend přepnul a jestli poslední uvolnění selhalo,
- `waiting` — kdo čeká (chat, který přišel uprostřed syntézy, tu uvidíš s `queue_ms` v logu).

Interaktivní dotaz, který přijde uprostřed dlouhé syntézy, **čeká** — běžící dotaz se
nepřerušuje (měkké přerušení jako u fronty úloh). Kdo čekat nechce, pošle
`X-Opx-Wait: 0` a dostane `503` + `Retry-After`.
