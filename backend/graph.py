"""LangGraph: planner -> searcher -> evaluator -(retry)-> planner / writer -> ask_watch."""

import json
import operator
import os
import re
from typing import Annotated, TypedDict

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from llm import ask_json

N8N_URL = os.getenv("N8N_URL", "http://localhost:5678")
N8N_SEARCH_URL = os.getenv("N8N_SEARCH_URL", f"{N8N_URL}/webhook/search")
N8N_WATCH_URL = os.getenv("N8N_WATCH_URL", f"{N8N_URL}/webhook/watch")
N8N_PUBLISH_URL = os.getenv("N8N_PUBLISH_URL", "")  # prázdné = nepublikovat
SEARCH_BACKEND = os.getenv("SEARCH_BACKEND", "n8n")  # n8n | searxng (bez n8n, pro ladění)
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "2"))

SYSTEM = ("Jsi research asistent. Pracuješ česky, věcně a nevymýšlíš si fakta, "
          "ceny ani odkazy - vycházíš jen z dodaných výsledků.")


class State(TypedDict, total=False):
    request: str
    interactive: bool          # False = bez dotazu na sledování ceny (cron, n8n)
    criteria: dict             # parametry vytažené ze zadání (velikost, rozpočet...)
    queries: list[str]
    results: Annotated[list[dict], operator.add]  # sbírá se přes všechny iterace
    iteration: int
    enough: bool
    feedback: str              # co hledat jinak při dalším kole
    top: list[dict]
    summary: str
    watch: dict | None


# ---------- pomocné funkce ----------

def parse_price(text: str) -> float | None:
    """'2 790 Kč', '2.790,-', '2790 CZK' -> 2790.0"""
    m = re.search(r"(\d{1,3}(?:[  .]\d{3})+|\d{3,6})(?:,\d{1,2}|,-)?\s*(?:Kč|CZK|,-)",
                  text or "", re.I)
    if not m:
        return None
    return float(re.sub(r"[  .]", "", m.group(1)))


def _format_results(results: list[dict], limit: int = 30) -> str:
    lines = []
    for i, r in enumerate(results[:limit]):
        price = f"{r['price']:.0f} {r.get('currency') or 'CZK'}" if r.get("price") else "cena ?"
        lines.append(f"[{i}] {r.get('title', '')} | {price} | {r.get('shop', '')} | "
                     f"{r.get('url', '')}\n    {(r.get('snippet') or '')[:200]}")
    return "\n".join(lines) or "(nic)"


def _fmt_price(r: dict) -> str:
    if not r.get("price"):
        return "?"
    cur = r.get("currency") or "CZK"
    num = f"{r['price']:,.0f}".replace(",", " ")
    return f"{num} Kč" if cur == "CZK" else f"{num} {cur}"


def _search_n8n(payload: dict) -> list[dict]:
    r = httpx.post(N8N_SEARCH_URL, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):  # Respond to Webhook může vrátit pole
        data = data[0] if data else {}
    return data.get("offers", [])


def _search_searxng(payload: dict) -> list[dict]:
    """Stejný výstup jako n8n workflow /search, ale přímo ze SearXNG."""
    offers = []
    for q in payload["queries"]:
        r = httpx.get(f"{SEARXNG_URL}/search",
                      params={"q": q, "format": "json", "language": "cs"}, timeout=30)
        r.raise_for_status()
        for hit in r.json().get("results", [])[:10]:
            text = f"{hit.get('title', '')} {hit.get('content', '')}"
            price = parse_price(text)
            if price and payload.get("max_price") and price > payload["max_price"]:
                continue
            offers.append({
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "snippet": hit.get("content", ""),
                "price": price,
                "currency": "CZK" if price else None,
                "shop": httpx.URL(hit["url"]).host if hit.get("url") else "",
                "size_available": None,
                "source": "searxng",
            })
    return offers


# ---------- uzly ----------

