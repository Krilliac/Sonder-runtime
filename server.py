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
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

import memory_store
import orchestrator
import retriever
import reward
import reflection
import embeddings
import personas
import recall
import summarizer
import code_runner
import live_reload
import system_profile
import emotion_vectors
import workflow_store
import web_tools
import self_heal
import grounding
import trilobite_paths
import memory_quality

from mcp.server.fastmcp import FastMCP

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "")
# OLLAMA_HOST may be set to 0.0.0.0 so `ollama serve` binds all interfaces (e.g.
# for a phone app on the LAN). 0.0.0.0 is a bind-all address, not a connectable
# one — dialing it fails on Windows (WinError 10049). Rewrite it to loopback for
# this client without disturbing the server's bind env.
# 0.0.0.0 is a bind-all server address, not a connectable client address on
# Windows. Let users bind Ollama broadly while this client dials loopback.
if OLLAMA_HOST.startswith("0.0.0.0"):
    OLLAMA_HOST = OLLAMA_HOST.replace("0.0.0.0", "127.0.0.1", 1)
BASE = f"http://{OLLAMA_HOST}"
# How long a model stays in VRAM after its last call. Short = frees GPU quickly.
KEEP_ALIVE = os.environ.get("LOCAL_LLM_KEEP_ALIVE", "2m")
TIMEOUT = int(os.environ.get("LOCAL_LLM_TIMEOUT", "300"))
LOCAL_CODE_MODEL = os.environ.get("LOCAL_LLM_CODE_LOCAL", "qwen2.5-coder:7b")


def _env_int_option(name, default=None):
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw.lower() in ("", "auto", "default", "none", "off"):
        return None
    try:
        return int(raw)
    except ValueError:
        return default


def _cpu_thread_default():
    return max(1, os.cpu_count() or 4)


def _local_model_options(temperature, num_predict, num_ctx):
    """Options sent with every local Ollama model request.

    Read env at call time so a long-running process can pick up live env patches made
    inside this Python process, and so tests can exercise the performance knobs.
    """
    options = {
        "temperature": temperature,
        "num_predict": num_predict,
        "num_ctx": num_ctx,
    }
    runtime = {
        "num_thread": _env_int_option("LOCAL_LLM_NUM_THREAD", _cpu_thread_default()),
        "num_gpu": _env_int_option("LOCAL_LLM_NUM_GPU", 999),
        "num_batch": _env_int_option("LOCAL_LLM_NUM_BATCH", 512),
    }
    for key, value in runtime.items():
        if value is not None:
            options[key] = value
    return options


def _local_runtime_summary():
    options = _local_model_options(0.2, 1, SESSION_NUM_CTX)
    return {
        "num_thread": options.get("num_thread", "ollama-default"),
        "num_gpu": options.get("num_gpu", "ollama-default"),
        "num_batch": options.get("num_batch", "ollama-default"),
    }


TIERS = {
    "fast": os.environ.get("LOCAL_LLM_FAST", "qwen2.5:3b"),
    "code": os.environ.get("LOCAL_LLM_CODE", "qwen2.5-coder:7b"),
    "general": os.environ.get("LOCAL_LLM_GENERAL", "qwen2.5:7b-instruct"),
    "cloud-code": os.environ.get("LOCAL_LLM_CLOUD_CODE", "qwen3-coder:480b-cloud"),
    "cloud-general": os.environ.get("LOCAL_LLM_CLOUD_GENERAL", "gpt-oss:120b-cloud"),
}
# Tiers whose ":...-cloud" model runs on Ollama's servers (data leaves the machine).
CLOUD_TIERS = {"cloud-code", "cloud-general"}


def _is_cloud_model_name(model):
    name = (model or "").lower()
    return "-cloud" in name or name.endswith(":cloud")


if _is_cloud_model_name(TIERS["code"]):
    TIERS["code"] = LOCAL_CODE_MODEL


def _is_cloud_tier(tier, model=None):
    if tier in CLOUD_TIERS:
        return True
    if model is None:
        model = TIERS.get(tier, "")
    return _is_cloud_model_name(model)

# Which offload tiers feed the learning loop (capture + distill lessons). A stronger
# paid/cloud model makes an excellent *teacher*: its grounded good outcomes become
# lessons and fine-tuning data the local student retrieves later. All configured tiers
# learn by default; override machine-wide with e.g. TRILOBITE_LEARN_TIERS="code"
# (local coder only) or "fast,code,general" (local-only all sizes).
DEFAULT_LEARN_TIERS = ",".join(TIERS.keys())
LEARN_TIERS = {
    t.strip()
    for t in os.environ.get(
        "TRILOBITE_LEARN_TIERS", DEFAULT_LEARN_TIERS
    ).split(",")
    if t.strip()
}

# strict=True pins trilobite to the fine-tuned alias only (errors if missing) instead
# of silently falling back to the base coder model. Env default lets ops flip this
# machine-wide without touching call sites.
_STRICT_DEFAULT = os.environ.get("TRILOBITE_STRICT", "").strip().lower() in ("1", "true", "yes", "on")

# Conversation memory is ON by default: a call with no explicit session threads the
# shared DEFAULT_SESSION so follow-ups are remembered. Pass session="none" to opt out
# (single-turn), or a distinct id to isolate a thread. Same idea for project facts.
DEFAULT_SESSION = os.environ.get("TRILOBITE_DEFAULT_SESSION", "default")
DEFAULT_PROJECT = os.environ.get("TRILOBITE_DEFAULT_PROJECT", "default")
# Sessioned calls get a roomier context (fits easily on the 6 GB 4050) and keep the
# last MAX_TURNS turns live; older turns are rolled into a summary.
SESSION_NUM_CTX = int(os.environ.get("LOCAL_LLM_SESSION_NUM_CTX", "8192"))
MAX_TURNS = int(os.environ.get("TRILOBITE_MAX_TURNS", "12"))

_DB_PATH = trilobite_paths.memory_db_path()

FOOTER_PREFIX = "\n\n[interaction_id: "
_FOOTER_RE = re.compile(r"\[interaction_id: ([0-9a-f]+)\]\s*$")
_CAMPAIGN_LEARN_LOCK = threading.Lock()

LIVE_RELOAD_MODULES = [
    "memory_store",
    "orchestrator",
    "retriever",
    "reward",
    "reflection",
    "embeddings",
    "personas",
    "recall",
    "summarizer",
    "code_runner",
    "system_profile",
    "emotion_vectors",
    "workflow_store",
    "web_tools",
    "self_heal",
    "memory_quality",
]


def _maybe_live_reload():
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    for name, module in modules.items():
        if name in globals():
            globals()[name] = module


def _open_db():
    return memory_store.connect(_DB_PATH, check_same_thread=True)


def with_footer(text, interaction_id):
    return "%s%s%s]" % (text, FOOTER_PREFIX, interaction_id)


def parse_interaction_id(text):
    m = _FOOTER_RE.search(text or "")
    return m.group(1) if m else None


TRACE_SYSTEM = (
    "Before giving your answer, output a section titled '## Reasoning' where you "
    "think step by step: restate the task in your own words, note constraints and "
    "edge cases, and explain your approach and any tradeoffs. Then output a section "
    "titled '## Answer' with the final solution."
)


def _format_trace(model, tier, params, trace):
    lessons = trace.get("lessons", [])
    lines = [
        "",
        "=== TRACE (how trilobite decided) ===",
        "model: %s   tier: %s" % (model, tier),
        "generation params: %r" % (params,),
        "lessons retrieved: %d" % len(lessons),
    ]
    for l in lessons:
        lines.append("   - %s" % l)
    lines.append("--- exact prompt sent to the model ---")
    lines.append(trace.get("augmented_prompt", ""))
    lines.append("=== END TRACE ===")
    return "\n".join(lines)


def _should_learn(tier, learn):
    # A tier feeds the learning loop when it is in LEARN_TIERS (env-configurable) and
    # the caller didn't opt out with learn=False. Defaults: local 'code' plus the
    # cloud tiers (teacher distillation); 'fast'/'general' stay mechanical.
    return bool(learn) and tier in LEARN_TIERS


def resolve_trilobite_model(strict=False):
    try:
        tags = [m.get("name", "") for m in _get("/api/tags").get("models", [])]
    except Exception:
        tags = []
    if any(t.split(":")[0] == "trilobite" for t in tags):
        return "trilobite"
    return None if strict else TIERS["code"]


def _make_generate(model, system, temperature, num_predict, num_ctx, cloud=False):
    """Build a generate(prompt, history) closure for `model`.

    cloud=True targets an Ollama-hosted model: keep_alive and num_ctx are omitted
    (they're VRAM/local-context knobs the remote tier doesn't take), matching how the
    non-learning cloud path posts.
    """
    def gen(prompt, history=None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        if cloud:
            options = {"temperature": temperature, "num_predict": num_predict}
        else:
            options = _local_model_options(temperature, num_predict, num_ctx)
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options}
        if not cloud:
            payload["keep_alive"] = KEEP_ALIVE
        out = _post("/api/chat", payload)
        return out.get("message", {}).get("content", "")
    return gen


def _no_retrieve(conn, task):
    """Retrieve hook that injects nothing — used for 'teacher' (clean) generation so a
    strong model answers at full strength without local-lesson augmentation, while its
    output is still captured for grounding + distillation."""
    return []


def _generate_text(prompt, tier="fast", system="", temperature=0.2,
                   num_predict=256, num_ctx=2048):
    model = TIERS.get(tier, TIERS["fast"])
    return _make_generate(model, system, temperature, num_predict, num_ctx)(prompt)


def _resolve_session(session):
    """"" -> DEFAULT_SESSION (memory on by default); "none" -> None (single turn)."""
    s = (session or "").strip()
    if s == "":
        return DEFAULT_SESSION
    if s.lower() == "none":
        return None
    return s


def _resolve_project(project):
    """Same convention as sessions: "" -> DEFAULT_PROJECT, "none" -> None."""
    p = (project or "").strip()
    if p == "":
        return DEFAULT_PROJECT
    if p.lower() == "none":
        return None
    return p


def _join_system_parts(*parts):
    return "\n\n".join(p for p in parts if p)


def _build_system(system, trace, persona):
    """Compose the effective system prompt from a base `system`, optional trace
    instruction, optional persona, editable profile, and emotion vectors."""
    effective_system = system
    if trace:
        effective_system = "%s\n\n%s" % (system, TRACE_SYSTEM) if system else TRACE_SYSTEM
    if persona and persona.strip():
        persona_prompt = personas.get(persona)
        effective_system = (
            "%s\n\n%s" % (persona_prompt, effective_system) if effective_system else persona_prompt
        )
    profile = system_profile.system_prompt()
    emotions = emotion_vectors.system_prompt()
    return _join_system_parts(profile, emotions, effective_system)


