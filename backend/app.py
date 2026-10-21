"""OpenAI-compatible API nad LangGraph grafem (pro Open WebUI a n8n)."""

import hashlib
import json
import re
import time
import uuid

from dotenv import load_dotenv

load_dotenv()  # .env musí být načtené dřív, než llm.py a graph.py čtou proměnné

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from langgraph.types import Command  # noqa: E402

from graph import MAX_ITERATIONS, graph, parse_watch_answer  # noqa: E402

MODEL_ID = "research-assistant"

app = FastAPI()

# chat -> thread_id běhu, který čeká na odpověď uživatele (interrupt)
pending: dict[str, str] = {}


@app.get("/")
def root():
    return {"message": "Research assistant API"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [
        {"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "local"}]}


def _text(content) -> str:
    if isinstance(content, list):  # [{"type": "text", "text": "..."}, ...]
        return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
    return content or ""


def _task_reply(prompt: str) -> str:
    """Open WebUI posílá stejnému modelu i pomocné úlohy (titulek, tagy, návrhy).
    Ty nemá smysl hnát celým grafem, odpovíme hned."""
    if '"title"' in prompt:
        m = re.search(r"USER:\s*(.+)", prompt)
        words = (m.group(1) if m else "Research").split()[:6]
        return json.dumps({"title": " ".join(words)}, ensure_ascii=False)
    if '"tags"' in prompt:
        return json.dumps({"tags": ["Research"]})
    if '"follow_ups"' in prompt:
        return json.dumps({"follow_ups": []})
    if '"queries"' in prompt:
        return json.dumps({"queries": []})
    return ""


def _progress(node: str, update: dict) -> str:
    if node == "planner":
        qs = update.get("queries", [])
        return f"Plánuji dotazy ({len(qs)}): " + "; ".join(f"„{q}“" for q in qs) + "\n\n"
    if node == "searcher":
        return f"Hledám... nalezeno {len(update.get('results', []))} nových výsledků.\n\n"
    if node == "evaluator":
        if update.get("enough"):
            return "Hodnotím relevanci... OK.\n\n"
        return f"Hodnotím relevanci... nestačí: {update.get('feedback', '')}\n\n"
    if node == "writer":
        return "---\n\n" + update.get("summary", "") + "\n\n"
    if node == "ask_watch":
        w = update.get("watch")
        if not w:
            return "Dobře, nic nesleduji."
        if w.get("error"):
            return f"Sledování se nepodařilo uložit do n8n: {w['error']}"
        goal = f"cíl {w['target_price']:.0f} {w['currency']}" if w.get("target_price") else "jakýkoli pokles"
        return f"Sleduji cenu **{w['model']}** ({goal}). Až zlevní, n8n pošle upozornění."
    if node == "__interrupt__":
        return "\n".join(i.value["question"] for i in update)
    return ""


async def _run(body: dict, headers):
    """Async generátor textových kousků odpovědi."""
    messages = body.get("messages", [])
    user_msgs = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
    last = user_msgs[-1] if user_msgs else ""

    if last.lstrip().startswith("### Task:"):
        yield _task_reply(last)
        return

    chat_key = (headers.get("x-openwebui-chat-id") or body.get("chat_id")
                or hashlib.sha1((user_msgs[0] if user_msgs else "").encode()).hexdigest())

    # odpověď na otázku z interruptu, nebo nové zadání?
    thread_id = pending.pop(chat_key, None)
    resume = False
    if thread_id and len(user_msgs) > 1:
        state = graph.get_state({"configurable": {"thread_id": thread_id}})
        n_top = len(state.values.get("top") or [])
        resume = bool(state.interrupts) and parse_watch_answer(last, n_top) is not None

    if resume:
        graph_input = Command(resume=last)
    else:
        thread_id = str(uuid.uuid4())
        graph_input = {"request": last, "interactive": body.get("interactive", True),
                       "iteration": 0, "results": []}
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 4 * MAX_ITERATIONS + 10}

    try:
        async for chunk in graph.astream(graph_input, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    pending[chat_key] = thread_id
                yield _progress(node, update or {})
    except Exception as e:  # chyba se má ukázat v chatu, ne jen v logu
        yield f"\n\n**Chyba:** {type(e).__name__}: {e}"


def _chunk(cid: str, content: str | None, finish: str | None = None) -> str:
    delta = {"content": content} if content is not None else {}
    return "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }, ensure_ascii=False) + "\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    cid = f"chatcmpl-{uuid.uuid4().hex}"

    if body.get("stream"):
        async def sse():
            yield _chunk(cid, "")
            async for text in _run(body, request.headers):
                if text:
                    yield _chunk(cid, text)
            yield _chunk(cid, None, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")

    content = "".join([t async for t in _run(body, request.headers)])
    return {
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": MODEL_ID,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