def planner(s: State) -> dict:
    it = s.get("iteration", 0)
    prompt = f"Zadání uživatele:\n{s['request']}\n\n"
    if it:
        prompt += (f"Už jsme hledali: {json.dumps(s.get('queries', []), ensure_ascii=False)}\n"
                   f"Hodnotitel říká, že to nestačí: {s.get('feedback', '')}\n"
                   "Navrhni NOVÉ, upřesněné nebo rozšířené dotazy.\n\n")
    prompt += (
        "Vytáhni ze zadání parametry a navrhni 3 až 5 vyhledávacích dotazů "
        "(v jazyce, ve kterém se dané věci nejlépe hledají; u nákupů v ČR česky).\n"
        'Vrať JSON {"criteria": {"topic": "...", "brand": null, "size": null, '
        '"max_price": null, "currency": "CZK", "must_have": ["..."]}, '
        '"queries": ["...", "..."]}. Nepoužité parametry nech null.'
    )
    data = ask_json(prompt, SYSTEM)
    out = {"queries": [q for q in data.get("queries", []) if q][:5], "iteration": it + 1}
    if not s.get("criteria"):
        out["criteria"] = data.get("criteria") or {}
    return out


def searcher(s: State) -> dict:
    c = s.get("criteria") or {}
    payload = {"queries": s["queries"], "size": c.get("size"),
               "max_price": c.get("max_price"), "currency": c.get("currency") or "CZK"}
    offers = _search_n8n(payload) if SEARCH_BACKEND == "n8n" else _search_searxng(payload)
    seen = {r.get("url") for r in s.get("results", [])}
    new = []
    for o in offers:
        if o.get("url") and o["url"] not in seen:
            seen.add(o["url"])
            new.append(o)
    return {"results": new}


def evaluator(s: State) -> dict:
    results = s.get("results", [])
    if not results:
        return {"enough": False, "feedback": "Nic se nenašlo, zkus obecnější dotazy."}
    data = ask_json(
        f"Zadání: {s['request']}\n"
        f"Kritéria: {json.dumps(s.get('criteria', {}), ensure_ascii=False)}\n\n"
        f"Nalezené výsledky ({len(results)}):\n{_format_results(results)}\n\n"
        "Stačí tyto výsledky na kvalitní odpověď? U nákupu: aspoň 3 různé relevantní "
        "produkty s cenou a odkazem v rozpočtu. U obecného tématu: aspoň 5 relevantních zdrojů.\n"
        'Vrať JSON {"enough": true/false, "feedback": "co chybí a jak hledat jinak"}.',
        SYSTEM,
    )
    return {"enough": bool(data.get("enough")), "feedback": data.get("feedback", "")}


def route(s: State) -> str:
    return "write" if s.get("enough") or s.get("iteration", 0) >= MAX_ITERATIONS else "retry"