def _resolve_model_and_system(system, trace, strict, persona):
    """Shared prep for the trilobite tool and the serve layer.

    Returns (model, effective_system); model is None if the strict alias is missing.
    """
    strict_eff = _STRICT_DEFAULT if strict is None else strict
    model = resolve_trilobite_model(strict_eff)
    if model is None:
        return None, None
    return model, _build_system(system, trace, persona)


def _serve_target(tier, strict):
    """Resolve a serve/app request's OpenAI `model` field to a concrete target.

    Returns (model, cloud, augment, tier_label):
      - model:      the Ollama model to generate with (None if a strict alias is
                    missing, or tier_label is None for an unknown name)
      - cloud:      True if it runs on Ollama's servers (payload omits VRAM knobs)
      - augment:    inject facts/lessons/recall? Only the local student ('code'/
                    trilobite) does; any other model answers clean (teacher mode)
      - tier_label: what to record on the interaction (None => unknown model)

    Default / "" / "trilobite" / "local" => the local self-improving student.
    Any TIERS key (e.g. "cloud-code", "general") selects that model directly, so a
    single server can drive many models — pick per request.
    """
    t = (tier or "").strip().lower()
    if t in ("", "trilobite", "local"):
        strict_eff = _STRICT_DEFAULT if strict is None else strict
        return resolve_trilobite_model(strict_eff), False, True, "trilobite"
    if t in TIERS:
        model = TIERS[t]
        return model, _is_cloud_tier(t, model), t == "code", t
    return None, False, True, None


def _canonical_learn_tier(tier_label):
    """Map a recorded tier label to the LEARN_TIERS key that governs it. The local
    student is labeled 'trilobite' on interactions but is gated by the same 'code'
    switch as offload's local coder, so both flip together."""
    return "code" if tier_label == "trilobite" else tier_label


def _session_history_messages(conn, session_id, max_turns):
    """Build the prior-turn chat messages for a session, summarizing overflow.

    Turns older than the last `max_turns` are folded (once) into sessions.summary via
    the fast tier; the summary is prepended as a system message so nothing is lost.
    Summarization is best-effort: if it fails, we simply send the live turns.
    """
    turns = memory_store.session_turns(conn, session_id)
    sess = memory_store.get_session(conn, session_id) or {}
    summary = sess.get("summary")
    summarized_through = sess.get("summarized_through")

    if max_turns and len(turns) > max_turns:
        live = turns[-max_turns:]
        window_start = len(turns) - len(live)
        marker_idx = -1
        if summarized_through:
            for i, t in enumerate(turns):
                if t["id"] == summarized_through:
                    marker_idx = i
                    break
        new_overflow = turns[marker_idx + 1:window_start]
        if new_overflow:
            pairs = [(t["task"], t["response"]) for t in new_overflow]
            try:
                summary = summarizer.summarize(summary, pairs, _generate_text)
                memory_store.update_session_summary(
                    conn, session_id, summary, new_overflow[-1]["id"]
                )
            except urllib.error.URLError:
                pass  # keep prior summary; live turns still carry recent context
    else:
        live = turns

    msgs = []
    if summary:
        msgs.append({"role": "system",
                     "content": "Earlier in this conversation:\n%s" % summary})
    for t in live:
        msgs.append({"role": "user", "content": t["task"]})
        msgs.append({"role": "assistant", "content": t["response"]})
    return msgs


def _maybe_title(conn, session_id, first_prompt):
    """Give a brand-new session a short title (best-effort, never fatal)."""
    sess = memory_store.get_session(conn, session_id) or {}
    if sess.get("title"):
        return
    try:
        title = summarizer.make_title(first_prompt, _generate_text)
    except urllib.error.URLError:
        title = (first_prompt or "").strip()[:40]
    memory_store.set_session_title(conn, session_id, title)


