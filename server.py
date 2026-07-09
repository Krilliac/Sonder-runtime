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
import domain_grounding
import master_orchestrator
import admin_auth
import file_ops
import context_policy
import command_registry
import permission_rules
import debug_dump

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
        "num_ctx": context_policy.native(num_ctx),
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
        "num_ctx_native": options.get("num_ctx", "ollama-default"),
        "num_ctx_requested": context_policy.requested(SESSION_NUM_CTX),
    }


def _context_requested(value=None):
    return context_policy.requested(SESSION_NUM_CTX if value in (None, "") else value)


def _context_native(value=None):
    return context_policy.native(_context_requested(value))


TIERS = {
    "fast": os.environ.get("LOCAL_LLM_FAST", "qwen2.5:3b"),
    "code": os.environ.get("LOCAL_LLM_CODE", "qwen2.5-coder:7b"),
    "general": os.environ.get("LOCAL_LLM_GENERAL", "qwen2.5:7b-instruct"),
    "cloud-code": os.environ.get("LOCAL_LLM_CLOUD_CODE", "qwen3-coder:480b-cloud"),
    "cloud-general": os.environ.get("LOCAL_LLM_CLOUD_GENERAL", "gpt-oss:120b-cloud"),
}
# Tiers whose ":...-cloud" model runs on Ollama's servers (data leaves the machine).
CLOUD_TIERS = {"cloud-code", "cloud-general"}
LOCAL_TIERS = tuple(k for k in TIERS if k not in CLOUD_TIERS)


def cloud_allowed():
    return os.environ.get("TRILOBITE_ALLOW_CLOUD", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def available_tiers(include_disabled=False):
    if include_disabled or cloud_allowed():
        return dict(TIERS)
    return {k: v for k, v in TIERS.items() if k not in CLOUD_TIERS}


def _valid_tier_names():
    return ", ".join(available_tiers())


def _cloud_disabled_message():
    return (
        "ERROR: hosted/cloud tiers are disabled. Set TRILOBITE_ALLOW_CLOUD=1 "
        "to opt in; prompts sent to cloud tiers leave this machine."
    )


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
DEFAULT_LEARN_TIERS = ",".join(LOCAL_TIERS)
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
SESSION_NUM_CTX = context_policy.default_requested()
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
    "domain_grounding",
    "master_orchestrator",
    "admin_auth",
    "file_ops",
    "context_policy",
    "command_registry",
    "permission_rules",
    "debug_dump",
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
        if _is_cloud_tier(t, model) and not cloud_allowed():
            return None, True, False, "cloud-disabled"
        return model, _is_cloud_tier(t, model), t == "code", t
    return None, False, True, None


def _control_history_messages(history, prompt):
    messages = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content") or ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    if prompt:
        messages.append({"role": "user", "content": prompt})
    return messages


def _latest_runnable_block(history):
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        block = grounding.extract_runnable_code_block(msg.get("content") or "")
        if block:
            return block
    return None


def _latest_project_files(history):
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        files = grounding.extract_project_files(msg.get("content") or "")
        if files:
            return files
    return []


def _parse_control_timeout(arg, command="/run"):
    arg = (arg or "").strip()
    if not arg:
        return grounding.DEFAULT_TIMEOUT, None
    try:
        return grounding.clamp_timeout(int(arg)), None
    except ValueError:
        return None, "usage: %s [seconds]  (runs the previous fenced code block)" % command


def _control_run(arg, history=None):
    timeout, err = _parse_control_timeout(arg, "/run")
    if err:
        return err
    block = _latest_runnable_block(history)
    if not block:
        return (
            "/run needs a previous assistant message with a fenced runnable code "
            "block. Use it from the REPL/app after a code answer."
        )
    result = code_runner.run_code(
        block["code"],
        language=block["language"],
        timeout=timeout,
    )
    if result.get("ok"):
        status = "[ran OK]"
    elif result.get("returncode") is None and result.get("error", "").startswith("timed out"):
        status = "[timed out]"
    else:
        status = "[exited with error]"
    return "%s\n%s" % (code_runner.format_result(result), status)


def _control_runproject(arg, history=None):
    timeout, err = _parse_control_timeout(arg, "/runproject")
    if err:
        return err
    files = _latest_project_files(history)
    if not files:
        return (
            "/runproject needs previous file/path fenced project blocks. Use it "
            "from the REPL/app after a project-style answer."
        )
    result = code_runner.run_project({"files": files}, timeout=timeout)
    status = "[ran OK]" if result.get("ok") else "[project failed]"
    return "%s\n%s" % (code_runner.format_project_result(result), status)


def _control_dump(arg, prompt, history=None, session="", project=""):
    label = (arg or "server").strip() or "server"
    messages = _control_history_messages(history, prompt)
    session_id = _resolve_session(session)
    project_id = _resolve_project(project)
    if session_id:
        conn = _open_db()
        try:
            for turn in memory_store.session_turns(conn, session_id):
                messages.append({"role": "user", "content": turn.get("task") or ""})
                messages.append({"role": "assistant", "content": turn.get("response") or ""})
        finally:
            conn.close()
    sections = [
        ("session", session_id or "(none)"),
        ("project", project_id or "(none)"),
        ("context", context_health(session=session_id or "none", project=project_id or "none")),
        ("quality", memory_quality_report(sample_limit=5)),
        ("agents", master_status(limit=20)),
        ("diagnostics", diagnostics()),
    ]
    path = debug_dump.write_dump(
        trilobite_paths.default_home(),
        label=label,
        messages=messages,
        sections=sections,
    )
    out = "dumped chat/debug log to %s" % path
    block = _latest_runnable_block(history)
    if block:
        out += (
            "\n\nlast runnable block retained for /run:\n```%s\n%s\n```"
            % (block["language"], block["code"])
        )
    return out


def control_command(prompt: str, history=None, session="", project=""):
    """Handle safe slash commands before a prompt reaches the model.

    Client layers have richer commands like /run that depend on their local last
    response. This guard catches read-only/status commands for direct MCP/API
    calls too, so `/quality` and `/context` never get treated as ordinary model
    prompts.
    """
    text = (prompt or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/stats":
        return trilobite_stats()
    if cmd == "/context":
        return context_health()
    if cmd in ("/contextsize", "/ctxsize"):
        return set_context_size(arg.strip()) if arg.strip() else context_policy_status()
    if cmd in ("/compact", "/compaction"):
        return context_compaction_plan()
    if cmd in ("/commands", "/cmds"):
        return command_registry_list(arg.strip())
    if cmd in ("/permissions", "/perms"):
        return permission_policy(arg.strip())
    if cmd == "/quality":
        return memory_quality_report()
    if cmd == "/qualityfix":
        return memory_quality_repair(apply=(arg.strip().lower() == "apply"))
    if cmd in ("/improve", "/improvements"):
        return system_improvement_report()
    if cmd in ("/agents", "/masterstatus"):
        return master_status()
    if cmd in ("/cot", "/chainofthought", "/thoughts"):
        return admin_private_chain_of_thought()
    if cmd == "/run":
        return _control_run(arg, history=history)
    if cmd == "/runproject":
        return _control_runproject(arg, history=history)
    if cmd == "/dump":
        return _control_dump(arg, text, history=history, session=session, project=project)
    return None


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
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, _valid_tier_names())
    if _is_cloud_tier(tier, model) and not cloud_allowed():
        return _cloud_disabled_message()

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
    context_size: str = "",
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
    record_outcome(<id>, "tests_passed" | "used" | "copied" | "edited" |
    "accepted" | "compiled" | "rejected" | "failed") so trilobite gets better over time. Defaults to the 7B coder base model,
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
    command = control_command(prompt, session=session, project=project)
    if command is not None:
        return command
    tgt_model, cloud, augment, tier_label = _serve_target(tier, strict)
    if tier_label == "cloud-disabled":
        return _cloud_disabled_message()
    if tier_label is None:
        return "ERROR: unknown tier '%s'. Valid: trilobite, %s." % (tier, _valid_tier_names())
    if tgt_model is None:
        return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                "with strict=False to fall back to the base coder.")
    effective_system = _build_system(system, trace, persona)

    session_id = _resolve_session(session)
    project_id = _resolve_project(project)
    requested_ctx = _context_requested(context_size or (SESSION_NUM_CTX if session_id else num_ctx))
    # Sessioned threads get the selected virtual context window; honor a larger explicit num_ctx.
    num_ctx_eff = max(num_ctx, requested_ctx) if session_id else requested_ctx

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
        params = {
            "temperature": temperature,
            "num_predict": num_predict,
            "num_ctx": num_ctx_eff,
            "num_ctx_native": _context_native(num_ctx_eff),
        }
        trace_block = _format_trace(tgt_model, tier_label, params, trace_ctx)
        # Footer must stay LAST so parse_interaction_id's $-anchored regex still finds it.
        return with_footer(response + trace_block, iid)
    return with_footer(response, iid)