def writer(s: State) -> dict:
    results = s.get("results", [])[:40]
    c = s.get("criteria") or {}
    data = ask_json(
        f"Zadání: {s['request']}\n"
        f"Kritéria: {json.dumps(c, ensure_ascii=False)}\n\n"
        f"Výsledky:\n{_format_results(results, 40)}\n\n"
        "Pokud jde o nákup produktu, vyber nejvýš 3 nejlepší nabídky (různé modely). "
        "Pokud jde o obecné téma, nech top prázdné.\n"
        'Vrať JSON {"intro": "1-2 věty", '
        '"top": [{"i": <index výsledku>, "name": "krátký název modelu", "reason": "proč"}], '
        '"summary": "shrnutí v markdownu; zdroje cituj jako [index]"}',
        SYSTEM,
    )

    top = []
    for t in data.get("top", [])[:3]:
        i = t.get("i")
        if isinstance(i, int) and 0 <= i < len(results):
            top.append({**results[i], "name": t.get("name") or results[i].get("title"),
                        "reason": t.get("reason", "")})

    parts = [data.get("intro", "").strip()]
    if top:
        size = c.get("size")
        parts.append(f"| # | Model | Cena | Obchod | {'Vel. ' + str(size) if size else 'Dostupnost'} |\n"
                     "|---|---|---|---|---|")
        for n, t in enumerate(top, 1):
            avail = {True: "ano", False: "ne"}.get(t.get("size_available"), "?")
            parts[-1] += (f"\n| {n} | [{t['name']}]({t.get('url', '')}) | {_fmt_price(t)} | "
                          f"{t.get('shop', '')} | {avail} |")
        parts.append("\n".join(f"{n}. **{t['name']}** - {t['reason']}"
                               for n, t in enumerate(top, 1)))
    if data.get("summary"):
        parts.append(data["summary"].strip())

    # [3] v textu -> odkaz na zdroj
    cited = sorted({int(m) for m in re.findall(r"\[(\d+)\]", "\n".join(parts))
                    if int(m) < len(results)})
    if cited:
        parts.append("**Zdroje**\n" + "\n".join(
            f"- [{i}] [{results[i].get('title') or results[i]['url']}]({results[i]['url']})"
            for i in cited))
    summary = "\n\n".join(p for p in parts if p)

    if N8N_PUBLISH_URL:
        try:
            httpx.post(N8N_PUBLISH_URL, json={"request": s["request"], "summary": summary,
                                              "top": top}, timeout=30)
        except httpx.HTTPError:
            pass  # publikace je jen bonus, odpověď uživateli kvůli ní neshazujeme
    return {"top": top, "summary": summary}


NO_WORDS = {"ne", "no", "nic", "zadny", "žádný", "nesleduj", "n"}


def parse_watch_answer(answer: str, n: int) -> tuple[str, int | None, float | None] | None:
    """'2' -> ('pick', 1, None), '2 do 2500' -> ('pick', 1, 2500), 'ne' -> ('no', None, None).
    None = odpověď nevypadá jako odpověď na otázku (bereme ji jako nové zadání)."""
    text = answer.strip().lower().rstrip(".!")
    if text in NO_WORDS:
        return ("no", None, None)
    m = re.fullmatch(r"(?:č\.?\s*|cislo\s*|číslo\s*)?([1-9])(?:\D+(\d[\d  ]*))?(?:\s*(?:kč|czk))?",
                     text)
    if m and 1 <= int(m.group(1)) <= n:
        target = float(re.sub(r"\s", "", m.group(2))) if m.group(2) else None
        return ("pick", int(m.group(1)) - 1, target)
    return None


def ask_watch(s: State) -> dict:
    top = s.get("top") or []
    if not top or not s.get("interactive", True):
        return {"watch": None}
    options = " / ".join(str(n) for n in range(1, len(top) + 1))
    answer = interrupt({"question": f"Chceš sledovat cenu některého modelu? ({options} / ne)\n"
                                    "Můžeš přidat cílovou cenu, např. `2 do 2500`."})
    parsed = parse_watch_answer(str(answer), len(top)) or ("no", None, None)
    if parsed[0] == "no":
        return {"watch": None}
    item = top[parsed[1]]
    watch = {"model": item["name"], "url": item.get("url"), "shop": item.get("shop"),
             "price": item.get("price"), "currency": item.get("currency") or "CZK",
             "target_price": parsed[2]}  # None = hlídat jakýkoli pokles
    try:
        httpx.post(N8N_WATCH_URL, json=watch, timeout=30).raise_for_status()
    except httpx.HTTPError as e:
        watch["error"] = str(e)
    return {"watch": watch}


g = StateGraph(State)
g.add_node("planner", planner)
g.add_node("searcher", searcher)
g.add_node("evaluator", evaluator)
g.add_node("writer", writer)
g.add_node("ask_watch", ask_watch)
g.add_edge(START, "planner")
g.add_edge("planner", "searcher")
g.add_edge("searcher", "evaluator")
g.add_conditional_edges("evaluator", route, {"retry": "planner", "write": "writer"})
g.add_edge("writer", "ask_watch")
g.add_edge("ask_watch", END)
# checkpointer je nutný pro interrupt (human-in-the-loop); stav drží v paměti procesu
graph = g.compile(checkpointer=InMemorySaver())
