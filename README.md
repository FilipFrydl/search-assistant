# Osobní research asistent (LangGraph + n8n + Open WebUI)

```
Open WebUI ──(OpenAI API)──> LangGraph backend (FastAPI, port 8000, běží na hostiteli)
                               planner → searcher → evaluator ─(nestačí, max 2×)→ planner
                                                      └─(stačí)→ writer → ask_watch (interrupt)
                                            │                          │            │
n8n:                            /webhook/search             /webhook/publish   /webhook/watch
                                SearXNG ×2 → Merge → Code   (volitelné)        + denní kontrola ceny → e-mail
                                cron 7:00 → /v1/chat/completions → e-mail (ranní digest)
```

| Složka | Obsah |
|---|---|
| `backend/llm.py` | `ask()` / `ask_json()` – přepínání `claude_code` (předplatné) ↔ `claude_api` (API klíč) |
| `backend/graph.py` | LangGraph graf, volání n8n, human-in-the-loop (sledování ceny) |
| `backend/app.py` | OpenAI-compatible API: `GET /v1/models`, `POST /v1/chat/completions` (stream i bez) |
| `docker/` | Open WebUI, n8n, SearXNG |
| `n8n/` | 4 workflow k importu do n8n |

---

## 1. Spuštění krok za krokem

### 1.1 Docker (Open WebUI, n8n, SearXNG)

Spusť aplikaci **Docker Desktop** (musí běžet, jinak příkaz `docker` neexistuje), pak:

```bash
cd docker && docker compose up -d && docker compose ps
```

- Open WebUI: http://localhost:3000 – první registrace = admin účet
- n8n: http://localhost:5678 – založ owner účet
- SearXNG: http://localhost:8888 – ověř JSON: http://localhost:8888/search?q=nike&format=json

### 1.2 Backend

```bash
cd backend
python3 -m venv .venv   # .venv už máš (Python 3.14), tenhle krok můžeš přeskočit
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Varianta A (výchozí, předplatné Pro/Max): Claude Code musí být přihlášený (`claude` → login) a **nesmí** být nastavená `ANTHROPIC_API_KEY` (backend ji pro jistotu před voláním `claude -p` z prostředí odstraní). Rychlý test:

```bash
python -c "import llm; print(llm.ask_json('Vrať JSON {\"ok\": true}'))"
```

Varianta B (API klíč): `pip install anthropic`, v `.env` nastav `LLM_BACKEND=claude_api` a `ANTHROPIC_API_KEY=...`.

Spuštění:

```bash
uvicorn app:app --port 8000 --reload
```

Kontrola: `curl localhost:8000/v1/models` vrátí model `research-assistant`.

> Dokud nemáš hotové n8n workflow `/search`, můžeš v `.env` dát `SEARCH_BACKEND=searxng` – backend pak hledá přímo v SearXNG (stejný výstup jako n8n).

### 1.3 Import n8n workflow

Buď v UI: **Workflows → ⋯ → Import from File** a vyber postupně soubory z `n8n/`, nebo z terminálu (složka je do kontejneru namountovaná):

```bash
cd docker && docker compose exec n8n n8n import:workflow --separate --input=/workflows
```

Pak v každém workflow vpravo nahoře přepni **Active** (Publish). Detaily jednotlivých flow jsou v kapitole 2.

### 1.4 Open WebUI

Připojení je nastavené přes proměnné v `docker-compose.yml` (`OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1`). Kdyby se model neobjevil: **Admin Panel → Settings → Connections → OpenAI API** → URL `http://host.docker.internal:8000/v1`, klíč cokoli → uložit. Ve výběru modelů zvol `research-assistant` a napiš:

```
Hledám Nike běžecké tenisky na asfalt, velikost EU 43, do 3 000 Kč.
Preferuju tlumení a delší vzdálenosti. Dej mi 3 nejlepší možnosti.
```

V chatu uvidíš průběh (plánování, hledání, hodnocení), tabulku top 3 a otázku „Chceš sledovat cenu…?“. Odpověz `2`, `2 do 2500` nebo `ne`.

---

## 2. n8n flow – co dělají a jak je postavit ručně

Všechny jsou v `n8n/` hotové k importu. Níže je popis, ať víš, co v nich je, a kdybys je chtěl skládat sám.

### 2.1 `1-search.json` – vyhledávání nabídek (`POST /webhook/search`)

```
Webhook /search → Rozdělit dotazy (Code) ─┬→ SearXNG – web ──────┬→ Merge → Normalizovat nabídky (Code) → Respond to Webhook
                                           └→ SearXNG – obchody ─┘
```

1. **Webhook** – HTTP Method `POST`, Path `search`, Respond: `Using 'Respond to Webhook' Node`. Přijme `{ "queries": [...], "size": "43", "max_price": 3000 }`.
2. **Code „Rozdělit dotazy“** – z `body.queries` udělá jeden item na dotaz (n8n pak každý další uzel spustí pro každý dotaz).
3. **Dvě paralelní větve HTTP Request** (GET `http://searxng:8080/search`, query `q`, `format=json`, `language=cs`). Druhá větev přidává k dotazu „cena Kč“, aby chytala e-shopy. V Settings uzlu je *On Error → Continue*, takže výpadek jednoho zdroje nezhodí celé hledání. Sem můžeš přidat třetí větev (produktový feed / API obchodu, RSS se slevami) – stačí v Merge zvýšit počet vstupů.
4. **Merge** – mode *Append*, spojí výsledky obou větví.
5. **Code „Normalizovat nabídky“** – vytáhne z textu cenu (`2 790 Kč`), odfiltruje nabídky nad rozpočet, odstraní duplicitní URL a vrátí `{ "offers": [{ title, url, snippet, price, currency, shop, size_available, source }] }`.
6. **Respond to Webhook** – *First Incoming Item*.