def _answer(conn, prompt, model, effective_system, temperature, num_predict,
            num_ctx, session_id, project, history, trace=False,
            tier="trilobite", cloud=False, augment=True):
    """Core answer path shared by the tool and serve: (optionally) augment
    (facts/lessons/recall), generate with `history`, capture. Returns
    (response, interaction_id, trace_ctx).

    tier      -> recorded on the interaction (so training data knows its source).
    cloud     -> generate against an Ollama-hosted model (omit VRAM knobs).
    augment   -> False runs 'teacher' mode: no lesson/fact/recall injection (the model
                 answers clean), but the turn is still captured (with its task
                 embedding) so record_outcome can ground and distill it.
    """
    gen = _make_generate(model, effective_system, temperature, num_predict, num_ctx,
                         cloud=cloud)
    qv = embeddings.embed(prompt)
    blob = embeddings.to_blob(qv) if qv else None
    if augment:
        recalls = recall.recall(conn, prompt, qv=qv, exclude_session=session_id)
        facts = None
        if project:
            facts = [f["text"] for f in memory_store.facts_for_project(conn, project)]
        retrieve_fn = retriever.retrieve
    else:
        recalls = None
        facts = None
        retrieve_fn = _no_retrieve
    if trace:
        resp, iid, tctx = orchestrator.run_with_learning_traced(
            conn, prompt, tier, gen, retrieve_fn=retrieve_fn, history=history,
            recalls=recalls, facts=facts, session_id=session_id, task_embedding=blob,
        )
        return resp, iid, tctx
    resp, iid = orchestrator.run_with_learning(
        conn, prompt, tier, gen, retrieve_fn=retrieve_fn, history=history,
        recalls=recalls, facts=facts, session_id=session_id, task_embedding=blob,
    )
    return resp, iid, None


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

    Local tiers (fast/code/general) run privately on the 6 GB 4050. The learning tiers
    (TRILOBITE_LEARN_TIERS, default local 'code' + both cloud tiers) participate in the
    lesson loop: with learn=True (default) the call is captured and the response ends
    with a '[interaction_id: <id>]' footer you can pass to record_outcome once you know
    whether it compiled / passed tests, so a good outcome distills a lesson. The local
    'code' tier is also memory-augmented (student); cloud tiers answer CLEAN (teacher)
    but are still captured — so a paid frontier model's grounded wins become lessons and
    fine-tuning data for the local model. 'fast'/'general' (mechanical work) and
    learn=False run the plain path: no capture, no footer, just text.

    Tiers: fast=3B (default), code=7B coder, general=7B instruct,
    cloud-code / cloud-general (METERED, prompt leaves this machine).
    Give a FULLY self-contained prompt (the model can't see this chat or your files).
    """
    _maybe_live_reload()
    model = TIERS.get(tier)
    if model is None:
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, ", ".join(TIERS))

    # Only the local 'code' tier (with learn not disabled) takes the learning path.
    if not _should_learn(tier, learn):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        if tier in CLOUD_TIERS:
            options = {"temperature": temperature, "num_predict": num_predict}
        else:
            options = _local_model_options(temperature, num_predict, num_ctx)
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options}
        if not _is_cloud_tier(tier, model):
            payload["keep_alive"] = KEEP_ALIVE
        try:
            out = _post("/api/chat", payload)
        except urllib.error.URLError as e:
            return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                    "running? (the tray app / `ollama serve`)" % (BASE, e))
        msg = out.get("message", {}).get("content", "")
        return msg if msg else "(empty response) raw=%s" % json.dumps(out)[:500]

    # Learning path. Local tiers are answered by the trilobite student model/alias and
    # augmented with lessons (consistent with the trilobite tool). Cloud tiers act as a
    # 'teacher': the actual cloud model answers CLEAN (no augmentation), and its grounded
    # good outcomes are still captured + distilled into lessons for the local student.
    retrieve_kwargs = {}
    if _is_cloud_tier(tier, model):
        gen = _make_generate(model, system, temperature, num_predict, num_ctx, cloud=True)
        retrieve_kwargs["retrieve_fn"] = _no_retrieve
    else:
        learning_model = resolve_trilobite_model(_STRICT_DEFAULT)
        if learning_model is None:
            return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                    "with strict=False to fall back to the base coder.")
        gen = _make_generate(learning_model, system, temperature, num_predict, num_ctx)
    conn = _open_db()
    try:
        response, iid = orchestrator.run_with_learning(
            conn, prompt, tier, gen, **retrieve_kwargs)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()
    return with_footer(response, iid)


@mcp.tool()
def trilobite(
    prompt: str,
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 4096,
    trace: bool = False,
    strict: bool = None,
    persona: str = "",
    session: str = "",
    project: str = "",
    tier: str = "",
) -> str:
    """Ask 'trilobite', the local self-improving coding model, for help.

    This is the interactive front door to the same learning loop the fleet uses:
    the prompt is augmented with project facts, lessons distilled from past work, and
    similar past solutions, answered locally on the 4050, captured, and returned with
    a '[interaction_id: <id>]' footer. After you learn how it went, call
    record_outcome(<id>, "tests_passed" | "accepted" | "compiled" | "rejected" |
    "failed") so trilobite gets better over time. Defaults to the 7B coder base model,
    or the 'trilobite' Ollama alias if it exists.

    `tier` picks which model answers (default "" / "trilobite" = the local student).
    Pass any tier name (e.g. "cloud-code") to route this call to that model instead —
    cloud/non-student tiers answer CLEAN (teacher mode: no lesson/fact injection) but
    are still captured, so a stronger model's grounded good outcomes distill into
    lessons for the local student. Conversation memory (session) is threaded either
    way. The turn is always captured (the tool is the deliberate learning front door);
    LEARN_TIERS governs the automatic capture in offload / the serve layer instead.

    CONVERSATION MEMORY IS ON BY DEFAULT. Successive calls remember each other: with
    no `session`, the shared "default" thread is used, so follow-ups have context.
    Pass a distinct `session` id to keep an isolated thread (recommended: one id per
    conversation), or session="none" for a one-off single-turn answer. Threads persist
    in memory.db across restarts; older turns are auto-summarized to stay in the local
    context window (the most recent turns are kept verbatim). Use trilobite_sessions()
    to list threads.

    `project` scopes durable facts (see trilobite_remember_fact); those facts are
    always injected. No project -> the "default" project; project="none" -> no facts.

    trace=True instructs the model to externalize its step-by-step reasoning
    ('## Reasoning' then '## Answer'), and appends a TRACE block showing the SYSTEM's
    actual decision context (retrieved lessons, exact augmented prompt, model/params).

    strict=True (or env TRILOBITE_STRICT=1) pins this call to the fine-tuned
    'trilobite' alias only, erroring if it isn't installed instead of falling back.

    persona selects one of personas.names() (e.g. "explainer", "reviewer", "teacher")
    to steer tone; its system prompt is prepended ahead of `system`/trace instructions.
    """
    _maybe_live_reload()
    tgt_model, cloud, augment, tier_label = _serve_target(tier, strict)
    if tier_label is None:
        return "ERROR: unknown tier '%s'. Valid: trilobite, %s." % (tier, ", ".join(TIERS))
    if tgt_model is None:
        return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                "with strict=False to fall back to the base coder.")
    effective_system = _build_system(system, trace, persona)

    session_id = _resolve_session(session)
    project_id = _resolve_project(project)
    # Sessioned threads get the roomier context window; honor a larger explicit num_ctx.
    num_ctx_eff = max(num_ctx, SESSION_NUM_CTX) if session_id else num_ctx

    conn = _open_db()
    try:
        history = None
        is_first = False
        if session_id:
            is_first = memory_store.session_turn_count(conn, session_id) == 0
            memory_store.touch_session(conn, session_id, project_id)
            history = _session_history_messages(conn, session_id, MAX_TURNS)
        response, iid, trace_ctx = _answer(
            conn, prompt, tgt_model, effective_system, temperature, num_predict,
            num_ctx_eff, session_id, project_id, history, trace=trace,
            tier=tier_label, cloud=cloud, augment=augment,
        )
        if session_id and is_first:
            _maybe_title(conn, session_id, prompt)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()

    if trace:
        params = {"temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx_eff}
        trace_block = _format_trace(tgt_model, tier_label, params, trace_ctx)
        # Footer must stay LAST so parse_interaction_id's $-anchored regex still finds it.
        return with_footer(response + trace_block, iid)
    return with_footer(response, iid)


def answer_with_history(prompt, history, trace=False, strict=None, tier=None):
    """Answer a turn using caller-supplied prior `history` (list of {role, content}).

    For the OpenAI-compatible serve layer, where the chat UI owns the conversation:
    history comes from the request, not the DB, so no DB session is threaded.

    `tier` maps the request's OpenAI `model` field to a target (see _serve_target):
    default/"trilobite" is the local self-improving student (augmented with facts +
    lessons); any other tier (e.g. a paid cloud model) answers CLEAN as a teacher. The
    turn is always captured so record_outcome can ground it and distill lessons — so
    the app learns from whatever model you point it at. Returns the reply (with footer).
    """
    _maybe_live_reload()
    model, cloud, augment, tier_label = _serve_target(tier, strict)
    if tier_label is None:
        return "ERROR: unknown model '%s'. Valid: trilobite, %s." % (
            tier, ", ".join(TIERS))
    if model is None:
        return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                "with strict=False to fall back to the base coder.")
    effective_system = _build_system("", trace, "")
    # Honor LEARN_TIERS here too. Serve conversation memory is client-side (the app
    # resends history each request), so a non-learning model can skip capture entirely:
    # no interaction row, no footer, nothing distilled. This lets a user exclude e.g.
    # cloud from learning and have the app respect it. The student is gated via 'code'.
    learn = _should_learn(_canonical_learn_tier(tier_label), True)
    conn = _open_db()
    try:
        if learn:
            project = DEFAULT_PROJECT if augment else None
            response, iid, trace_ctx = _answer(
                conn, prompt, model, effective_system, 0.2, 1024, SESSION_NUM_CTX,
                None, project, history or None, trace=trace,
                tier=tier_label, cloud=cloud, augment=augment,
            )
        else:
            gen = _make_generate(model, effective_system, 0.2, 1024,
                                 SESSION_NUM_CTX, cloud=cloud)
            response = gen(prompt, history or None)
            iid, trace_ctx = None, None
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()
    if trace and trace_ctx is not None:
        params = {"temperature": 0.2, "num_predict": 1024, "num_ctx": SESSION_NUM_CTX}
        trace_block = _format_trace(model, tier_label, params, trace_ctx)
        return with_footer(response + trace_block, iid)
    if iid is not None:
        return with_footer(response, iid)
    return response


@mcp.tool()
def record_outcome(interaction_id: str, signal: str) -> str:
    """Feed a real-world outcome back into trilobite's learning loop.

    Call this after a trilobite/offload response once you know how it went.
    signal is one of: tests_passed, accepted, compiled, rejected, failed.
    A good outcome triggers a distilled 'lesson' that future prompts will retrieve.
    Pass the id from the '[interaction_id: <id>]' footer of the response.
    """
    _maybe_live_reload()
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
        memory_store.record_lesson_usage_outcome(conn, interaction_id, signal, r)
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
def parallel_run_code(jobs_json: str, max_workers: int = 4, timeout: int = 8) -> str:
    """Compile and execute many snippets concurrently.

    jobs_json is a JSON list. Each item may be a code string or an object:
      {"name":"candidate-a", "language":"python|javascript|powershell|cpp|csharp",
       "code":"print(2+2)", "check":"assert ...", "timeout":8, "execute":true}

    Every supported job is compiled/checked first, then executed with its optional
    check appended where that language supports it.
    Worker count and timeouts are bounded so this stays useful without stampeding the
    machine.
    """
    try:
        jobs = json.loads(jobs_json)
        results = grounding.run_code_jobs(
            jobs,
            max_workers=max_workers,
            default_timeout=timeout,
        )
    except Exception as e:
        return "ERROR: %s" % e
    return grounding.format_code_jobs(results)


@mcp.tool()
def parallel_generate_run(
    prompt: str,
    check: str = "",
    variants: int = 4,
    tier: str = "code",
    max_workers: int = 4,
    timeout: int = 8,
    temperature: float = 0.4,
    num_predict: int = 900,
    num_ctx: int = 4096,
) -> str:
    """Generate several Python code candidates in parallel, then compile/run each.

    The prompt should describe the desired Python solution. `check` is appended to
    each extracted code block, usually as assertions. This is meant for search:
    generate multiple attempts, compile them, execute them, and keep the winners.
    """
    variants = max(1, min(int(variants or 1), 12))
    max_workers = max(1, min(int(max_workers or 1), 8, variants))
    timeout = max(1, min(int(timeout or 8), 120))
    model = TIERS.get(tier)
    if model is None:
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, ", ".join(TIERS))
    cloud = _is_cloud_tier(tier, model)
    system = (
        "Return one complete runnable Python solution in a single ```python code block. "
        "No prose outside the code block. Avoid input() and unbounded loops."
    )
    gen = _make_generate(model, system, temperature, num_predict, num_ctx, cloud=cloud)
    started = time.time()
    generation_results = [None] * variants

    def one(i):
        candidate_prompt = (
            "%s\n\nGenerate candidate %d of %d. Use a distinct implementation strategy "
            "if there is a reasonable alternative." % (prompt, i + 1, variants)
        )
        try:
            response = gen(candidate_prompt)
            code = grounding.extract_code_block(response)
            if not code:
                return {
                    "index": i,
                    "name": "candidate-%d" % (i + 1),
                    "ok": False,
                    "output": "no Python code block returned",
                    "seconds": 0,
                    "response": response[:1200],
                }
            ok, out = grounding.run_code(code, check, timeout=timeout, compile_first=True)
            return {
                "index": i,
                "name": "candidate-%d" % (i + 1),
                "ok": bool(ok),
                "output": out,
                "seconds": 0,
                "code": code,
            }
        except Exception as e:
            return {
                "index": i,
                "name": "candidate-%d" % (i + 1),
                "ok": False,
                "output": "ERROR: %s" % e,
                "seconds": 0,
            }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(one, i): i for i in range(variants)}
        for future in as_completed(futures):
            result = future.result()
            generation_results[result["index"]] = result
    elapsed = round(time.time() - started, 3)
    passed = sum(1 for r in generation_results if r and r.get("ok"))
    lines = [
        "parallel generate/run: %d/%d passed in %.3fs (tier=%s, workers=%d)"
        % (passed, variants, elapsed, tier, max_workers)
    ]
    for r in generation_results:
        status = "PASS" if r.get("ok") else "FAIL"
        lines.append("[%s] %s" % (status, r.get("name")))
        out = (r.get("output") or "").strip()
        if out:
            lines.append(out[:1200])
    winner = next((r for r in generation_results if r.get("ok") and r.get("code")), None)
    if winner:
        lines.append("winner code:")
        lines.append("```python\n%s\n```" % winner["code"])
    return "\n".join(lines)


@mcp.tool()
def parallel_generate_run_languages(
    prompt: str,
    languages: str = "python,javascript,powershell,cpp,csharp",
    check: str = "",
    variants_per_language: int = 1,
    tier: str = "code",
    max_workers: int = 5,
    timeout: int = 8,
    temperature: float = 0.35,
    num_predict: int = 900,
    num_ctx: int = 4096,
) -> str:
    """Generate, compile, and execute many candidates across multiple languages.

    `languages` is a comma-separated list from python, javascript, powershell, cpp,
    csharp. The model is asked for one fenced block per candidate in the requested
    language. All candidates are generated and tested in parallel.
    """
    language_list = [
        grounding.normalize_language(x)
        for x in (languages or "").split(",")
        if x.strip()
    ]
    if not language_list:
        return "ERROR: at least one language is required"
    allowed = {"python", "javascript", "powershell", "cpp", "csharp"}
    bad = [x for x in language_list if x not in allowed]
    if bad:
        return "ERROR: unsupported language(s): %s" % ", ".join(bad)
    variants_per_language = max(1, min(int(variants_per_language or 1), 6))
    jobs = []
    for lang in language_list:
        for i in range(variants_per_language):
            jobs.append((lang, i + 1))
    max_workers = max(1, min(int(max_workers or 1), 12, len(jobs)))
    timeout = max(1, min(int(timeout or 8), 120))
    model = TIERS.get(tier)
    if model is None:
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, ", ".join(TIERS))
    cloud = _is_cloud_tier(tier, model)
    started = time.time()
    results = [None] * len(jobs)

    def one(index, lang, variant):
        fence = lang
        system = (
            "Return one complete runnable %s program in a single ```%s code block. "
            "No prose outside the code block. Avoid interactive input and unbounded loops."
            % (lang, fence)
        )
        gen = _make_generate(model, system, temperature, num_predict, num_ctx, cloud=cloud)
        candidate_prompt = (
            "%s\n\nGenerate %s candidate %d. It must compile and terminate quickly."
            % (prompt, lang, variant)
        )
        try:
            response = gen(candidate_prompt)
            code = grounding.extract_code_block(response, lang)
            if not code:
                return {
                    "index": index,
                    "name": "%s-%d" % (lang, variant),
                    "language": lang,
                    "ok": False,
                    "output": "no %s code block returned" % lang,
                    "seconds": 0,
                }
            ok, out = grounding.run_language_code(
                code,
                language=lang,
                extra=check,
                timeout=timeout,
                execute=True,
            )
            return {
                "index": index,
                "name": "%s-%d" % (lang, variant),
                "language": lang,
                "ok": bool(ok),
                "output": out,
                "seconds": 0,
                "code": code,
            }
        except Exception as e:
            return {
                "index": index,
                "name": "%s-%d" % (lang, variant),
                "language": lang,
                "ok": False,
                "output": "ERROR: %s" % e,
                "seconds": 0,
            }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(one, index, lang, variant)
            for index, (lang, variant) in enumerate(jobs)
        ]
        for future in as_completed(futures):
            result = future.result()
            results[result["index"]] = result
    elapsed = round(time.time() - started, 3)
    passed = sum(1 for r in results if r and r.get("ok"))
    lines = [
        "parallel multi-language generate/run: %d/%d passed in %.3fs (tier=%s, workers=%d)"
        % (passed, len(results), elapsed, tier, max_workers)
    ]
    for r in results:
        status = "PASS" if r.get("ok") else "FAIL"
        lines.append("[%s] %s [%s]" % (status, r.get("name"), r.get("language")))
        out = (r.get("output") or "").strip()
        if out:
            lines.append(out[:1200])
    winners = [r for r in results if r.get("ok") and r.get("code")]
    if winners:
        lines.append("winner code blocks:")
        for r in winners[:3]:
            fence = grounding._LANG_FENCE.get(r["language"], r["language"])
            lines.append("```%s\n%s\n```" % (fence, r["code"]))
    return "\n".join(lines)


_CAMPAIGN_TASKS = [
    ("hello", "print exactly: trilobite-ok"),
    ("sum", "compute 12 + 30 and print exactly: 42"),
    ("loop", "print the numbers 1, 2, and 3 each on its own line"),
    ("string", "reverse the string 'trilobite' and print exactly: etibolirt"),
    ("branch", "if 17 is prime print exactly: prime"),
    ("list", "compute the sum of [2, 4, 6, 8] and print exactly: 20"),
]


def _campaign_expected(task_name):
    return {
        "hello": "trilobite-ok",
        "sum": "42",
        "loop": "1\n2\n3",
        "string": "etibolirt",
        "branch": "prime",
        "list": "20",
    }.get(task_name, "")


def _campaign_prompt(language, task_name, task_text, repair_note=""):
    fence = grounding._LANG_FENCE.get(language, language)
    repair = ("\nPrevious attempt failed:\n%s\nFix it." % repair_note) if repair_note else ""
    language_note = ""
    if language == "powershell" and task_name == "string":
        language_note = (
            " PowerShell arrays print one item per line; when building a string from "
            "characters, reverse by index/order and join explicitly with -join; do not "
            "sort the characters."
        )
    if language == "powershell" and task_name == "list":
        language_note = (
            " In PowerShell, use Measure-Object -Sum or a simple loop to sum numeric "
            "arrays; do not use Invoke-Expression for arithmetic."
        )
    if language == "cpp" and task_name == "string":
        language_note = (
            " In C++, include <algorithm> before using std::reverse, or reverse the "
            "string manually."
        )
    return (
        "Write a complete runnable %s program for this task: %s.\n"
        "Return only one ```%s code block. Do not use interactive input. "
        "The program must terminate quickly.%s%s" % (
            language, task_text, fence, language_note, repair)
    )


@mcp.tool()
def campaign_generate_compile_execute_record(
    total: int = 24,
    languages: str = "python,javascript,powershell,cpp,csharp",
    tier: str = "code",
    max_workers: int = 5,
    timeout: int = 8,
    repair_rounds: int = 1,
    record_failures: bool = True,
) -> str:
    """Run a bounded self-improvement campaign across multiple languages.

    The campaign generates many complete programs, compiles/executes them, repairs
    failures once by default, and records every passing interaction as tests_passed.
    When record_failures is true, terminal failed attempts with an interaction id are
    recorded as failed too, so the reward store keeps negative signals.
    """
    total = max(1, min(int(total or 1), 120))
    max_workers = max(1, min(int(max_workers or 1), 12, total))
    timeout = max(1, min(int(timeout or 8), 120))
    repair_rounds = max(0, min(int(repair_rounds or 0), 3))
    language_list = [
        grounding.normalize_language(x)
        for x in (languages or "").split(",")
        if x.strip()
    ]
    allowed = {"python", "javascript", "powershell", "cpp", "csharp"}
    language_list = [x for x in language_list if x in allowed]
    if not language_list:
        return "ERROR: no supported languages selected"

    jobs = []
    for i in range(total):
        lang = language_list[i % len(language_list)]
        task_name, task_text = _CAMPAIGN_TASKS[i % len(_CAMPAIGN_TASKS)]
        jobs.append((i, lang, task_name, task_text))

    def run_one(index, lang, task_name, task_text):
        attempts = []
        last_note = ""
        for attempt in range(repair_rounds + 1):
            prompt = _campaign_prompt(lang, task_name, task_text, last_note)
            with _CAMPAIGN_LEARN_LOCK:
                response = trilobite(
                    prompt,
                    tier=tier,
                    session="none",
                    temperature=0.35 if attempt == 0 else 0.2,
                    num_predict=900,
                )
            iid = parse_interaction_id(response)
            code = grounding.extract_code_block(response, lang)
            if not code:
                ok = False
                out = "no %s code block returned" % lang
            else:
                ok, out = grounding.run_language_code(
                    code,
                    language=lang,
                    timeout=timeout,
                    execute=True,
                )
                expected = _campaign_expected(task_name)
                if ok and expected and expected not in (out or ""):
                    ok = False
                    out = "wrong output; expected to contain %r, got %r" % (expected, out)
            record_msg = ""
            if ok and iid:
                with _CAMPAIGN_LEARN_LOCK:
                    record_msg = record_outcome(iid, "tests_passed")
            elif attempt == repair_rounds and record_failures and iid:
                with _CAMPAIGN_LEARN_LOCK:
                    record_msg = record_outcome(iid, "failed")
            attempts.append({
                "attempt": attempt + 1,
                "ok": ok,
                "iid": iid,
                "output": out,
                "record": record_msg,
            })
            if ok:
                break
            last_note = (out or "unknown failure")[:1200]
        final = attempts[-1]
        return {
            "index": index,
            "name": "%s-%s-%d" % (lang, task_name, index + 1),
            "language": lang,
            "task": task_name,
            "ok": bool(final["ok"]),
            "attempts": attempts,
            "iid": final.get("iid"),
        }

    started = time.time()
    results = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_one, *job) for job in jobs]
        for future in as_completed(futures):
            result = future.result()
            results[result["index"]] = result
    elapsed = round(time.time() - started, 3)
    passed = sum(1 for r in results if r and r.get("ok"))
    recorded = sum(
        1
        for r in results
        for a in r.get("attempts", [])
        if a.get("ok") and a.get("record")
    )
    failed_recorded = sum(
        1
        for r in results
        for a in r.get("attempts", [])
        if not a.get("ok") and a.get("record")
    )
    by_lang = {}
    for r in results:
        lang = r["language"]
        ok, total_lang = by_lang.get(lang, (0, 0))
        by_lang[lang] = (ok + (1 if r["ok"] else 0), total_lang + 1)
    lines = [
        "campaign generate/compile/execute/record: %d/%d passed, %d recorded, %d failed-recorded in %.3fs"
        % (passed, len(results), recorded, failed_recorded, elapsed),
        "by language: %s" % ", ".join(
            "%s=%d/%d" % (lang, ok, total_lang)
            for lang, (ok, total_lang) in sorted(by_lang.items())
        ),
    ]
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        lines.append("[%s] %s attempts=%d iid=%s" % (
            status, r["name"], len(r["attempts"]), r.get("iid") or "-"))
        final_out = (r["attempts"][-1].get("output") or "").strip()
        if final_out:
            lines.append(final_out[:800])
        record_msg = (r["attempts"][-1].get("record") or "").strip()
        if record_msg:
            lines.append(record_msg[:800])
    return "\n".join(lines)


@mcp.tool()
def trilobite_stats() -> str:
    """Report what trilobite has learned so far.

    Read-only observability into the learning loop's SQLite memory: how many
    interactions have been logged, how outcomes break down by signal, and the
    most recently distilled lessons. Makes no model call and needs no Ollama —
    it only reads memory.db, so it works even if the Ollama server is down.
    """
    _maybe_live_reload()
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


def _rough_token_count(text) -> int:
    """Cheap, dependency-free estimate for dashboard health meters."""
    if not text:
        return 0
    return max(1, (len(str(text)) + 3) // 4)


def _rough_token_count_from_chars(count) -> int:
    count = max(0, int(count or 0))
    return max(1, (count + 3) // 4) if count else 0


def _health_bar(percent, width=18) -> str:
    pct = max(0.0, min(1.0, float(percent or 0.0)))
    filled = int(round(pct * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def context_health_data(session: str = "", project: str = "") -> dict:
    """Read-only context/memory snapshot for app and console visualizers.

    This reports an approximate context load. Ollama does not expose the exact
    live prompt token count here, so we estimate from the active session summary
    plus the recent turns that Trilobite keeps in the prompt.
    """
    _maybe_live_reload()
    session_id = _resolve_session(session)
    project_id = _resolve_project(project)
    conn = _open_db()
    try:
        turns = memory_store.session_history(conn, session_id, MAX_TURNS) if session_id else []
        session_row = memory_store.get_session(conn, session_id) if session_id else None
        summary = (session_row or {}).get("summary") or ""
        turn_count = memory_store.session_turn_count(conn, session_id) if session_id else 0
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        lesson_count = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        fact_count = (
            memory_store.count_facts(conn, project_id) if project_id else
            conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        )
        interaction_count = memory_store.count_interactions(conn)
        outcome_count = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        summarized_through = (session_row or {}).get("summarized_through") or ""
        updated_ts = (session_row or {}).get("updated_ts") or ""
        title = (session_row or {}).get("title") or ""
        live_chars = sum(len(task or "") + len(response or "") for task, response in turns)
        summary_tokens = _rough_token_count(summary)
        live_tokens = _rough_token_count_from_chars(live_chars)
        estimated_tokens = summary_tokens + live_tokens
    finally:
        conn.close()

    context_limit = max(1, int(SESSION_NUM_CTX or 1))
    context_ratio = min(1.0, estimated_tokens / context_limit)
    live_turn_count = len(turns)
    turn_ratio = min(1.0, live_turn_count / max(1, int(MAX_TURNS or 1)))
    if context_ratio >= 0.90:
        status_label = "hot"
    elif context_ratio >= 0.70:
        status_label = "warm"
    else:
        status_label = "healthy"
    memory_items = lesson_count + fact_count + outcome_count
    memory_ratio = min(1.0, memory_items / 1000.0)
    return {
        "session": session_id or "none",
        "project": project_id or "none",
        "title": title,
        "status": status_label,
        "context_limit": context_limit,
        "estimated_tokens": estimated_tokens,
        "context_percent": round(context_ratio * 100.0, 1),
        "context_bar": _health_bar(context_ratio),
        "live_turns": live_turn_count,
        "max_live_turns": MAX_TURNS,
        "total_turns": turn_count,
        "turn_percent": round(turn_ratio * 100.0, 1),
        "turn_bar": _health_bar(turn_ratio),
        "summary_tokens": summary_tokens,
        "live_tokens": live_tokens,
        "summary_chars": len(summary),
        "summarized_through": summarized_through,
        "updated_ts": updated_ts,
        "sessions": session_count,
        "lessons": lesson_count,
        "facts": fact_count,
        "interactions": interaction_count,
        "outcomes": outcome_count,
        "memory_percent": round(memory_ratio * 100.0, 1),
        "memory_bar": _health_bar(memory_ratio),
        "db_path": _DB_PATH,
        "state_home": str(trilobite_paths.default_home()),
    }


def format_context_health(data: dict) -> str:
    lines = [
        "trilobite context health",
        "  status: %s" % data.get("status", "unknown"),
        "  session: %s%s" % (
            data.get("session", "none"),
            " (%s)" % data.get("title") if data.get("title") else "",
        ),
        "  context %s %s%%  ~%s/%s tokens" % (
            data.get("context_bar", ""),
            data.get("context_percent", 0),
            data.get("estimated_tokens", 0),
            data.get("context_limit", 0),
        ),
        "  live    %s %s/%s turns in active prompt (%s total)" % (
            data.get("turn_bar", ""),
            data.get("live_turns", 0),
            data.get("max_live_turns", 0),
            data.get("total_turns", 0),
        ),
        "  memory  %s %s lessons, %s facts, %s interactions, %s outcomes" % (
            data.get("memory_bar", ""),
            data.get("lessons", 0),
            data.get("facts", 0),
            data.get("interactions", 0),
            data.get("outcomes", 0),
        ),
        "  summary: %s chars, ~%s tokens%s" % (
            data.get("summary_chars", 0),
            data.get("summary_tokens", 0),
            " through %s" % data.get("summarized_through")
            if data.get("summarized_through") else "",
        ),
        "  db: %s" % data.get("db_path", ""),
    ]
    return "\n".join(lines)


@mcp.tool()
def context_health(session: str = "", project: str = "") -> str:
    """Show context budget, live turns, summaries, and memory as text meters."""
    return format_context_health(context_health_data(session=session, project=project))


@mcp.tool()
def memory_quality_report(sample_limit: int = 5) -> str:
    """Audit lesson quality: duplicates, long/vague rows, embeddings, and FTS health."""
    _maybe_live_reload()
    sample_limit = _safe_limit(sample_limit, 5, 20)
    conn = _open_db()
    try:
        report = memory_quality.audit(conn)
    finally:
        conn.close()
    return memory_quality.format_audit(report, sample_limit=sample_limit)


@mcp.tool()
def memory_quality_repair(apply: bool = False) -> str:
    """Prune exact duplicate lessons; dry-run unless apply=True."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        plan, deleted = memory_quality.repair_exact_duplicates(conn, apply=bool(apply))
        report = memory_quality.format_audit(memory_quality.audit(conn), sample_limit=5)
    finally:
        conn.close()
    prunable = sum(len(entry["prune_ids"]) for entry in plan)
    lines = [
        "memory quality repair",
        "  mode: %s" % ("apply" if apply else "dry-run"),
        "  exact duplicate groups: %d" % len(plan),
        "  prunable exact duplicates: %d" % prunable,
        "  deleted: %d" % deleted,
    ]
    if not apply and prunable:
        lines.append("  rerun with apply=True to delete exact duplicate lesson rows.")
    lines.extend(["", report])
    return "\n".join(lines)


@mcp.tool()
def learn_tiers() -> str:
    """Show which tiers currently feed the learning loop."""
    lines = ["learning tiers"]
    for tier, model in TIERS.items():
        state = "on" if tier in LEARN_TIERS else "off"
        locality = "cloud" if _is_cloud_tier(tier, model) else "local"
        lines.append("  %s: %s (%s, %s)" % (tier, state, locality, model))
    lines.append("override with TRILOBITE_LEARN_TIERS=fast,code,general,cloud-code,cloud-general")
    return "\n".join(lines)


@mcp.tool()
def trilobite_sessions(limit: int = 20) -> str:
    """List trilobite conversation threads, most recently used first.

    Each line shows the session id (pass it as `session` to trilobite to resume),
    its auto-generated title, live turn count, and last-updated time. Read-only.
    """
    _maybe_live_reload()
    conn = _open_db()
    try:
        sessions = memory_store.list_sessions(conn, limit=limit)
    finally:
        conn.close()
    if not sessions:
        return "no conversation sessions yet."
    lines = ["trilobite sessions (most recent first):"]
    for s in sessions:
        lines.append("  %s  [%d turns]  %s  (updated %s)" % (
            s["session_id"], s["turn_count"], s.get("title") or "(untitled)",
            s.get("updated_ts") or "?",
        ))
    return "\n".join(lines)


@mcp.tool()
def trilobite_remember_fact(text: str, project: str = "") -> str:
    """Store a durable fact trilobite should ALWAYS know for a project.

    Unlike lessons (earned from good outcomes), facts are asserted directly and are
    injected into every trilobite call for that project — a mini project brief the
    model carries itself (toolchain, conventions, key paths, gotchas). No `project`
    stores it under the "default" project. Use trilobite(..., project="<name>") to
    scope which facts apply to a call.
    """
    _maybe_live_reload()
    text = (text or "").strip()
    if not text:
        return "ERROR: empty fact."
    project_id = _resolve_project(project) or DEFAULT_PROJECT
    conn = _open_db()
    try:
        emb = embeddings.embed(text)
        blob = embeddings.to_blob(emb) if emb else None
        fact_id = memory_store.new_id()
        memory_store.add_fact(conn, fact_id, project_id, text, blob)
        n = memory_store.count_facts(conn, project_id)
    finally:
        conn.close()
    return "Remembered fact for project '%s' (%d total). id=%s" % (project_id, n, fact_id)


@mcp.tool()
def run_code(
    code: str,
    language: str = "python",
    stdin: str = "",
    timeout: int = 10,
    cwd: str = "",
) -> str:
    """Execute a short local code snippet and return stdout/stderr.

    This gives Claude/Codex a Claude-like execution tool through the local-llm MCP
    server. Supported languages: python, javascript/js/node, powershell/ps1,
    cpp/c++, and csharp/cs. Code runs on this machine with the same permissions as the MCP server, so treat it
    like a local terminal: use it for small checks, experiments, and diagnostics,
    not for untrusted code. Execution is bounded by a timeout (1-60s), output is
    trimmed, and cwd is confined to this project workspace.
    """
    _maybe_live_reload()
    try:
        result = code_runner.run_code(
            code=code,
            language=language,
            stdin=stdin,
            timeout=timeout,
            cwd=cwd or None,
        )
    except ValueError as e:
        return "ERROR: %s" % e
    return code_runner.format_result(result)


@mcp.tool()
def run_project(
    files_json: str,
    commands_json: str = "",
    stdin: str = "",
    timeout: int = 60,
) -> str:
    """Run a temporary multi-file project and return build/run output.

    files_json may be {"files": {"path": "content"}} or a list of
    {"path": "...", "content": "..."} objects. Paths must be relative and stay
    inside the temp project. commands_json is optional; when omitted, the runner
    auto-detects common layouts: main.py/app.py, C# .csproj or .cs files,
    C++ .cpp/.cc/.cxx files, or package.json. Custom commands must be argv JSON,
    e.g. [{"cmd": ["dotnet", "test"]}]; no shell is used.
    """
    _maybe_live_reload()
    try:
        result = code_runner.run_project(
            files_json=files_json,
            commands_json=commands_json,
            stdin=stdin,
            timeout=timeout,
        )
    except ValueError as e:
        return "ERROR: %s" % e
    return code_runner.format_project_result(result)


def _loop_text_result(action_type, text):
    text = text or ""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return {
        "ok": not text.startswith("ERROR:"),
        "type": action_type,
        "summary": first_line[:200],
        "output": text,
    }


def _loop_dispatch(action):
    action_type = (action.get("type") or action.get("action") or "code").strip().lower()
    if action_type in ("code", "run_code"):
        result = code_runner.run_code(
            code=action.get("code", ""),
            language=action.get("language", "python"),
            stdin=action.get("stdin", ""),
            timeout=action.get("timeout", 10),
            cwd=action.get("cwd") or None,
        )
        rc = result.get("returncode")
        summary = result.get("error") or "returncode %s" % (
            "(none)" if rc is None else rc
        )
        return {
            "ok": result.get("ok"),
            "type": "code",
            "summary": summary,
            "output": code_runner.format_result(result),
        }
    if action_type in ("project", "run_project"):
        try:
            result = code_runner.run_project(
                files_json=action.get("files") or action.get("files_json") or [],
                commands_json=action.get("commands") or action.get("commands_json") or "",
                stdin=action.get("stdin", ""),
                timeout=action.get("timeout", 60),
            )
        except ValueError as e:
            return {
                "ok": False,
                "type": "project",
                "summary": str(e),
                "output": "",
            }
        return {
            "ok": result.get("ok"),
            "type": "project",
            "summary": "project %s" % ("ok" if result.get("ok") else "failed"),
            "output": code_runner.format_project_result(result),
        }
    if action_type == "offload":
        return _loop_text_result("offload", offload(
            prompt=action.get("prompt", ""),
            tier=action.get("tier", "fast"),
            system=action.get("system", ""),
            temperature=action.get("temperature", 0.2),
            num_predict=action.get("num_predict", 1024),
            num_ctx=action.get("num_ctx", 4096),
            learn=action.get("learn", True),
        ))
    if action_type == "trilobite":
        return _loop_text_result("trilobite", trilobite(
            prompt=action.get("prompt", ""),
            system=action.get("system", ""),
            temperature=action.get("temperature", 0.2),
            num_predict=action.get("num_predict", 1024),
            num_ctx=action.get("num_ctx", 4096),
            trace=action.get("trace", False),
            strict=action.get("strict"),
            persona=action.get("persona", ""),
            session=action.get("session", ""),
            project=action.get("project", ""),
            tier=action.get("tier", ""),
        ))
    if action_type == "status":
        return _loop_text_result("status", status())
    if action_type == "diagnostics":
        return _loop_text_result("diagnostics", diagnostics())
    if action_type == "context_health":
        return _loop_text_result("context_health", context_health(
            session=action.get("session", ""),
            project=action.get("project", ""),
        ))
    if action_type == "memory_quality_report":
        return _loop_text_result("memory_quality_report", memory_quality_report(
            sample_limit=action.get("sample_limit", 5),
        ))
    if action_type == "memory_quality_repair":
        return _loop_text_result("memory_quality_repair", memory_quality_repair(
            apply=action.get("apply", False),
        ))
    if action_type == "self_heal_check":
        return _loop_text_result("self_heal_check", self_heal_check())
    if action_type == "self_heal_repair":
        return _loop_text_result("self_heal_repair", self_heal_repair(
            apply=action.get("apply", False),
        ))
    if action_type == "profile_status":
        return _loop_text_result("profile_status", system_profile_text())
    if action_type == "emotion_status":
        return _loop_text_result("emotion_status", emotion_vector_status())
    if action_type == "memory_search":
        return _loop_text_result("memory_search", memory_search(
            query=action.get("query", ""),
            limit=action.get("limit", 10),
        ))
    if action_type == "apply_learned":
        return _loop_text_result("apply_learned", apply_learned(
            task=action.get("task", ""),
            limit=action.get("limit", 5),
        ))
    if action_type == "web_search":
        return _loop_text_result("web_search", web_search(
            query=action.get("query", ""),
            limit=action.get("limit", 5),
        ))
    if action_type == "web_fetch":
        return _loop_text_result("web_fetch", web_fetch(
            url=action.get("url", ""),
            max_chars=action.get("max_chars", 8000),
        ))
    if action_type == "unload":
        return _loop_text_result("unload", unload(action.get("tier", "all")))
    if action_type == "sleep":
        seconds = code_runner._clamp_delay(action.get("seconds", 1))
        time.sleep(seconds)
        return {
            "ok": True,
            "type": "sleep",
            "summary": "slept for %.2fs" % seconds,
            "output": "",
        }
    return {
        "ok": False,
        "type": action_type or "(unknown)",
        "summary": "unknown action type",
        "output": "Valid action types: code, project, offload, trilobite, status, diagnostics, context_health, memory_quality_report, memory_quality_repair, self_heal_check, self_heal_repair, profile_status, emotion_status, memory_search, apply_learned, web_search, web_fetch, unload, sleep.",
    }


@mcp.tool()
def loop(
    actions_json: str,
    max_iterations: int = 5,
    stop_on_failure: bool = True,
    stop_on_success: bool = False,
    delay_seconds: float = 0,
) -> str:
    """Run a bounded loop of code/model/system actions.

    `actions_json` is a JSON list of action objects, or {"actions": [...]}.
    Supported action types:
      - {"type":"code","language":"python|js|powershell|cpp|csharp","code":"..."}
      - {"type":"project","files":[{"path":"src/main.cpp","content":"..."}],"commands":[{"cmd":["g++","src/main.cpp","-o","app"]}]}
      - {"type":"offload","prompt":"...","tier":"fast|code|general|cloud-code|cloud-general"}
      - {"type":"trilobite","prompt":"...","session":"none"}
      - {"type":"web_search","query":"...","limit":5}
      - {"type":"web_fetch","url":"https://...","max_chars":8000}
      - {"type":"memory_search","query":"..."}
      - {"type":"status"}
      - {"type":"unload","tier":"all"}
      - {"type":"sleep","seconds":1}

    The loop is deliberately bounded: max_iterations is clamped to 1-50 and
    delay_seconds to 0-10. Use stop_on_success=True for polling/retry loops, or
    stop_on_failure=False to keep running after failures until the iteration cap.
    """
    _maybe_live_reload()
    try:
        parsed = json.loads(actions_json)
    except json.JSONDecodeError as e:
        return "ERROR: actions_json is not valid JSON: %s" % e
    actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
    try:
        result = code_runner.run_loop(
            actions,
            _loop_dispatch,
            max_iterations=max_iterations,
            stop_on_failure=stop_on_failure,
            stop_on_success=stop_on_success,
            delay_seconds=delay_seconds,
        )
    except ValueError as e:
        return "ERROR: %s" % e
    return code_runner.format_loop_result(result)


@mcp.tool()
def workflow_list() -> str:
    """List reusable named workflows stored in workflows.json."""
    _maybe_live_reload()
    workflows, path = workflow_store.ensure_workflows()
    return "workflows: %s\n\n%s" % (path, workflow_store.format_workflows(workflows))


@mcp.tool()
def workflow_save(name: str, actions_json: str, description: str = "") -> str:
    """Save a named workflow made of loop action objects.

    `actions_json` may be a JSON list or {"actions": [...]}.
    """
    _maybe_live_reload()
    try:
        parsed = json.loads(actions_json)
    except json.JSONDecodeError as e:
        return "ERROR: actions_json is not valid JSON: %s" % e
    actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
    try:
        workflow, path = workflow_store.save_workflow(name, actions, description)
    except ValueError as e:
        return "ERROR: %s" % e
    return "Saved workflow '%s' to %s (%d actions)." % (
        workflow_store.normalize_name(name), path, len(workflow["actions"]))


@mcp.tool()
def workflow_run(
    name: str,
    max_iterations: int = 1,
    stop_on_failure: bool = True,
    stop_on_success: bool = False,
    delay_seconds: float = 0,
) -> str:
    """Run a saved workflow through the bounded loop engine."""
    _maybe_live_reload()
    try:
        workflow = workflow_store.get_workflow(name)
    except ValueError as e:
        return "ERROR: %s" % e
    if workflow is None:
        return "ERROR: no workflow named '%s'." % name
    result = code_runner.run_loop(
        workflow["actions"],
        _loop_dispatch,
        max_iterations=max_iterations,
        stop_on_failure=stop_on_failure,
        stop_on_success=stop_on_success,
        delay_seconds=delay_seconds,
    )
    header = "workflow: %s\n%s\n" % (
        workflow_store.normalize_name(name),
        workflow.get("description") or "(no description)",
    )
    return header + code_runner.format_loop_result(result)


@mcp.tool()
def workflow_delete(name: str) -> str:
    """Delete a saved workflow from workflows.json."""
    _maybe_live_reload()
    try:
        existed, path = workflow_store.delete_workflow(name)
    except ValueError as e:
        return "ERROR: %s" % e
    if not existed:
        return "No workflow named '%s' existed. File unchanged except normalization: %s" % (name, path)
    return "Deleted workflow '%s' from %s." % (workflow_store.normalize_name(name), path)


@mcp.tool()
def web_search(query: str, limit: int = 5) -> str:
    """Search the public web and return compact result links.

    Uses a stdlib HTML parser against TRILOBITE_SEARCH_URL (default:
    DuckDuckGo HTML). Disable with TRILOBITE_WEB_TOOLS=0.
    """
    _maybe_live_reload()
    try:
        results = web_tools.web_search(query, limit=limit)
    except Exception as e:
        return "ERROR: %s" % e
    return web_tools.format_search_results(results)


@mcp.tool()
def web_fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a public HTTP/HTTPS URL as readable text.

    Blocks localhost/private-network literal IPs and trims output. Disable with
    TRILOBITE_WEB_TOOLS=0.
    """
    _maybe_live_reload()
    try:
        return web_tools.web_fetch(url, max_chars=max_chars)
    except Exception as e:
        return "ERROR: %s" % e


@mcp.tool()
def live_reload_status() -> str:
    """Show live-reload state for this running process.

    Helper modules are checked at tool/request boundaries when
    TRILOBITE_LIVE_RELOAD is on (default). The HTTP proxy and REPL also reload
    server.py itself before each request/turn. An MCP process can refresh helper
    module behavior, but brand-new MCP tool names still require reconnecting the
    MCP server because FastMCP registers the tool list at startup.
    """
    _maybe_live_reload()
    lines = [
        "live reload: %s" % ("on" if live_reload.enabled() else "off"),
        "watched modules:",
    ]
    for row in live_reload.snapshot(LIVE_RELOAD_MODULES):
        line = "  - %s%s" % (
            row["name"],
            (" (%s)" % row["path"]) if row["path"] else " (not loaded)",
        )
        if row.get("error"):
            line += "  ERROR: %s" % row["error"]
        lines.append(line)
    lines.append(
        "note: HTTP proxy/REPL reload server.py on each request; MCP tool names are registered at startup."
    )
    return "\n".join(lines)


@mcp.tool()
def system_profile_text() -> str:
    """Read the editable standing instructions injected into trilobite.

    The profile lives in system_profile.md by default and is read on every
    trilobite/serve request, so edits take effect without restarting the proxy or
    REPL. Empty means no extra standing instructions are injected.
    """
    _maybe_live_reload()
    text, path = system_profile.ensure_profile()
    return "profile: %s\n\n%s" % (path, text or "(empty)")


@mcp.tool()
def update_system_profile(text: str, mode: str = "append") -> str:
    """Append, replace, or clear trilobite's editable standing instructions.

    mode: append (default), replace, or clear. The profile is plain Markdown in
    system_profile.md, so direct file edits work too and are reflected on the
    next request.
    """
    _maybe_live_reload()
    mode = (mode or "append").strip().lower()
    try:
        if mode == "append":
            path = system_profile.append_profile(text)
        elif mode == "replace":
            path = system_profile.write_profile(text)
        elif mode == "clear":
            path = system_profile.write_profile("")
        else:
            return "ERROR: unknown mode '%s'. Use append, replace, or clear." % mode
    except ValueError as e:
        return "ERROR: %s" % e
    n = len(system_profile.read_profile())
    return "Updated system profile (%s). %d characters active." % (path, n)


@mcp.tool()
def emotion_vector_status() -> str:
    """Show the current live emotion/tone steering vectors.

    Values are behavioral style controls from -1.0 to +1.0. They are injected
    into the system prompt on every request, underneath correctness and explicit
    user instructions.
    """
    _maybe_live_reload()
    vectors, path = emotion_vectors.ensure_vectors()
    return "emotion vectors: %s\n\n%s" % (path, emotion_vectors.format_vectors(vectors))


@mcp.tool()
def update_emotion_vectors(vectors_json: str, mode: str = "merge") -> str:
    """Merge, replace, or clear the live emotion/tone steering vectors.

    `vectors_json` must be a JSON object, for example:
      {"warmth": 0.6, "brevity": 0.4, "urgency": -0.2}

    Values are clamped to [-1.0, 1.0]. mode: merge (default), replace, or clear.
    Direct edits to emotion_vectors.json also apply on the next request.
    """
    _maybe_live_reload()
    try:
        updates = json.loads(vectors_json or "{}")
    except json.JSONDecodeError as e:
        return "ERROR: vectors_json is not valid JSON: %s" % e
    try:
        vectors, path = emotion_vectors.update_vectors(updates, mode=mode)
    except ValueError as e:
        return "ERROR: %s" % e
    return "Updated emotion vectors (%s).\n%s" % (
        path,
        emotion_vectors.format_vectors(vectors),
    )


def _safe_limit(limit, default=10, max_value=100):
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max_value))


@mcp.tool()
def memory_search(query: str, limit: int = 10) -> str:
    """Search local lessons, facts, sessions, and recent interactions."""
    _maybe_live_reload()
    query = (query or "").strip()
    if not query:
        return "ERROR: empty query."
    limit = _safe_limit(limit, 10, 50)
    like = "%%%s%%" % query.replace("%", r"\%").replace("_", r"\_")
    conn = _open_db()
    try:
        lesson_ids = memory_store.fts_search(conn, query, limit=limit)
        lessons = []
        for lesson_id in lesson_ids:
            text = memory_store.get_lesson_text(conn, lesson_id)
            if text:
                lessons.append({"id": lesson_id, "text": text})
        facts = [dict(r) for r in conn.execute(
            "SELECT id, project, text FROM facts WHERE text LIKE ? ESCAPE '\\' "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (like, limit),
        ).fetchall()]
        sessions = [dict(r) for r in conn.execute(
            "SELECT session_id, title, summary, project FROM sessions "
            "WHERE title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' "
            "ORDER BY updated_ts DESC, rowid DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()]
        interactions = [dict(r) for r in conn.execute(
            "SELECT id, task, response, tier, session_id, ts FROM interactions "
            "WHERE task LIKE ? ESCAPE '\\' OR response LIKE ? ESCAPE '\\' "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()]
    finally:
        conn.close()

    lines = ["memory search: %r" % query]
    lines.append("lessons (%d):" % len(lessons))
    lines.extend("  - %s: %s" % (l["id"], l["text"][:220]) for l in lessons)
    lines.append("facts (%d):" % len(facts))
    lines.extend("  - %s/%s: %s" % (f["project"], f["id"], f["text"][:220]) for f in facts)
    lines.append("sessions (%d):" % len(sessions))
    lines.extend("  - %s [%s]: %s" % (
        s["session_id"], s.get("project") or "default",
        (s.get("title") or s.get("summary") or "(untitled)")[:220],
    ) for s in sessions)
    lines.append("interactions (%d):" % len(interactions))
    lines.extend("  - %s [%s]: %s" % (
        i["id"], i.get("tier") or "?",
        (i.get("task") or "")[:220],
    ) for i in interactions)
    return "\n".join(lines)


@mcp.tool()
def learn_from_example(task: str, solution: str, signal: str = "accepted") -> str:
    """Distill a reusable lesson from a known-good example.

    This is a direct teaching path: provide a task and solution that worked, and
    trilobite will try to extract one concrete lesson into memory. Use grounded
    signals like accepted, tests_passed, or compiled for best results.
    """
    _maybe_live_reload()
    if signal not in reward.VALID_SIGNALS:
        return "ERROR: unknown signal '%s'. Valid: %s." % (
            signal, ", ".join(sorted(reward.VALID_SIGNALS)))
    if not (task or "").strip() or not (solution or "").strip():
        return "ERROR: task and solution are required."
    conn = _open_db()
    try:
        interaction_id = memory_store.new_id()
        emb = embeddings.embed(task)
        blob = embeddings.to_blob(emb) if emb else None
        memory_store.log_interaction(
            conn, interaction_id, task, "", solution, "example", task_embedding=blob)
        r = reward.score(signal)
        memory_store.record_outcome_row(conn, interaction_id, signal, r)
        lesson_id = None
        if reward.is_good(signal):
            lesson_id = reflection.maybe_add_lesson(
                conn, interaction_id, task, solution, signal,
                offload_fn=_generate_text, embed_fn=embeddings.embed,
            )
    finally:
        conn.close()
    if lesson_id:
        return "Learned lesson %s from example interaction %s." % (lesson_id, interaction_id)
    return "Example recorded as %s, but no non-duplicate concrete lesson was distilled." % interaction_id


@mcp.tool()
def apply_learned(task: str, limit: int = 5) -> str:
    """Show which learned lessons would be applied to a task."""
    _maybe_live_reload()
    task = (task or "").strip()
    if not task:
        return "ERROR: empty task."
    limit = _safe_limit(limit, 5, 20)
    conn = _open_db()
    try:
        rows = retriever.retrieve_with_ids(conn, task, k=limit)
        stats = memory_store.lesson_usage_stats(conn)
    finally:
        conn.close()
    if not rows:
        return "No learned lessons were relevant enough for this task."
    lines = ["learned lesson application plan", "task: %s" % task, ""]
    for i, row in enumerate(rows, start=1):
        st = stats.get(row["id"], {})
        lines.append("%d. %s" % (i, row["text"]))
        lines.append("   lesson_id=%s uses=%s wins=%s losses=%s" % (
            row["id"], st.get("uses", 0), st.get("wins", 0), st.get("losses", 0)))
        lines.append("   apply by treating it as a constraint or tactic for the task.")
    return "\n".join(lines)


@mcp.tool()
def memory_export(limit: int = 50, include_interactions: bool = False) -> str:
    """Export a compact JSON snapshot of local memory."""
    _maybe_live_reload()
    limit = _safe_limit(limit, 50, 200)
    conn = _open_db()
    try:
        data = {
            "lessons": memory_store.recent_lessons(conn, limit=limit),
            "sessions": memory_store.list_sessions(conn, limit=limit),
            "facts": [dict(r) for r in conn.execute(
                "SELECT id, project, text, ts FROM facts ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()],
            "outcomes": memory_store.outcome_signal_counts(conn),
        }
        if include_interactions:
            data["interactions"] = [dict(r) for r in conn.execute(
                "SELECT id, task, response, tier, session_id, ts FROM interactions "
                "ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()]
    finally:
        conn.close()
    return json.dumps(data, indent=2, sort_keys=True)


@mcp.tool()
def session_export(session: str = "", limit: int = 50) -> str:
    """Export a remembered conversation session as readable transcript text."""
    _maybe_live_reload()
    session_id = _resolve_session(session)
    if not session_id:
        return "ERROR: session='none' has no stored transcript."
    limit = _safe_limit(limit, 50, 200)
    conn = _open_db()
    try:
        sess = memory_store.get_session(conn, session_id)
        if sess is None:
            found = memory_store.find_session(conn, session_id)
            if found:
                session_id = found
                sess = memory_store.get_session(conn, session_id)
        if sess is None:
            return "ERROR: no session '%s'." % session
        turns = memory_store.session_turns(conn, session_id)[-limit:]
    finally:
        conn.close()
    lines = [
        "session: %s" % session_id,
        "title: %s" % (sess.get("title") or "(untitled)"),
        "project: %s" % (sess.get("project") or "(none)"),
        "",
    ]
    for turn in turns:
        lines.append("USER: %s" % (turn.get("task") or ""))
        lines.append("ASSISTANT: %s" % (turn.get("response") or ""))
        lines.append("")
    return "\n".join(lines).rstrip()


@mcp.tool()
def tool_manifest() -> str:
    """List the local-llm MCP tools and what they are for."""
    tools = {
        "agent": "Run a Claude-like tool-calling loop that can use local tools and web tools.",
        "trilobite": "Ask the local self-improving coding model.",
        "offload": "Route a self-contained task to a configured local/cloud tier.",
        "web_search/web_fetch": "Search or fetch public web pages.",
        "run_code": "Run a bounded Python/JS/PowerShell/C++/C# snippet.",
        "run_project": "Run a bounded temporary multi-file project with optional build commands.",
        "loop": "Repeat bounded code/model/system actions.",
        "workflow_list/save/run/delete": "Manage reusable loop workflows.",
        "system_profile_text/update_system_profile": "Read or edit standing instructions.",
        "emotion_vector_status/update_emotion_vectors": "Read or edit tone vectors.",
        "memory_search/memory_export/session_export": "Inspect local memory.",
        "memory_quality_report/memory_quality_repair": "Audit and dry-run/prune exact duplicate lessons.",
        "learn_from_example/apply_learned": "Teach from examples and preview lesson application.",
        "self_heal_check/self_heal_repair": "Detect and safely repair common local breakage.",
        "context_health/diagnostics/live_reload_status/status/unload": "Observe and manage runtime health.",
        "record_outcome": "Feed grounded outcomes back into learning.",
        "trilobite_stats/trilobite_sessions/trilobite_remember_fact": "Memory observability and durable facts.",
    }
    return "\n".join("  %s: %s" % item for item in sorted(tools.items()))


AGENT_TOOL_HELP = """Available tools:
- run_code: {"code": "...", "language": "python|js|powershell|cpp|csharp", "stdin": "", "timeout": 10}
- run_project: {"files_json": {"files": {"src/main.cpp": "..."}}, "commands_json": [{"cmd": ["g++", "src/main.cpp", "-o", "app"]}], "stdin": "", "timeout": 60}
- web_search: {"query": "...", "limit": 5}
- web_fetch: {"url": "https://...", "max_chars": 8000}
- memory_search: {"query": "...", "limit": 10}
- apply_learned: {"task": "...", "limit": 5}
- workflow_run: {"name": "...", "max_iterations": 1}
- diagnostics: {}
- context_health: {}
- memory_quality_report: {"sample_limit": 5}
- memory_quality_repair: {"apply": false}
- self_heal_check: {}
- self_heal_repair: {"apply": false}
- status: {}
- system_profile_text: {}
- emotion_vector_status: {}
- tool_manifest: {}
- offload: {"prompt": "...", "tier": "fast|code|general|cloud-code|cloud-general"}

Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}, "reason": "short reason"}
or
{"final": "your final answer"}
"""


def _extract_agent_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("agent response was not JSON: %s" % text[:300])
        return json.loads(text[start:end + 1])


def _agent_dispatch(tool_name, args, allow_web=True):
    tool_name = (tool_name or "").strip()
    args = args or {}
    if not isinstance(args, dict):
        return "ERROR: tool args must be a JSON object"
    if tool_name == "run_code":
        return run_code(
            code=args.get("code", ""),
            language=args.get("language", "python"),
            stdin=args.get("stdin", ""),
            timeout=args.get("timeout", 10),
        )
    if tool_name == "run_project":
        return run_project(
            files_json=args.get("files_json", args.get("files", [])),
            commands_json=args.get("commands_json", args.get("commands", "")),
            stdin=args.get("stdin", ""),
            timeout=args.get("timeout", 60),
        )
    if tool_name == "web_search":
        if not allow_web:
            return "ERROR: web access disabled for this agent run"
        return web_search(args.get("query", ""), args.get("limit", 5))
    if tool_name == "web_fetch":
        if not allow_web:
            return "ERROR: web access disabled for this agent run"
        return web_fetch(args.get("url", ""), args.get("max_chars", 8000))
    if tool_name == "memory_search":
        return memory_search(args.get("query", ""), args.get("limit", 10))
    if tool_name == "apply_learned":
        return apply_learned(args.get("task", ""), args.get("limit", 5))
    if tool_name == "workflow_run":
        return workflow_run(
            args.get("name", ""),
            max_iterations=args.get("max_iterations", 1),
            stop_on_failure=args.get("stop_on_failure", True),
            stop_on_success=args.get("stop_on_success", False),
            delay_seconds=args.get("delay_seconds", 0),
        )
    if tool_name == "diagnostics":
        return diagnostics()
    if tool_name == "context_health":
        return context_health(
            session=args.get("session", ""),
            project=args.get("project", ""),
        )
    if tool_name == "memory_quality_report":
        return memory_quality_report(sample_limit=args.get("sample_limit", 5))
    if tool_name == "memory_quality_repair":
        return memory_quality_repair(apply=args.get("apply", False))
    if tool_name == "self_heal_check":
        return self_heal_check()
    if tool_name == "self_heal_repair":
        return self_heal_repair(apply=args.get("apply", False))
    if tool_name == "status":
        return status()
    if tool_name == "system_profile_text":
        return system_profile_text()
    if tool_name == "emotion_vector_status":
        return emotion_vector_status()
    if tool_name == "tool_manifest":
        return tool_manifest()
    if tool_name == "offload":
        return offload(
            prompt=args.get("prompt", ""),
            tier=args.get("tier", "fast"),
            system=args.get("system", ""),
            temperature=args.get("temperature", 0.2),
            num_predict=args.get("num_predict", 1024),
            num_ctx=args.get("num_ctx", 4096),
            learn=args.get("learn", False),
        )
    return "ERROR: unknown tool '%s'." % tool_name


@mcp.tool()
def agent(
    prompt: str,
    tier: str = "code",
    max_steps: int = 6,
    allow_web: bool = True,
) -> str:
    """Run a Claude-like local agent loop that can call tools.

    The model chooses one JSON tool call at a time, receives the observation,
    and continues until it returns {"final": "..."} or max_steps is reached.
    Tools include code execution, memory search, workflows, diagnostics, and
    public web search/fetch when allow_web=True and TRILOBITE_WEB_TOOLS is on.
    """
    _maybe_live_reload()
    max_steps = _safe_limit(max_steps, 6, 12)
    model, cloud, augment, tier_label = _serve_target(tier, None)
    if tier_label is None:
        return "ERROR: unknown tier '%s'. Valid: trilobite, %s." % (tier, ", ".join(TIERS))
    if model is None:
        return "ERROR: trilobite model/alias not found."
    system = _build_system(
        "You are a local tool-using agent. Decide when tools are useful. "
        "Use web tools for current external information and cite fetched URLs in the final answer. "
        "Do not invent tool results.",
        False,
        "",
    )
    gen = _make_generate(model, system, 0.1, 1200, SESSION_NUM_CTX, cloud=cloud)
    observations = []
    transcript = "Task:\n%s\n\n%s" % (prompt, AGENT_TOOL_HELP)
    for step in range(1, max_steps + 1):
        step_prompt = transcript
        if observations:
            step_prompt += "\n\nTool observations so far:\n" + "\n\n".join(observations)
        step_prompt += "\n\nChoose the next tool call or final answer."
        raw = gen(step_prompt)
        try:
            decision = _extract_agent_json(raw)
        except Exception as e:
            return "ERROR: could not parse agent decision at step %d: %s\nraw=%s" % (
                step, e, raw[:1000])
        if not isinstance(decision, dict):
            return "ERROR: agent decision must be a JSON object."
        if "final" in decision:
            return str(decision.get("final") or "")
        tool_name = decision.get("tool")
        if not tool_name:
            return "ERROR: agent decision missing 'tool' or 'final': %s" % decision
        observation = _agent_dispatch(tool_name, decision.get("args", {}), allow_web=allow_web)
        observations.append(
            "step %d tool=%s reason=%s\n%s" % (
                step,
                tool_name,
                decision.get("reason", ""),
                observation[:6000],
            )
        )
    return "ERROR: agent reached max_steps=%d without final answer.\n\n%s" % (
        max_steps, "\n\n".join(observations))


@mcp.tool()
def self_heal_check() -> str:
    """Check for common local breakage without changing anything."""
    _maybe_live_reload()
    issues = self_heal.check(_DB_PATH, module_names=LIVE_RELOAD_MODULES)
    return self_heal.format_report(issues)


@mcp.tool()
def self_heal_repair(apply: bool = False) -> str:
    """Repair safe local issues, or dry-run by default.

    Safe repairs include rebuilding missing lesson FTS rows, removing orphan FTS
    rows, clearing corrupt lesson embeddings, and restoring default JSON config
    files after backing up invalid ones. Broken Python/venv and live-reload syntax
    errors are reported but not auto-fixed.
    """
    _maybe_live_reload()
    issues, actions = self_heal.repair(
        _DB_PATH,
        module_names=LIVE_RELOAD_MODULES,
        apply=bool(apply),
    )
    return self_heal.format_report(issues, actions=actions)


@mcp.tool()
def diagnostics() -> str:
    """Run lightweight health checks for the local trilobite system."""
    _maybe_live_reload()
    lines = ["trilobite diagnostics"]
    lines.append("  live reload: %s" % ("on" if live_reload.enabled() else "off"))
    runtime = _local_runtime_summary()
    lines.append("  local runtime: threads=%s, gpu_layers=%s, batch=%s" % (
        runtime["num_thread"], runtime["num_gpu"], runtime["num_batch"]))
    try:
        profile_text, profile_path = system_profile.ensure_profile()
        lines.append("  system profile: ok (%s, %d chars)" % (
            profile_path, len(profile_text)))
    except Exception as e:
        lines.append("  system profile: ERROR %s" % e)
    try:
        vectors, vector_path = emotion_vectors.ensure_vectors()
        active = sum(1 for value in vectors.values() if abs(value) >= 0.001)
        lines.append("  emotion vectors: ok (%s, %d active)" % (
            vector_path, active))
    except Exception as e:
        lines.append("  emotion vectors: ERROR %s" % e)
    try:
        conn = _open_db()
        try:
            n_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
            n_interactions = memory_store.count_interactions(conn)
        finally:
            conn.close()
        lines.append("  memory db: ok (%s, %d lessons, %d interactions)" % (
            _DB_PATH, n_lessons, n_interactions))
    except Exception as e:
        lines.append("  memory db: ERROR %s" % e)
    try:
        ctx = context_health_data()
        lines.append("  context: %s %s%% (~%s/%s tokens), live turns %s/%s" % (
            ctx["status"], ctx["context_percent"], ctx["estimated_tokens"],
            ctx["context_limit"], ctx["live_turns"], ctx["max_live_turns"]))
    except Exception as e:
        lines.append("  context: ERROR %s" % e)
    try:
        conn = _open_db()
        try:
            quality = memory_quality.audit(conn)
        finally:
            conn.close()
        lines.append("  memory quality: %d duplicate group(s), %d prunable, %d no embedding" % (
            quality["exact_duplicate_groups"], quality["exact_duplicate_prunable"],
            quality["no_embedding"]))
    except Exception as e:
        lines.append("  memory quality: ERROR %s" % e)
    try:
        heal_issues = self_heal.check(_DB_PATH, module_names=LIVE_RELOAD_MODULES)
        repairable = sum(1 for issue in heal_issues if issue.repairable)
        lines.append("  self heal: %s (%d repairable)" % (
            "ok" if not heal_issues else "%d issue(s)" % len(heal_issues),
            repairable,
        ))
    except Exception as e:
        lines.append("  self heal: ERROR %s" % e)
    try:
        tags = _get("/api/tags").get("models", [])
        names = sorted(m.get("name", "?") for m in tags)
        lines.append("  ollama: ok (%d models: %s)" % (
            len(names), ", ".join(names[:8]) if names else "none"))
    except Exception as e:
        lines.append("  ollama: ERROR %s" % e)
    lines.append("  web tools: %s" % ("on" if web_tools.enabled() else "off"))
    return "\n".join(lines)


@mcp.tool()
def status() -> str:
    """Report local-LLM state: which models are installed, and which are currently in VRAM.

    Use this to check whether the GPU is busy before offloading, or to confirm models pulled.
    """
    _maybe_live_reload()
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
        f"  {k}={v}" + ("  [CLOUD — leaves machine]" if _is_cloud_tier(k, v) else "  [local GPU]")
        for k, v in TIERS.items()
    ]
    lines = [
        f"Ollama @ {BASE}",
        "Tiers:",
        *tier_lines,
        f"Learning tiers: {', '.join(sorted(LEARN_TIERS)) if LEARN_TIERS else '(none)'}",
        f"Installed/registered models: {', '.join(installed) if installed else '(none)'}",
        f"In VRAM now: {', '.join(loaded) if loaded else '(none — GPU idle)'}",
        f"local keep_alive: {KEEP_ALIVE}",
        "local runtime: threads={num_thread}, gpu_layers={num_gpu}, batch={num_batch}".format(
            **_local_runtime_summary()
        ),
    ]
    return "\n".join(lines)


@mcp.tool()
def unload(tier: str = "all") -> str:
    """Immediately free GPU VRAM by unloading a model (or all of them).

    Args:
        tier: "all" (default), or one of "fast", "code", "general".
    """
    _maybe_live_reload()
    if tier == "all":
        # Only local tiers occupy VRAM; cloud tiers run remote.
        targets = [v for k, v in TIERS.items() if not _is_cloud_tier(k, v)]
    elif _is_cloud_tier(tier):
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
