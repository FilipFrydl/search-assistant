"""Jednotné rozhraní k LLM. Graf volá jen ask() / ask_json().

Backend se přepíná proměnnou prostředí LLM_BACKEND:
  claude_code  - `claude -p` přes předplatné Pro/Max (výchozí)
  claude_api   - Claude API přes ANTHROPIC_API_KEY
"""

import json
import os
import re
import subprocess
import tempfile

BACKEND = os.getenv("LLM_BACKEND", "claude_code")  # claude_code | claude_api
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_CODE_TIMEOUT = int(os.getenv("CLAUDE_CODE_TIMEOUT", "180"))


def _claude_code(prompt: str, system: str = "") -> str:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--max-turns", "1"]
    if system:
        cmd += ["--append-system-prompt", system]
    # ANTHROPIC_API_KEY by přepnul Claude Code na placené API místo předplatného
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    # prázdný pracovní adresář, aby si CLI nenačítalo CLAUDE.md a soubory projektu
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=CLAUDE_CODE_TIMEOUT, env=env, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"Claude Code selhal: {r.stderr or r.stdout}")
    data = json.loads(r.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"Claude Code vrátil chybu: {data.get('result')}")
    return data["result"]


def _claude_api(prompt: str, system: str = "") -> str:
    from anthropic import Anthropic
    client = Anthropic()  # čte ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=system or "Jsi užitečný asistent.",
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def ask(prompt: str, system: str = "") -> str:
    if BACKEND == "claude_api":
        return _claude_api(prompt, system)
    return _claude_code(prompt, system)


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    # model občas obalí JSON do bloku s trojitými zpětnými apostrofy
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # nebo před/za JSON přidá větu textu
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(raw[start:end + 1])


def ask_json(prompt: str, system: str = "", retries: int = 1) -> dict:
    prompt += "\n\nOdpověz POUZE validním JSON, bez dalšího textu."
    for attempt in range(retries + 1):
        raw = ask(prompt, system)
        try:
            return _extract_json(raw)
        except json.JSONDecodeError:
            if attempt == retries:
                raise