Test: v n8n klikni **Execute workflow** (poslouchá na `/webhook-test/search`) a v terminálu:

```bash
curl -X POST localhost:5678/webhook-test/search -H 'Content-Type: application/json' -d '{"queries":["Nike Pegasus 43 cena"],"max_price":3000}'
```

Po aktivaci workflow platí produkční adresa `/webhook/search` (tu volá backend).

### 2.2 `2-price-watch.json` – hlídání ceny

Jeden workflow, tři spouštěče (sdílí *workflow static data*, takže nepotřebuje databázi ani credentials):

```
Webhook POST /watch     → Uložit sledování
Webhook GET  /watches   → Vypsat sledování          (kontrola: curl localhost:5678/webhook/watches)
Každý den 8:00 → Sledované položky → Stáhnout stránku → Zjistit cenu → IF Zlevnilo? → Poslat e-mail
```

- `/watch` volá backend po odpovědi v chatu (`{ model, url, shop, price, currency, target_price }`).
- **Zjistit cenu** hledá cenu ve stránce produktu (JSON-LD `"price"`, `itemprop="price"`, `product:price:amount`, jinak první „xxx Kč“), porovná s minulou cenou a cílovou cenou a uloží novou.
- **IF** pustí dál jen položky s `dropped = true`.
- **Poslat e-mail**: otevři uzel, v *Credential* vytvoř **SMTP** účet (např. Gmail: `smtp.gmail.com`, port 465, SSL, heslo = *App password*) a přepiš `From`/`To`. Místo e-mailu můžeš dát uzel **Slack → Send Message**.

Pozor: static data se ukládají jen v **produkčních** během (workflow musí být Active, volá se `/webhook/...`, ne ruční Execute). Pro trvalejší úložiště vyměň Code uzly za **Data Table**, Google Sheets nebo Notion. A před stahováním stránek e-shopů zkontroluj jejich podmínky a `robots.txt`.

### 2.3 `3-morning-digest.json` – ranní digest

```
Každý den 7:00 → Témata (Code) → LangGraph (HTTP Request) → Poslat digest (e-mail)
```

- **Témata** – seznam zadání; každé je jeden item, takže n8n pro každé zavolá backend zvlášť.
- **LangGraph** – `POST http://host.docker.internal:8000/v1/chat/completions` s tělem `{ model: "research-assistant", stream: false, interactive: false, messages: [...] }`. `interactive: false` vypne otázku na sledování ceny. Timeout 15 min (`claude -p` je pomalé).
- **Poslat digest** – text je v `{{ $json.choices[0].message.content }}`; nastav SMTP credentials stejně jako výše.

Pozor na limity předplatného: každé téma = ~4–6 volání `claude -p`.

### 2.4 `4-publish.json` – uložení výsledků (volitelné)

`POST /webhook/publish` uloží posledních 50 výsledků, `GET /webhook/reports` je vypíše. Backend sem posílá každý výsledek, jen když v `.env` nastavíš `N8N_PUBLISH_URL=http://localhost:5678/webhook/publish`. Code uzel klidně nahraď uzlem Notion / Google Sheets.

### Adresy – co odkud volat

| Odkud → kam | URL |
|---|---|
| backend (hostitel) → n8n | `http://localhost:5678/webhook/...` |
| n8n → SearXNG | `http://searxng:8080` (stejná docker síť) |
| n8n / Open WebUI → backend | `http://host.docker.internal:8000` |
| ruční test webhooku | `/webhook-test/...` jen při „Execute workflow“, jinak `/webhook/...` |

---

## 3. Jak backend funguje

- **Open WebUI pomocné úlohy** (generování titulku, tagů, návrhů) chodí na stejný model; backend je pozná podle `### Task:` a odpoví hned, bez grafu a bez volání Clauda.
- **Human-in-the-loop**: uzel `ask_watch` zavolá `interrupt()`. Backend si zapamatuje `thread_id` k danému chatu (hlavička `X-OpenWebUI-Chat-Id`, zapnutá přes `ENABLE_FORWARD_USER_INFO_HEADERS`). Další zpráva typu `1`/`2 do 2500`/`ne` graf obnoví přes `Command(resume=...)`; cokoli jiného se bere jako nové zadání.
- Stav grafu je v paměti (`InMemorySaver`) – po restartu backendu rozpracovaná otázka na sledování zmizí. Pro trvalost vyměň za `SqliteSaver` (`pip install langgraph-checkpoint-sqlite`).
- Proměnné prostředí jsou popsané v `backend/.env.example`.

## 4. Řešení problémů

| Problém | Řešení |
|---|---|
| `docker: command not found` | Spusť Docker Desktop. |
| V chatu `ConnectError` / 404 na `/webhook/search` | Workflow v n8n není Active, nebo dočasně `SEARCH_BACKEND=searxng`. |
| SearXNG vrací 403 na `format=json` | V `docker/searxng/settings.yml` musí být `json` ve `search.formats`; `docker compose restart searxng`. |
| SearXNG vrací málo výsledků | Některé enginy (Google) občas blokují; v SearXNG preferences zapni další (Bing, DuckDuckGo, Brave). |
| Open WebUI hlásí timeout | `AIOHTTP_CLIENT_TIMEOUT` v compose, případně `LLM_BACKEND=claude_api` (rychlejší). |
| `Claude Code selhal` / limit | Dočerpaný limit předplatného → `LLM_BACKEND=claude_api` a restart backendu. |