def answer_with_history(prompt, history, trace=False, strict=None, tier=None, context_size=""):
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
    command = control_command(prompt, history=history)
    if command is not None:
        return command
    model, cloud, augment, tier_label = _serve_target(tier, strict)
    if tier_label == "cloud-disabled":
        return _cloud_disabled_message()
    if tier_label is None:
        return "ERROR: unknown model '%s'. Valid: trilobite, %s." % (
            tier, _valid_tier_names())
    if model is None:
        return ("ERROR: trilobite model/alias not found. Run setup_alias.py, or call "
                "with strict=False to fall back to the base coder.")
    effective_system = _build_system("", trace, "")
    # Honor LEARN_TIERS here too. Serve conversation memory is client-side (the app
    # resends history each request), so a non-learning model can skip capture entirely:
    # no interaction row, no footer, nothing distilled. This lets a user exclude e.g.
    # cloud from learning and have the app respect it. The student is gated via 'code'.
    learn = _should_learn(_canonical_learn_tier(tier_label), True)
    req_ctx = _context_requested(context_size or SESSION_NUM_CTX)
    conn = _open_db()
    try:
        if learn:
            project = DEFAULT_PROJECT if augment else None
            response, iid, trace_ctx = _answer(
                conn, prompt, model, effective_system, 0.2, 1024, req_ctx,
                None, project, history or None, trace=trace,
                tier=tier_label, cloud=cloud, augment=augment,
            )
        else:
            gen = _make_generate(model, effective_system, 0.2, 1024,
                                 req_ctx, cloud=cloud)
            response = gen(prompt, history or None)
            iid, trace_ctx = None, None
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (the tray app / `ollama serve`)" % (BASE, e))
    finally:
        conn.close()
    if trace and trace_ctx is not None:
        params = {
            "temperature": 0.2,
            "num_predict": 1024,
            "num_ctx": req_ctx,
            "num_ctx_native": _context_native(req_ctx),
        }
        trace_block = _format_trace(model, tier_label, params, trace_ctx)
        return with_footer(response + trace_block, iid)
    if iid is not None:
        return with_footer(response, iid)
    return response


