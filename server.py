"""
local-llm MCP server
---------------------
Bridges Claude Code to a local Ollama instance running on the RTX 4050 (6 GB VRAM).

Design goals:
  * Claude decides WHEN to offload (tools are opt-in), so VRAM is idle until used.
  * Tiered models: only one sits in VRAM at a time; short keep_alive frees it fast.
  * Zero third-party HTTP deps (stdlib urllib) -> only `mcp` is required.

Tiers (escalation ladder, cheapest first):
  LOCAL  (private, free, offline, runs on the 6 GB 4050):
    fast        -> qwen2.5:3b            (~2 GB, fully GPU-resident, snappy)
    code        -> qwen2.5-coder:7b      (~4.7 GB Q4, strong coding model)
    general     -> qwen2.5:7b-instruct   (~4.7 GB Q4, general text grunt-work)
  CLOUD  (Ollama-hosted, huge, metered; prompt leaves this machine):
    cloud-code  -> qwen3-coder:480b-cloud (frontier coding, no local VRAM cost)
    cloud-general -> gpt-oss:120b-cloud   (heavy reasoning over text)
"""

import json
import os
import re
import urllib.request
import urllib.error

import memory_store
import orchestrator
import reward
import reflection
import embeddings

from mcp.server.fastmcp import FastMCP

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "")
BASE = f"http://{OLLAMA_HOST}"
# How long a model stays in VRAM after its last call. Short = frees GPU quickly.
KEEP_ALIVE = os.environ.get("LOCAL_LLM_KEEP_ALIVE", "2m")
TIMEOUT = int(os.environ.get("LOCAL_LLM_TIMEOUT", "300"))

TIERS = {
    "fast": os.environ.get("LOCAL_LLM_FAST", "qwen2.5:3b"),
    "code": os.environ.get("LOCAL_LLM_CODE", "qwen2.5-coder:7b"),
    "general": os.environ.get("LOCAL_LLM_GENERAL", "qwen2.5:7b-instruct"),
    "cloud-code": os.environ.get("LOCAL_LLM_CLOUD_CODE", "qwen3-coder:480b-cloud"),
    "cloud-general": os.environ.get("LOCAL_LLM_CLOUD_GENERAL", "gpt-oss:120b-cloud"),
}
# Tiers whose ":...-cloud" model runs on Ollama's servers (data leaves the machine).
CLOUD_TIERS = {"cloud-code", "cloud-general"}

_DB_PATH = os.path.join(os.path.dirname(__file__), "memory.db")

FOOTER_PREFIX = "\n\n[interaction_id: "
_FOOTER_RE = re.compile(r"\[interaction_id: ([0-9a-f]+)\]\s*$")


def _open_db():
    return memory_store.connect(_DB_PATH, check_same_thread=True)


def with_footer(text, interaction_id):
    return "%s%s%s]" % (text, FOOTER_PREFIX, interaction_id)


def parse_interaction_id(text):
    m = _FOOTER_RE.search(text or "")
    return m.group(1) if m else None


def resolve_trilobite_model():
    try:
        tags = [m.get("name", "") for m in _get("/api/tags").get("models", [])]
    except Exception:
        tags = []
    if any(t.split(":")[0] == "trilobite" for t in tags):
        return "trilobite"
    return TIERS["code"]


def _make_generate(model, system, temperature, num_predict, num_ctx):
    def gen(prompt):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options = {"temperature": temperature, "num_predict": num_predict,
                   "num_ctx": num_ctx}
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options, "keep_alive": KEEP_ALIVE}
        out = _post("/api/chat", payload)
        return out.get("message", {}).get("content", "")
    return gen


def _generate_text(prompt, tier="fast", system="", temperature=0.2,
                   num_predict=256, num_ctx=2048):
    model = TIERS.get(tier, TIERS["fast"])
    return _make_generate(model, system, temperature, num_predict, num_ctx)(prompt)


mcp = FastMCP("local-llm")


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