@mcp.tool()
def record_outcome(interaction_id: str, signal: str) -> str:
    """Feed a real-world outcome back into trilobite's learning loop.

    Call this after a trilobite/offload response once you know how it went.
    signal is one of: tests_passed, used, copied, edited, accepted, compiled,
    rejected, failed.
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
def ground_artifact(artifact: str, checks_json: str) -> str:
    """Validate non-code artifacts with deterministic checks.

    checks_json is a JSON list of checks such as:
      {"type":"contains","text":"..."},
      {"type":"regex","pattern":"..."},
      {"type":"json"},
      {"type":"json_field","path":"a.b","equals":3}.
    Use the pass/fail result as a grounded signal for writing, configs, plans,
    structured data, and other domains where compile/run is not the test.
    """
    _maybe_live_reload()
    try:
        checks = json.loads(checks_json)
        result = domain_grounding.evaluate(artifact, checks)
    except Exception as e:
        return "ERROR: %s" % e
    return domain_grounding.format_result(result)


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
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, _valid_tier_names())
    if _is_cloud_tier(tier, model) and not cloud_allowed():
        return _cloud_disabled_message()
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
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, _valid_tier_names())
    if _is_cloud_tier(tier, model) and not cloud_allowed():
        return _cloud_disabled_message()
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

    policy = context_policy.policy(SESSION_NUM_CTX)
    context_limit = max(1, int(policy["requested"] or 1))
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
        "native_context_limit": policy["native"],
        "native_context_max": policy["native_max"],
        "virtual_context_max": policy["virtual_max"],
        "context_mode": policy["mode"],
        "virtual_context": policy["virtual"],
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
        "  native  ~%s token Ollama num_ctx (%s mode)" % (
            data.get("native_context_limit", 0),
            data.get("context_mode", "native"),
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
def context_policy_status(context_size: str = "") -> str:
    """Show requested virtual context and actual Ollama native num_ctx."""
    _maybe_live_reload()
    return context_policy.format_policy(context_size or SESSION_NUM_CTX)


@mcp.tool()
def set_context_size(context_size: str) -> str:
    """Select Trilobite's requested virtual context size, up to 1m by default."""
    global SESSION_NUM_CTX
    _maybe_live_reload()
    SESSION_NUM_CTX = context_policy.requested(context_size)
    return "context size selected\n" + context_policy.format_policy(SESSION_NUM_CTX)


@mcp.tool()
def command_registry_list(filter_text: str = "") -> str:
    """List slash commands/tools by name, category, risk, or summary text."""
    _maybe_live_reload()
    return command_registry.format_commands(filter_text)


def _format_task(row: dict) -> str:
    if not row:
        return "(no task)"
    detail = (" - " + row.get("detail", "")) if row.get("detail") else ""
    scope = []
    if row.get("project"):
        scope.append("project=%s" % row["project"])
    if row.get("owner"):
        scope.append("owner=%s" % row["owner"])
    suffix = (" [" + ", ".join(scope) + "]") if scope else ""
    return "%s  p%s  %-11s %s%s%s" % (
        row.get("id", "")[:8],
        row.get("priority", 2),
        row.get("status", "pending"),
        row.get("title", ""),
        detail,
        suffix,
    )


@mcp.tool()
def task_create(
    title: str,
    detail: str = "",
    priority: int = 2,
    project: str = "",
    owner: str = "",
) -> str:
    """Create a visible task/todo row the model, console, and app can inspect."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        row = memory_store.create_task(
            conn,
            title=title,
            detail=detail,
            priority=priority,
            project=project,
            owner=owner,
        )
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    return "task created\n  " + _format_task(row)


@mcp.tool()
def task_list(
    status: str = "",
    project: str = "",
    owner: str = "",
    include_done: bool = False,
    limit: int = 50,
) -> str:
    """List visible task/todo rows, pending and active by default."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        rows = memory_store.list_tasks(
            conn,
            status=status,
            project=project,
            owner=owner,
            include_done=bool(include_done),
            limit=limit,
        )
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    lines = ["trilobite tasks"]
    if not rows:
        lines.append("  (no matching tasks)")
    for row in rows:
        lines.append("  " + _format_task(row))
    return "\n".join(lines)


@mcp.tool()
def task_update(
    task_id: str,
    status: str = "",
    title: str = "",
    detail: str = "",
    priority: str = "",
    project: str = "",
    owner: str = "",
    note: str = "",
) -> str:
    """Update task status/details. task_id may be an unambiguous id prefix."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        row = memory_store.update_task(
            conn,
            task_id,
            status=status or None,
            title=title or None,
            detail=detail if detail else None,
            priority=priority if priority else None,
            project=project if project else None,
            owner=owner if owner else None,
            note=note,
        )
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    return "task updated\n  " + _format_task(row)


@mcp.tool()
def task_show(task_id: str, events: bool = True) -> str:
    """Show one task and its recent visible event history."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        row = memory_store.get_task(conn, task_id)
        history = memory_store.task_events(conn, task_id, limit=20) if events else []
    finally:
        conn.close()
    if not row:
        return "ERROR: no task '%s'." % task_id
    lines = ["task", "  " + _format_task(row)]
    if history:
        lines.append("events:")
        for event in history:
            lines.append("  %(ts)s  %(event)s  %(note)s" % event)
    return "\n".join(lines)


def context_compaction_plan_data(session: str = "", project: str = "") -> dict:
    data = context_health_data(session=session, project=project)
    actions = []
    if data.get("context_percent", 0) >= 90:
        actions.append({
            "priority": "high",
            "action": "start a fresh session or summarize immediately",
            "reason": "estimated prompt tokens are above 90% of the selected context",
        })
    elif data.get("context_percent", 0) >= 70:
        actions.append({
            "priority": "medium",
            "action": "prefer summarizing older turns before adding large files",
            "reason": "context is warming up",
        })
    if data.get("live_turns", 0) >= data.get("max_live_turns", 0):
        actions.append({
            "priority": "medium",
            "action": "roll older turns into the session summary",
            "reason": "the live turn window is full",
        })
    if data.get("summary_chars", 0) > 16000:
        actions.append({
            "priority": "low",
            "action": "start a new session with the summary as a project fact",
            "reason": "the summary itself is becoming large",
        })
    if not actions:
        actions.append({
            "priority": "info",
            "action": "no compaction needed yet",
            "reason": "context and live-turn usage are healthy",
        })
    return {"context": data, "actions": actions}


def format_context_compaction_plan(plan: dict) -> str:
    ctx = plan.get("context", {})
    lines = [
        "trilobite context compaction plan",
        "  session: %s" % ctx.get("session", "none"),
        "  context: %s%%  ~%s/%s tokens (%s mode)" % (
            ctx.get("context_percent", 0),
            ctx.get("estimated_tokens", 0),
            ctx.get("context_limit", 0),
            ctx.get("context_mode", "native"),
        ),
        "  live turns: %s/%s | summary: ~%s tokens" % (
            ctx.get("live_turns", 0),
            ctx.get("max_live_turns", 0),
            ctx.get("summary_tokens", 0),
        ),
        "  recommended actions:",
    ]
    for item in plan.get("actions", []):
        lines.append("    [%s] %s" % (item.get("priority", "info"), item.get("action", "")))
        lines.append("        -> %s" % item.get("reason", ""))
    return "\n".join(lines)


@mcp.tool()
def context_compaction_plan(session: str = "", project: str = "") -> str:
    """Preview when/how Trilobite should summarize or split context."""
    _maybe_live_reload()
    return format_context_compaction_plan(context_compaction_plan_data(session, project))


@mcp.tool()
def permission_policy(tool_name: str = "") -> str:
    """Show local permission rules, or the matching rule for one tool."""
    _maybe_live_reload()
    return permission_rules.format_policy(trilobite_paths.default_home(), tool_name)