@mcp.tool()
def offload(
    prompt: str,
    tier: str = "fast",
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 4096,
    learn: bool = True,
) -> str:
    """Offload a self-contained subtask to a local-GPU or Ollama-cloud model.

    Local tiers (fast/code/general) run privately on the 6 GB 4050. When learn=True
    (default) a local call is memory-augmented and captured: the response ends with
    a '[interaction_id: <id>]' footer you can pass to record_outcome once you know
    whether it compiled / passed tests. Cloud tiers never learn (data privacy).
    Set learn=False for throwaway work you don't want captured (pure text, no footer).

    Tiers: fast=3B (default), code=7B coder, general=7B instruct,
    cloud-code / cloud-general (METERED, prompt leaves this machine).
    Give a FULLY self-contained prompt (the model can't see this chat or your files).
    """
    model = TIERS.get(tier)
    if model is None:
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, ", ".join(TIERS))

    # Cloud tiers and opt-out both take the plain, non-learning path.
    if tier in CLOUD_TIERS or not learn:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options = {"temperature": temperature, "num_predict": num_predict}
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options}
        if tier not in CLOUD_TIERS:
            payload["keep_alive"] = KEEP_ALIVE
            options["num_ctx"] = num_ctx
        try:
            out = _post("/api/chat", payload)
        except urllib.error.URLError as e:
            return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                    "running? (the tray app / `ollama serve`)" % (BASE, e))
        msg = out.get("message", {}).get("content", "")
        return msg if msg else "(empty response) raw=%s" % json.dumps(out)[:500]

    # Learning path (local tiers only).
    gen = _make_generate(model, system, temperature, num_predict, num_ctx)
    conn = _open_db()
    try:
        response, iid = orchestrator.run_with_learning(conn, prompt, tier, gen)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()
    return with_footer(response, iid)


@mcp.tool()
def trilobite(
    prompt: str,
    tier: str = "code",
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 4096,
) -> str:
    """Ask 'trilobite', the local self-improving coding model, for help.

    This is the interactive front door to the same learning loop the fleet uses:
    the prompt is augmented with lessons distilled from past work, answered locally
    on the 4050, captured, and returned with a '[interaction_id: <id>]' footer.
    After you learn how it went, call record_outcome(<id>, "tests_passed" | "accepted"
    | "compiled" | "rejected" | "failed") so trilobite gets better over time.
    Defaults to the 7B coder / the 'trilobite' Ollama alias if it exists.
    """
    if tier in CLOUD_TIERS:
        return ("ERROR: trilobite is local-only (private lessons must not leave "
                "the machine). Use offload for cloud tiers.")
    if tier == "code":
        model = resolve_trilobite_model()
    else:
        model = TIERS.get(tier, resolve_trilobite_model())
    gen = _make_generate(model, system, temperature, num_predict, num_ctx)
    conn = _open_db()
    try:
        response, iid = orchestrator.run_with_learning(conn, prompt, "trilobite", gen)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()
    return with_footer(response, iid)


@mcp.tool()
def record_outcome(interaction_id: str, signal: str) -> str:
    """Feed a real-world outcome back into trilobite's learning loop.

    Call this after a trilobite/offload response once you know how it went.
    signal is one of: tests_passed, accepted, compiled, rejected, failed.
    A good outcome triggers a distilled 'lesson' that future prompts will retrieve.
    Pass the id from the '[interaction_id: <id>]' footer of the response.
    """
    if signal not in reward.VALID_SIGNALS:
        return "ERROR: unknown signal '%s'. Valid: %s." % (
            signal, ", ".join(sorted(reward.VALID_SIGNALS)))
    conn = _open_db()
    try:
        inter = memory_store.get_interaction(conn, interaction_id)
        if inter is None:
            return "ERROR: no interaction '%s' (already expired or wrong id)." % interaction_id
        r = reward.score(signal)
        memory_store.record_outcome_row(conn, interaction_id, signal, r)
        lesson_id = None
        if reward.is_good(signal):
            try:
                lesson_id = reflection.maybe_add_lesson(
                    conn, interaction_id, inter["task"], inter["response"], signal,
                    offload_fn=_generate_text, embed_fn=embeddings.embed,
                )
            except urllib.error.URLError:
                lesson_id = None
    finally:
        conn.close()
    msg = "Recorded '%s' (reward %+.2f) for %s." % (signal, r, interaction_id)
    if lesson_id:
        msg += " Distilled lesson %s." % lesson_id
    return msg