@mcp.tool()
def permission_rule_set(
    pattern: str,
    action: str,
    note: str = "",
    token: str = "",
) -> str:
    """Set a local permission rule. Developer token or explicit env opt-in required."""
    _maybe_live_reload()
    account = _admin_account_from_token(token) if token else None
    ok, _ = admin_auth.require(account, "developer")
    env_ok = os.environ.get("TRILOBITE_ALLOW_PERMISSION_EDITS", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if not ok and not env_ok:
        return (
            "ERROR: permission edits require a developer token or "
            "TRILOBITE_ALLOW_PERMISSION_EDITS=1."
        )
    try:
        permission_rules.add_rule(trilobite_paths.default_home(), pattern, action, note)
    except Exception as e:
        return "ERROR: %s" % e
    return permission_rules.format_policy(trilobite_paths.default_home())


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
    for tier, model in available_tiers(include_disabled=True).items():
        state = "on" if tier in LEARN_TIERS else "off"
        locality = "cloud" if _is_cloud_tier(tier, model) else "local"
        if locality == "cloud" and not cloud_allowed():
            state = "disabled"
        lines.append("  %s: %s (%s, %s)" % (tier, state, locality, model))
    lines.append("cloud tiers require TRILOBITE_ALLOW_CLOUD=1; override learning with TRILOBITE_LEARN_TIERS")
    return "\n".join(lines)


def improvement_report_data(session: str = "", project: str = "") -> dict:
    """Machine-readable next-step report for system self-improvement."""
    _maybe_live_reload()
    context = context_health_data(session=session, project=project)
    conn = _open_db()
    try:
        quality = memory_quality.audit(conn)
        interactions = memory_store.count_interactions(conn)
        signal_counts = memory_store.outcome_signal_counts(conn)
        lesson_count = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    finally:
        conn.close()

    outcomes = sum(signal_counts.values())
    acceptance = (
        sum(
            signal_counts.get(sig, 0)
            for sig in ("tests_passed", "accepted", "used", "copied", "edited", "compiled")
        )
        / max(1, outcomes)
    )
    issues = []

    def add(area, severity, title, action):
        issues.append({
            "area": area,
            "severity": severity,
            "title": title,
            "action": action,
        })

    if interactions and outcomes / max(1, interactions) < 0.35:
        add(
            "learning",
            "high",
            "Too few interactions have grounded outcomes.",
            "Use /accept, /edited, /copied, /pass, /fail, or record_outcome after real use.",
        )
    if interactions == 0:
        add(
            "learning",
            "medium",
            "No learning interactions have been captured yet.",
            "Ask through trilobite or run /train so answers can become local lessons.",
        )
    if lesson_count < 10:
        add(
            "memory",
            "medium",
            "Lesson memory is still thin.",
            "Run grounded training or teach examples from known-good work.",
        )
    if quality.get("exact_duplicate_prunable", 0):
        add(
            "memory",
            "medium",
            "Duplicate lessons can be pruned.",
            "Run memory_quality_repair(apply=True) or /qualityfix apply after review.",
        )
    if quality.get("path_or_secret_like", 0):
        add(
            "privacy",
            "high",
            "Some lessons look like they may contain paths or secrets.",
            "Review /quality output and remove or rewrite those lessons before contributing data.",
        )
    if quality.get("vague_without_anchor", 0):
        add(
            "memory",
            "low",
            "Some lessons are vague and lack concrete anchors.",
            "Prefer lessons naming APIs, files, errors, commands, or explicit patterns.",
        )
    if quality.get("missing_fts", 0) or quality.get("orphan_fts", 0):
        add(
            "store",
            "medium",
            "Search index drift was detected.",
            "Run self_heal_check and repair the store before large training sessions.",
        )
    if context.get("status") == "hot":
        add(
            "context",
            "medium",
            "The active conversation is near the context limit.",
            "Start a new session or let summaries compress older turns before continuing.",
        )
    if not cloud_allowed():
        add(
            "deployment",
            "info",
            "Hosted tiers are disabled, preserving the local privacy promise.",
            "Enable hosted/cloud tiers only when you intentionally want prompts to leave this machine.",
        )
    if "ground_artifact" not in tool_manifest():
        add(
            "grounding",
            "medium",
            "General artifact grounding is not advertised.",
            "Expose ground_artifact in the tool manifest so non-code work can be validated.",
        )
    if not issues:
        add(
            "system",
            "info",
            "No urgent improvement items detected.",
            "Keep collecting grounded outcomes and periodically run /quality.",
        )

    severity_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    issues.sort(key=lambda item: (severity_rank.get(item["severity"], 9), item["area"], item["title"]))
    return {
        "score": max(0, min(100, int(round(
            100
            - 18 * sum(1 for i in issues if i["severity"] == "high")
            - 9 * sum(1 for i in issues if i["severity"] == "medium")
            - 4 * sum(1 for i in issues if i["severity"] == "low")
        )))),
        "interactions": interactions,
        "outcomes": outcomes,
        "acceptance_percent": round(acceptance * 100.0, 1),
        "lessons": lesson_count,
        "facts": fact_count,
        "cloud_allowed": cloud_allowed(),
        "context_status": context.get("status", "unknown"),
        "memory_quality": {
            "duplicates": quality.get("exact_duplicate_prunable", 0),
            "vague": quality.get("vague_without_anchor", 0),
            "path_or_secret_like": quality.get("path_or_secret_like", 0),
            "fts_issues": quality.get("missing_fts", 0) + quality.get("orphan_fts", 0),
        },
        "issues": issues,
    }


def format_improvement_report(report: dict) -> str:
    lines = [
        "trilobite improvement report",
        "  readiness score: %s/100" % report.get("score", 0),
        "  learning: %s interactions, %s outcomes, %s%% positive/grounded" % (
            report.get("interactions", 0),
            report.get("outcomes", 0),
            report.get("acceptance_percent", 0),
        ),
        "  memory: %s lessons, %s facts, duplicate rows=%s, vague=%s" % (
            report.get("lessons", 0),
            report.get("facts", 0),
            report.get("memory_quality", {}).get("duplicates", 0),
            report.get("memory_quality", {}).get("vague", 0),
        ),
        "  context: %s | hosted/cloud: %s" % (
            report.get("context_status", "unknown"),
            "enabled" if report.get("cloud_allowed") else "disabled",
        ),
        "  next improvements:",
    ]
    for issue in report.get("issues", [])[:8]:
        lines.append("    [%s] %s: %s" % (
            issue.get("severity", "info"),
            issue.get("area", "system"),
            issue.get("title", ""),
        ))
        lines.append("        -> %s" % issue.get("action", ""))
    return "\n".join(lines)


@mcp.tool()
def system_improvement_report(session: str = "", project: str = "") -> str:
    """Suggest the next concrete improvements for learning quality and runtime health."""
    return format_improvement_report(improvement_report_data(session=session, project=project))


def _orchestrator_worker(tier: str, learn: bool = False):
    def worker(prompt: str) -> str:
        return offload(
            prompt=prompt,
            tier=tier,
            temperature=0.2,
            num_predict=1400,
            learn=learn,
        )
    return worker


@mcp.tool()
def master_orchestrate(
    task: str,
    mode: str = "ask",
    agents: int = 3,
    tier: str = "code",
    learn: bool = False,
) -> str:
    """Run a master orchestration pass inline or by delegated parallel agents.

    mode="ask" returns the choice prompt. mode="inline" keeps work in the master
    lane. mode="delegate" spawns bounded parallel subagents, then audits and
    merges their outputs. Status is visible through master_status().
    """
    _maybe_live_reload()
    task = (task or "").strip()
    mode = (mode or "ask").strip().lower()
    if mode in ("ask", "choose", "prompt"):
        return (
            "Master orchestrator ready.\n"
            "Choose execution mode:\n"
            "  inline   - master handles the task directly.\n"
            "  delegate - spawn %d parallel agent(s), audit their outputs, then merge.\n"
            "Call master_orchestrate(task, mode='inline'|'delegate') or chat `/master inline ...`."
        ) % max(1, min(8, int(agents or 3)))
    if not task:
        return "ERROR: empty task."
    worker = _orchestrator_worker(tier, learn=learn)
    if mode in ("inline", "master"):
        result = master_orchestrator.run_inline(task, worker)
        return result["output"]
    if mode in ("delegate", "delegated", "agents", "parallel"):
        result = master_orchestrator.run_delegated(
            task,
            worker_fn=worker,
            audit_fn=_orchestrator_worker(tier, learn=False),
            agents=agents,
        )
        lines = [
            "master orchestration complete",
            "mode: delegated | master=%s | agents=%d" % (
                result["master_id"], len(result.get("agents") or [])),
            "",
            result["output"],
        ]
        return "\n".join(lines).strip()
    return "ERROR: unknown mode '%s'. Use ask, inline, or delegate." % mode


@mcp.tool()
def master_status(include_finished: bool = True, limit: int = 20) -> str:
    """Show live master/subagent activity, token estimates, and recent actions."""
    _maybe_live_reload()
    return master_orchestrator.format_snapshot(
        master_orchestrator.snapshot(include_finished=include_finished, limit=limit)
    )


def _admin_account_from_token(token: str):
    conn = _open_db()
    try:
        return admin_auth.authenticate(conn, token)
    finally:
        conn.close()


def _admin_require(token: str, role: str = "admin"):
    account = _admin_account_from_token(token)
    ok, msg = admin_auth.require(account, role)
    return ok, msg, account


def _format_account(account: dict) -> str:
    return (
        "%(username)s role=%(role)s tier=%(tier)s banned=%(banned)s "
        "dev_flags=%(dev_flags)s"
    ) % account


@mcp.tool()
def admin_register(username: str, password: str) -> str:
    """Register a local hosted account. The first account becomes admin."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        account = admin_auth.register(conn, username, password)
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    return "registered %s" % _format_account(account)


@mcp.tool()
def admin_login(username: str, password: str) -> str:
    """Login and return a bearer token for admin/debug commands and hosted API use."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        token, account = admin_auth.login(conn, username, password)
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    return "login ok\n%s\ntoken: %s" % (_format_account(account), token)


@mcp.tool()
def admin_whoami(token: str = "") -> str:
    """Show the account attached to a session token."""
    _maybe_live_reload()
    account = _admin_account_from_token(token)
    if not account:
        return "not logged in"
    return _format_account(account)


@mcp.tool()
def admin_accounts(token: str = "", limit: int = 50) -> str:
    """List hosted accounts. Admin token required."""
    _maybe_live_reload()
    ok, msg, _ = _admin_require(token, "admin")
    if not ok:
        return "ERROR: %s." % msg
    conn = _open_db()
    try:
        accounts = admin_auth.list_accounts(conn, limit=limit)
    finally:
        conn.close()
    if not accounts:
        return "no accounts"
    return "\n".join(_format_account(a) for a in accounts)


@mcp.tool()
def admin_set_account(
    token: str,
    username: str,
    role: str = "",
    tier: str = "",
    dev_flags: str = "",
    banned: str = "",
) -> str:
    """Update role/tier/dev flags/ban state. Admin token required."""
    _maybe_live_reload()
    ok, msg, _ = _admin_require(token, "admin")
    if not ok:
        return "ERROR: %s." % msg
    changes = {}
    if role:
        changes["role"] = role
    if tier:
        changes["tier"] = tier
    if dev_flags:
        changes["dev_flags"] = dev_flags
    if str(banned).strip().lower() in ("1", "true", "yes", "on", "ban", "banned"):
        changes["banned"] = True
    elif str(banned).strip().lower() in ("0", "false", "no", "off", "unban"):
        changes["banned"] = False
    conn = _open_db()
    try:
        account = admin_auth.set_account(conn, username, **changes)
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        conn.close()
    return "updated %s" % _format_account(account)


@mcp.tool()
def admin_status(token: str = "") -> str:
    """Show hosted/admin safety state. Developer token recommended, local-safe without token."""
    _maybe_live_reload()
    account = _admin_account_from_token(token)
    conn = _open_db()
    try:
        count = admin_auth.account_count(conn)
    finally:
        conn.close()
    lines = [
        "trilobite admin status",
        "  accounts: %d" % count,
        "  auth mode: %s" % ("api-key" if os.environ.get("TRILOBITE_API_KEY") else "local-open"),
        "  require account: %s" % os.environ.get("TRILOBITE_REQUIRE_ACCOUNT", "0"),
        "  hosted/cloud allowed: %s" % ("yes" if cloud_allowed() else "no"),
        "  logged in: %s" % (_format_account(account) if account else "no"),
        "  safeguards: role gates, bans, session tokens, per-tier rate limits, bounded execution",
    ]
    return "\n".join(lines)


@mcp.tool()
def debug_inspect(token: str = "", include_status: bool = True) -> str:
    """Developer/admin inspection bundle without hidden chain-of-thought."""
    _maybe_live_reload()
    account = _admin_account_from_token(token)
    if token:
        ok, msg = admin_auth.require(account, "developer")
        if not ok:
            return "ERROR: %s." % msg
    sections = [
        "trilobite debug inspect",
        "  note: private hidden chain-of-thought is not exposed; use trace/tool/activity logs instead.",
        "",
        admin_status(token),
        "",
        master_status(limit=10),
        "",
        system_improvement_report(),
        "",
        memory_quality_report(sample_limit=3),
    ]
    if include_status:
        sections.extend(["", status()])
    return "\n".join(sections)


@mcp.tool()
def admin_private_chain_of_thought(token: str = "") -> str:
    """Deny private chain-of-thought exposure and point to safe inspectable traces."""
    _maybe_live_reload()
    return (
        "DENIED: hidden private chain-of-thought cannot be exposed. "
        "Use /trace, /debug, /agents, master_status, debug_inspect, "
        "tool call logs, prompts, retrieved lessons, and final rationale summaries instead."
    )


def _file_bypass_allowed(token: str = "", approval: str = "") -> bool:
    if file_ops.bypass_enabled():
        return True
    expected = os.environ.get("TRILOBITE_FILE_APPROVAL_CODE", "").strip()
    if expected and approval and approval == expected:
        return True
    account = _admin_account_from_token(token) if token else None
    ok, _ = admin_auth.require(account, "developer")
    return ok


def _format_file_result(title: str, data: dict) -> str:
    lines = [title]
    for key, value in data.items():
        if key == "text":
            continue
        lines.append("  %s: %s" % (key, value))
    if "text" in data:
        lines.extend(["", data["text"]])
    return "\n".join(lines)


@mcp.tool()
def file_policy(token: str = "", approval: str = "", extra_roots: str = "") -> str:
    """Show guarded filesystem roots and bypass state."""
    _maybe_live_reload()
    return file_ops.policy_text(
        bypass=_file_bypass_allowed(token, approval),
        extra_roots=extra_roots,
    )


@mcp.tool()
def file_find(
    query: str = "*",
    root: str = "",
    max_results: int = 50,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Find files under allowed roots. Use extra_roots or admin/dev bypass for broader search."""
    _maybe_live_reload()
    try:
        data = file_ops.find_files(
            query=query,
            root=root,
            max_results=max_results,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as e:
        return "ERROR: %s" % e
    lines = ["file find: %s under %s" % (data["query"], data["root"])]
    for row in data["results"]:
        lines.append("  %(type)s %(relative)s (%(bytes)s bytes)" % row)
    if not data["results"]:
        lines.append("  (no matches)")
    return "\n".join(lines)


@mcp.tool()
def file_read(path: str, max_bytes: int = 256000, token: str = "", approval: str = "", extra_roots: str = "") -> str:
    """Read a UTF-8-ish text file inside allowed roots."""
    _maybe_live_reload()
    try:
        data = file_ops.read_file(
            path,
            max_bytes=max_bytes,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as e:
        return "ERROR: %s" % e
    return _format_file_result("file read", data)


@mcp.tool()
def file_write(
    path: str,
    content: str,
    mode: str = "create",
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Create, overwrite, or append a text file inside allowed roots."""
    _maybe_live_reload()
    try:
        data = file_ops.write_file(
            path,
            content,
            mode=mode,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as e:
        return "ERROR: %s" % e
    return _format_file_result("file write", data)


@mcp.tool()
def file_edit(
    path: str,
    old: str,
    new: str,
    count: int = 1,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Replace text in a file inside allowed roots."""
    _maybe_live_reload()
    try:
        data = file_ops.edit_file(
            path,
            old,
            new,
            count=count,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as e:
        return "ERROR: %s" % e
    return _format_file_result("file edit", data)


@mcp.tool()
def file_delete(
    path: str,
    recursive: bool = False,
    dry_run: bool = True,
    confirm: str = "",
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Delete a file or directory. Dry-run by default; confirm must match returned string."""
    _maybe_live_reload()
    try:
        data = file_ops.delete_path(
            path,
            recursive=recursive,
            dry_run=dry_run,
            confirm=confirm,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as e:
        return "ERROR: %s" % e
    return _format_file_result("file delete", data)


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
            context_size=action.get("context_size", ""),
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
    if action_type in ("improvement_report", "system_improvement_report"):
        return _loop_text_result("improvement_report", system_improvement_report(
            session=action.get("session", ""),
            project=action.get("project", ""),
        ))
    if action_type in ("master_status", "agent_status"):
        return _loop_text_result("master_status", master_status(
            include_finished=action.get("include_finished", True),
            limit=action.get("limit", 20),
        ))
    if action_type in ("master", "master_orchestrate"):
        return _loop_text_result("master_orchestrate", master_orchestrate(
            task=action.get("task", action.get("prompt", "")),
            mode=action.get("mode", "ask"),
            agents=action.get("agents", 3),
            tier=action.get("tier", "code"),
            learn=action.get("learn", False),
        ))
    if action_type == "file_policy":
        return _loop_text_result("file_policy", file_policy(
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "file_find":
        return _loop_text_result("file_find", file_find(
            query=action.get("query", "*"),
            root=action.get("root", ""),
            max_results=action.get("max_results", 50),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "file_read":
        return _loop_text_result("file_read", file_read(
            path=action.get("path", ""),
            max_bytes=action.get("max_bytes", 256000),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "file_write":
        return _loop_text_result("file_write", file_write(
            path=action.get("path", ""),
            content=action.get("content", ""),
            mode=action.get("mode", "create"),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "file_edit":
        return _loop_text_result("file_edit", file_edit(
            path=action.get("path", ""),
            old=action.get("old", ""),
            new=action.get("new", ""),
            count=action.get("count", 1),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "file_delete":
        return _loop_text_result("file_delete", file_delete(
            path=action.get("path", ""),
            recursive=action.get("recursive", False),
            dry_run=action.get("dry_run", True),
            confirm=action.get("confirm", ""),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
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
    if action_type == "ground_artifact":
        return _loop_text_result("ground_artifact", ground_artifact(
            artifact=action.get("artifact", ""),
            checks_json=json.dumps(action.get("checks", [])),
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
        "output": "Valid action types: code, project, offload, trilobite, master_orchestrate, master_status, file_policy, file_find, file_read, file_write, file_edit, file_delete, status, diagnostics, context_health, memory_quality_report, memory_quality_repair, improvement_report, self_heal_check, self_heal_repair, profile_status, emotion_status, memory_search, ground_artifact, apply_learned, web_search, web_fetch, unload, sleep.",
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
      - {"type":"trilobite","prompt":"...","context_size":"1m"}
      - {"type":"master_orchestrate","task":"...","mode":"inline|delegate","agents":3}
      - {"type":"master_status"}
      - {"type":"file_find","query":"*.py","root":"."}
      - {"type":"file_read","path":"README.md"}
      - {"type":"file_write","path":"notes.txt","content":"...","mode":"create|overwrite|append"}
      - {"type":"file_edit","path":"notes.txt","old":"before","new":"after"}
      - {"type":"file_delete","path":"notes.txt","dry_run":true}
      - {"type":"web_search","query":"...","limit":5}
      - {"type":"web_fetch","url":"https://...","max_chars":8000}
      - {"type":"memory_search","query":"..."}
      - {"type":"improvement_report"}
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
        "master_orchestrate/master_status": "Run inline/delegated master orchestration and inspect live subagent activity.",
        "admin_register/admin_login/admin_accounts/admin_set_account": "Manage hosted accounts, roles, bans, tiers, and developer flags.",
        "admin_status/debug_inspect/admin_private_chain_of_thought": "Inspect admin/debug state and safely deny private chain-of-thought exposure.",
        "trilobite": "Ask the local self-improving coding model.",
        "offload": "Route a self-contained task to a configured local/cloud tier.",
        "web_search/web_fetch": "Search or fetch public web pages.",
        "file_policy/file_find/file_read/file_write/file_edit/file_delete": "Guarded filesystem find/read/create/edit/delete with approval bypass support.",
        "task_create/task_list/task_update/task_show": "Visible Claude-style todo/task state shared by console, app, agents, and MCP.",
        "command_registry_list": "Inspect available slash commands by category, name, or risk.",
        "permission_policy/permission_rule_set": "Inspect or guarded-edit local permission rules for tool actions.",
        "context_compaction_plan": "Preview when to summarize, split sessions, or reduce live context.",
        "run_code": "Run a bounded Python/JS/PowerShell/C++/C# snippet.",
        "ground_artifact": "Validate non-code artifacts with exact/contains/regex/JSON checks.",
        "run_project": "Run a bounded temporary multi-file project with optional build commands.",
        "loop": "Repeat bounded code/model/system actions.",
        "workflow_list/save/run/delete": "Manage reusable loop workflows.",
        "system_profile_text/update_system_profile": "Read or edit standing instructions.",
        "emotion_vector_status/update_emotion_vectors": "Read or edit tone vectors.",
        "memory_search/memory_export/session_export": "Inspect local memory.",
        "memory_quality_report/memory_quality_repair": "Audit and dry-run/prune exact duplicate lessons.",
        "system_improvement_report": "Suggest next improvements from learning, memory, context, and deployment signals.",
        "context_policy_status/set_context_size": "Show or select requested virtual context up to 1m while clamping Ollama native num_ctx.",
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
- file_policy: {}
- file_find: {"query": "*.py", "root": ".", "max_results": 50}
- file_read: {"path": "README.md"}
- file_write: {"path": "notes.txt", "content": "...", "mode": "create|overwrite|append"}
- file_edit: {"path": "notes.txt", "old": "before", "new": "after", "count": 1}
- file_delete: {"path": "notes.txt", "dry_run": true}
- task_create: {"title": "...", "detail": "...", "priority": 2, "project": "...", "owner": "..."}
- task_list: {"status": "pending|in_progress|blocked|done|canceled", "project": "", "include_done": false, "limit": 50}
- task_update: {"task_id": "...", "status": "in_progress|blocked|done", "note": "..."}
- task_show: {"task_id": "..."}
- command_registry_list: {"filter_text": "filesystem|dangerous|context"}
- permission_policy: {"tool_name": "file_delete"}
- context_compaction_plan: {"session": "", "project": ""}
- memory_search: {"query": "...", "limit": 10}
- ground_artifact: {"artifact": "...", "checks_json": [{"type": "contains", "text": "..."}]}
- apply_learned: {"task": "...", "limit": 5}
- workflow_run: {"name": "...", "max_iterations": 1}
- diagnostics: {}
- context_health: {}
- context_policy_status: {"context_size": "1m"}
- set_context_size: {"context_size": "256k"}
- memory_quality_report: {"sample_limit": 5}
- memory_quality_repair: {"apply": false}
- system_improvement_report: {}
- master_orchestrate: {"task": "...", "mode": "ask|inline|delegate", "agents": 3, "tier": "code"}
- master_status: {}
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
    if tool_name == "file_policy":
        return file_policy(
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "file_find":
        return file_find(
            query=args.get("query", "*"),
            root=args.get("root", ""),
            max_results=args.get("max_results", 50),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "file_read":
        return file_read(
            path=args.get("path", ""),
            max_bytes=args.get("max_bytes", 256000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "file_write":
        return file_write(
            path=args.get("path", ""),
            content=args.get("content", ""),
            mode=args.get("mode", "create"),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "file_edit":
        return file_edit(
            path=args.get("path", ""),
            old=args.get("old", ""),
            new=args.get("new", ""),
            count=args.get("count", 1),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "file_delete":
        return file_delete(
            path=args.get("path", ""),
            recursive=args.get("recursive", False),
            dry_run=args.get("dry_run", True),
            confirm=args.get("confirm", ""),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "ground_artifact":
        return ground_artifact(
            args.get("artifact", ""),
            json.dumps(args.get("checks_json", args.get("checks", []))),
        )
    if tool_name == "task_create":
        return task_create(
            title=args.get("title", ""),
            detail=args.get("detail", ""),
            priority=args.get("priority", 2),
            project=args.get("project", ""),
            owner=args.get("owner", ""),
        )
    if tool_name == "task_list":
        return task_list(
            status=args.get("status", ""),
            project=args.get("project", ""),
            owner=args.get("owner", ""),
            include_done=args.get("include_done", False),
            limit=args.get("limit", 50),
        )
    if tool_name == "task_update":
        return task_update(
            task_id=args.get("task_id", args.get("id", "")),
            status=args.get("status", ""),
            title=args.get("title", ""),
            detail=args.get("detail", ""),
            priority=args.get("priority", ""),
            project=args.get("project", ""),
            owner=args.get("owner", ""),
            note=args.get("note", ""),
        )
    if tool_name == "task_show":
        return task_show(args.get("task_id", args.get("id", "")))
    if tool_name == "command_registry_list":
        return command_registry_list(args.get("filter_text", args.get("filter", "")))
    if tool_name == "permission_policy":
        return permission_policy(args.get("tool_name", args.get("tool", "")))
    if tool_name == "context_compaction_plan":
        return context_compaction_plan(
            session=args.get("session", ""),
            project=args.get("project", ""),
        )
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
    if tool_name == "context_policy_status":
        return context_policy_status(args.get("context_size", ""))
    if tool_name == "set_context_size":
        return set_context_size(args.get("context_size", ""))
    if tool_name == "memory_quality_report":
        return memory_quality_report(sample_limit=args.get("sample_limit", 5))
    if tool_name == "memory_quality_repair":
        return memory_quality_repair(apply=args.get("apply", False))
    if tool_name in ("system_improvement_report", "improvement_report"):
        return system_improvement_report(
            session=args.get("session", ""),
            project=args.get("project", ""),
        )
    if tool_name in ("master_status", "agent_status"):
        return master_status(
            include_finished=args.get("include_finished", True),
            limit=args.get("limit", 20),
        )
    if tool_name in ("master_orchestrate", "master"):
        return master_orchestrate(
            task=args.get("task", args.get("prompt", "")),
            mode=args.get("mode", "ask"),
            agents=args.get("agents", 3),
            tier=args.get("tier", "code"),
            learn=args.get("learn", False),
        )
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
    if tier_label == "cloud-disabled":
        return _cloud_disabled_message()
    if tier_label is None:
        return "ERROR: unknown tier '%s'. Valid: trilobite, %s." % (tier, _valid_tier_names())
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
        for k, v in available_tiers(include_disabled=cloud_allowed()).items()
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
        return f"ERROR: unknown tier '{tier}'. Valid: all, {_valid_tier_names()}."
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