@mcp.tool()
def trilobite_stats() -> str:
    """Report what trilobite has learned so far.

    Read-only observability into the learning loop's SQLite memory: how many
    interactions have been logged, how outcomes break down by signal, and the
    most recently distilled lessons. Makes no model call and needs no Ollama —
    it only reads memory.db, so it works even if the Ollama server is down.
    """
    conn = _open_db()
    try:
        n_interactions = memory_store.count_interactions(conn)
        signal_counts = memory_store.outcome_signal_counts(conn)
        lessons = memory_store.recent_lessons(conn, limit=5)
        n_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    finally:
        conn.close()
    n_outcomes = sum(signal_counts.values())
    signals_line = (
        ", ".join("%s=%d" % (sig, n) for sig, n in sorted(signal_counts.items()))
        if signal_counts else "(none yet)"
    )
    lines = [
        "trilobite learning stats",
        "  lessons: %d" % n_lessons,
        "  interactions: %d | outcomes: %d" % (n_interactions, n_outcomes),
        "  outcomes by signal: %s" % signals_line,
    ]
    if lessons:
        lines.append("  recent lessons:")
        for l in lessons:
            lines.append("    - %s" % l["text"])
    else:
        lines.append("  recent lessons: (none yet)")
    return "\n".join(lines)


@mcp.tool()
def status() -> str:
    """Report local-LLM state: which models are installed, and which are currently in VRAM.

    Use this to check whether the GPU is busy before offloading, or to confirm models pulled.
    """
    try:
        tags = _get("/api/tags").get("models", [])
        ps = _get("/api/ps").get("models", [])
    except urllib.error.URLError as e:
        return f"ERROR contacting Ollama at {BASE}: {e}"

    installed = sorted(m.get("name", "?") for m in tags)
    loaded = [
        f"{m.get('name')} (VRAM ~{round(m.get('size_vram', 0)/1e9, 1)} GB)" for m in ps
    ]
    tier_lines = [
        f"  {k}={v}" + ("  [CLOUD — leaves machine]" if k in CLOUD_TIERS else "  [local GPU]")
        for k, v in TIERS.items()
    ]
    lines = [
        f"Ollama @ {BASE}",
        "Tiers:",
        *tier_lines,
        f"Installed/registered models: {', '.join(installed) if installed else '(none)'}",
        f"In VRAM now: {', '.join(loaded) if loaded else '(none — GPU idle)'}",
        f"local keep_alive: {KEEP_ALIVE}",
    ]
    return "\n".join(lines)


@mcp.tool()
def unload(tier: str = "all") -> str:
    """Immediately free GPU VRAM by unloading a model (or all of them).

    Args:
        tier: "all" (default), or one of "fast", "code", "general".
    """
    if tier == "all":
        # Only local tiers occupy VRAM; cloud tiers run remote.
        targets = [v for k, v in TIERS.items() if k not in CLOUD_TIERS]
    elif tier in CLOUD_TIERS:
        return f"'{tier}' is a cloud tier — it uses no local VRAM, nothing to unload."
    else:
        targets = [TIERS.get(tier)]
    if None in targets:
        return f"ERROR: unknown tier '{tier}'. Valid: all, {', '.join(TIERS)}."
    freed = []
    for model in targets:
        try:
            _post("/api/generate", {"model": model, "keep_alive": 0})
            freed.append(model)
        except urllib.error.URLError:
            pass
    return f"Unload requested for: {', '.join(freed) if freed else '(none)'}."


if __name__ == "__main__":
    mcp.run()
