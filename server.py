"""
sonder-runtime MCP server
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

import contextlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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
import preference_learning
import workflow_store
import web_tools
import web_intents
import self_heal
import grounding
import sonder_paths
import memory_quality
import learning_health
import domain_grounding
import master_orchestrator
import admin_auth
import file_ops
import context_policy
import command_registry
import adaptive_training
import selfmod
import permission_rules
import debug_dump
import activity_tracker
import assetgen
import artifact_grounding
import game_forge
import workbench
import creative_router
import intents
import runtime_policy
import reloadable_mcp
import autopilot_store
import autopilot_controller

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
KEEP_ALIVE = os.environ.get("SONDER_KEEP_ALIVE", "2m")
TIMEOUT = int(os.environ.get("SONDER_TIMEOUT", "300"))
LOCAL_CODE_MODEL = os.environ.get("SONDER_CODE_LOCAL", "qwen2.5-coder:7b")


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
        "num_thread": _env_int_option("SONDER_NUM_THREAD", _cpu_thread_default()),
        "num_gpu": _env_int_option("SONDER_NUM_GPU", 999),
        "num_batch": _env_int_option("SONDER_NUM_BATCH", 512),
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
    "fast": os.environ.get("SONDER_FAST", "qwen2.5:3b"),
    "code": os.environ.get("SONDER_CODE", "qwen2.5-coder:7b"),
    "general": os.environ.get("SONDER_GENERAL", "qwen2.5:7b-instruct"),
    "cloud-code": os.environ.get("SONDER_CLOUD_CODE", "qwen3-coder:480b-cloud"),
    "cloud-general": os.environ.get("SONDER_CLOUD_GENERAL", "gpt-oss:120b-cloud"),
}
# Tiers whose ":...-cloud" model runs on Ollama's servers (data leaves the machine).
CLOUD_TIERS = {"cloud-code", "cloud-general"}
LOCAL_TIERS = tuple(k for k in TIERS if k not in CLOUD_TIERS)


def cloud_allowed():
    return os.environ.get("SONDER_ALLOW_CLOUD", "").strip().lower() in (
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
        "ERROR: hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 "
        "to opt in; prompts sent to cloud tiers leave this machine."
    )


def _is_cloud_model_name(model):
    name = (model or "").lower()
    return "-cloud" in name or name.endswith(":cloud")


if _is_cloud_model_name(TIERS["code"]):
    TIERS["code"] = LOCAL_CODE_MODEL


_RUNTIME_POLICY = {}


def _refresh_runtime_policy(create=True):
    """Apply the shared local-only policy without touching cloud configuration."""
    global LOCAL_CODE_MODEL, _RUNTIME_POLICY
    policy = runtime_policy.load(create=create)
    for tier in runtime_policy.LOCAL_TIERS:
        TIERS[tier] = policy["local_models"][tier]
    LOCAL_CODE_MODEL = policy["local_models"]["code"]
    _RUNTIME_POLICY = policy
    return policy


_refresh_runtime_policy(create=True)


def _runtime_lane_tier(lane: str, requested: str = "") -> str:
    """Resolve an explicit local tier or the shared default for one lane."""
    requested = str(requested or "").strip().lower()
    if requested and requested not in {"auto", "default", "policy"}:
        return requested
    return runtime_policy.route_tier(
        lane,
        _RUNTIME_POLICY or _refresh_runtime_policy(create=True),
        fallback="code",
    )


def _is_cloud_tier(tier, model=None):
    if tier in CLOUD_TIERS:
        return True
    if model is None:
        model = TIERS.get(tier, "")
    return _is_cloud_model_name(model)

# Which offload tiers feed the learning loop (capture + distill lessons). A stronger
# paid/cloud model can provide grounded good outcomes that become lessons and
# fine-tuning data for later local retrieval. All configured tiers
# learn by default; override machine-wide with e.g. SONDER_LEARN_TIERS="code"
# (local coder only) or "fast,code,general" (local-only all sizes).
DEFAULT_LEARN_TIERS = ",".join(LOCAL_TIERS)
LEARN_TIERS = {
    t.strip()
    for t in os.environ.get(
        "SONDER_LEARN_TIERS", DEFAULT_LEARN_TIERS
    ).split(",")
    if t.strip()
}

# strict=True pins the local runtime route to the `sonder:latest` Ollama alias
# (errors if missing) instead of silently falling back to the base coder model.
# The environment default lets operators change this without touching call sites.
_STRICT_DEFAULT = os.environ.get("SONDER_STRICT", "").strip().lower() in ("1", "true", "yes", "on")

# Conversation memory is ON by default: a call with no explicit session threads the
# shared DEFAULT_SESSION so follow-ups are remembered. Pass session="none" to opt out
# (single-turn), or a distinct id to isolate a thread. Same idea for project facts.
DEFAULT_SESSION = os.environ.get("SONDER_DEFAULT_SESSION", "default")
DEFAULT_PROJECT = os.environ.get("SONDER_DEFAULT_PROJECT", "default")
# Sessioned calls get a roomier context (fits easily on the 6 GB 4050) and keep the
# last MAX_TURNS turns live; older turns are rolled into a summary.
SESSION_NUM_CTX = context_policy.default_requested()
MAX_TURNS = int(os.environ.get("SONDER_MAX_TURNS", "12"))

_DB_PATH = sonder_paths.memory_db_path()

FOOTER_PREFIX = "\n\n[interaction_id: "
_FOOTER_RE = re.compile(r"\[interaction_id: ([0-9a-f]+)\]\s*$")
_CAMPAIGN_LEARN_LOCK = threading.Lock()
_AUTOPILOT_THREADS_LOCK = threading.RLock()
_AUTOPILOT_THREADS = {}

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
    "preference_learning",
    "workflow_store",
    "web_tools",
    "web_intents",
    "self_heal",
    "memory_quality",
    "learning_health",
    "domain_grounding",
    "master_orchestrator",
    "admin_auth",
    "file_ops",
    "context_policy",
    "command_registry",
    "permission_rules",
    "debug_dump",
    "activity_tracker",
    "media_assets",
    "model_assets",
    "ooxml_assets",
    "assetgen",
    "artifact_grounding",
    "game_forge",
    "workbench",
    "creative_router",
    "intents",
    "runtime_policy",
    # The controller is stateless and safe to refresh between callback calls.
    # autopilot_store intentionally stays loaded because it exclusively owns a
    # process-safe SQLite schema and may be serving background worker threads.
    "autopilot_controller",
]


def _maybe_live_reload():
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    for name, module in modules.items():
        if name in globals():
            globals()[name] = module
    _refresh_runtime_policy(create=True)


def _open_db():
    return memory_store.connect(_DB_PATH, check_same_thread=True)


def with_footer(text, interaction_id):
    current = activity_tracker.current()
    activity = activity_tracker.format_response(current) if current else ""
    if activity and not activity.startswith("activity:") and "=== ACTIVITY (observable work) ===" not in (text or ""):
        text = "%s\n\n%s" % (text, activity)
    return "%s%s%s]" % (text, FOOTER_PREFIX, interaction_id)


def _strip_activity_block(text):
    """Remove the final observable-activity block while preserving other text."""
    value = str(text or "")
    marker = "=== ACTIVITY (observable work) ==="
    end_marker = "=== END ACTIVITY ==="
    start = value.rfind(marker)
    if start < 0:
        return value
    end = value.find(end_marker, start)
    if end < 0:
        return value
    end += len(end_marker)
    before = value[:start].rstrip()
    after = value[end:].lstrip()
    return "\n\n".join(part for part in (before, after) if part)


def _append_activity(text, response=None, replace=False):
    current = response if response is not None else activity_tracker.current()
    if replace:
        text = _strip_activity_block(text)
    activity = activity_tracker.format_response(current) if current else ""
    if activity and not activity.startswith("activity:") and "=== ACTIVITY (observable work) ===" not in (text or ""):
        footer = _FOOTER_RE.search(text or "")
        if footer:
            before = (text or "")[:footer.start()].rstrip()
            return "%s\n\n%s\n\n%s" % (
                before, activity, (text or "")[footer.start():],
            )
        return "%s\n\n%s" % (text, activity)
    return text


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
        "=== TRACE (how Sonder Runtime decided) ===",
        "model: %s   tier: %s" % (model, tier),
        "generation params: %r" % (params,),
        "lessons retrieved: %d" % len(lessons),
    ]
    for lesson_text in lessons:
        lines.append("   - %s" % lesson_text)
    lines.append("--- exact prompt sent to the model ---")
    lines.append(trace.get("augmented_prompt", ""))
    lines.append("=== END TRACE ===")
    return "\n".join(lines)


def _should_learn(tier, learn):
    # A tier feeds the learning loop when it is in LEARN_TIERS (env-configurable) and
    # the caller didn't opt out with learn=False. Defaults: local 'code' plus the
    # cloud tiers (teacher distillation); 'fast'/'general' stay mechanical.
    return bool(learn) and tier in LEARN_TIERS


def resolve_sonder_model(strict=False):
    try:
        tags = [m.get("name", "") for m in _get("/api/tags").get("models", [])]
    except Exception:
        tags = []
    if any(t.split(":")[0] == "sonder" for t in tags):
        return "sonder"
    return None if strict else TIERS["code"]


def _make_generate(
    model, system, temperature, num_predict, num_ctx, cloud=False, timeout=None
):
    """Build a generate(prompt, history) closure for `model`.

    cloud=True targets an Ollama-hosted model: keep_alive and num_ctx are omitted
    (they're VRAM/local-context knobs the remote tier doesn't take), matching how the
    non-learning cloud path posts.
    """
    def gen(prompt, history=None):
        gen.last_usage = {}
        started = time.time()
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
        ok = False
        content = ""
        try:
            if timeout is None:
                out = _post("/api/chat", payload)
            else:
                out = _post("/api/chat", payload, timeout=timeout)
            content = out.get("message", {}).get("content", "")
            tokens_in = out.get("prompt_eval_count")
            tokens_out = out.get("eval_count")
            source = "ollama" if tokens_in is not None or tokens_out is not None else "estimated"
            if tokens_in is None:
                tokens_in = sum(_rough_token_count(m.get("content", "")) for m in messages)
            if tokens_out is None:
                tokens_out = _rough_token_count(content)
            gen.last_usage = {
                "tokens_in": int(tokens_in or 0),
                "tokens_out": int(tokens_out or 0),
                "token_source": source,
            }
            ok = True
        finally:
            usage = getattr(gen, "last_usage", {}) or {}
            activity_tracker.record_model_call(
                model=model,
                prompt_chars=len(prompt or ""),
                history_messages=len(history or []),
                tokens_in=usage.get("tokens_in", 0),
                tokens_out=usage.get("tokens_out", 0),
                token_source=usage.get("token_source", ""),
                ok=ok,
                elapsed_ms=int((time.time() - started) * 1000),
            )
        return content
    gen.last_usage = {}
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
    """Shared prep for the Sonder Runtime tool and HTTP serve layer.

    Returns (model, effective_system); model is None if the strict alias is missing.
    """
    strict_eff = _STRICT_DEFAULT if strict is None else strict
    model = resolve_sonder_model(strict_eff)
    if model is None:
        return None, None
    return model, _build_system(system, trace, persona)


def _serve_target(tier, strict):
    """Resolve a serve/app request's OpenAI `model` field to a concrete target.

    Returns (model, cloud, augment, tier_label):
      - model:      the Ollama model to generate with (None if a strict alias is
                    missing, or tier_label is None for an unknown name)
      - cloud:      True if it runs on Ollama's servers (payload omits VRAM knobs)
      - augment:    inject facts/lessons/recall? Only the local learning route
                    ('code'/"sonder") does; other model routes answer clean
      - tier_label: what to record on the interaction (None => unknown model)

    Default / "" / "sonder" / "local" => Sonder Runtime's local learning route.
    Any TIERS key (e.g. "cloud-code", "general") selects that model directly, so a
    single server can drive many models — pick per request.
    """
    t = (tier or "").strip().lower()
    if t in ("", "sonder", "local"):
        strict_eff = _STRICT_DEFAULT if strict is None else strict
        return resolve_sonder_model(strict_eff), False, True, "sonder"
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
    request_message_count = len(messages)
    session_id = _resolve_session(session) if (session or "").strip() else None
    project_id = _resolve_project(project)
    persisted_turns = 0
    if session_id:
        conn = _open_db()
        try:
            for turn in memory_store.session_turns(conn, session_id):
                persisted_turns += 1
                messages.append({"role": "user", "content": turn.get("task") or ""})
                messages.append({"role": "assistant", "content": turn.get("response") or ""})
        finally:
            conn.close()
    sections = [
        (
            "dump sources",
            (
                "request/history messages: %d\n"
                "persisted session: %s\n"
                "persisted session turns appended: %d\n"
                "note: large dumps usually mean saved memory.db history was included, "
                "not necessarily that the server process stayed alive."
            ) % (
                request_message_count,
                session_id or "(none)",
                persisted_turns,
            ),
        ),
        ("session", session_id or "(none)"),
        ("project", project_id or "(none)"),
        ("context", context_health(session=session_id or "none", project=project_id or "none")),
        ("quality", memory_quality_report(sample_limit=5)),
        ("agents", master_status(limit=20)),
        ("diagnostics", diagnostics()),
    ]
    path = debug_dump.write_dump(
        sonder_paths.default_home(),
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


def _parse_game_campaign_command(arg: str) -> dict | None:
    parts = [part.strip() for part in str(arg or "").split("|", 3)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    kwargs = {"name": parts[0], "concept": parts[1]}
    if len(parts) > 2 and parts[2]:
        kwargs["language"] = parts[2]
    if len(parts) > 3 and parts[3]:
        kwargs["dimension"] = parts[3]
    return kwargs


def _autopilot_command(arg: str, project: str = "") -> str:
    text = str(arg or "").strip()
    if not text:
        return autopilot_status()
    action, _, rest = text.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in ("status", "show", "list"):
        return autopilot_status(rest)
    if action in ("run", "start", "plan"):
        policy = "workspace"
        allow_web = True
        adaptive = True
        while rest.startswith("--"):
            option, _, remaining = rest.partition(" ")
            if option == "--observe":
                policy = "observe"
            elif option == "--no-web":
                allow_web = False
            elif option == "--static":
                adaptive = False
            else:
                return "ERROR: unknown autopilot option '%s'." % option
            rest = remaining.strip()
        if not rest:
            return (
                "usage: /autopilot %s [--observe] [--no-web] [--static] <objective>"
                % action
            )
        return autopilot_start(
            objective=rest,
            project=_resolve_project(project) or "",
            policy=policy,
            allow_web=allow_web,
            adaptive=adaptive,
            plan_only=action == "plan",
        )
    if action == "resume":
        return autopilot_resume(rest) if rest else "usage: /autopilot resume <run-id>"
    if action == "pause":
        return autopilot_pause(rest) if rest else "usage: /autopilot pause <run-id>"
    if action == "cancel":
        return autopilot_cancel(rest) if rest else "usage: /autopilot cancel <run-id>"
    if action in ("help", "?"):
        return (
            "autopilot commands:\n"
            "  /autopilot status [id]\n"
            "  /autopilot plan [--observe] [--no-web] [--static] <objective>\n"
            "  /autopilot run [--observe] [--no-web] [--static] <objective>\n"
            "  /autopilot resume|pause|cancel <id>"
        )
    return "ERROR: unknown autopilot action '%s'; try /autopilot help." % action


def _runtime_command(arg: str) -> str:
    text = str(arg or "").strip()
    if not text or text.lower() in {"status", "show", "list"}:
        return runtime_policy_status()
    action, _, rest = text.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action == "reset":
        return runtime_policy_update(reset=True)
    if action == "set":
        local_models = {}
        routing = {}
        for item in rest.split():
            if "=" not in item:
                return "ERROR: runtime assignment must use key=value: %s" % item
            key, value = item.split("=", 1)
            key, value = key.strip().lower(), value.strip()
            if key in runtime_policy.LOCAL_TIERS:
                local_models[key] = value
            elif key in runtime_policy.ROUTING_LANES:
                routing[key] = value
            else:
                return "ERROR: unknown runtime policy key '%s'." % key
        if not local_models and not routing:
            return (
                "usage: /runtime set code=<local-model> workbench=<fast|code|general>"
            )
        return runtime_policy_update(
            local_models_json=json.dumps(local_models),
            routing_json=json.dumps(routing),
        )
    if action in {"help", "?"}:
        return (
            "runtime policy commands:\n"
            "  /runtime status\n"
            "  /runtime set fast=<model> code=<model> general=<model>\n"
            "  /runtime set router=<tier> workbench=<tier> autopilot=<tier> "
            "fleet=<tier> review=<tier>\n"
            "  /runtime reset\n"
            "Only installed local models and fast/code/general route tiers are accepted."
        )
    return "ERROR: unknown runtime action '%s'; try /runtime help." % action


def _mcp_command(arg: str) -> str:
    action = str(arg or "status").strip().lower() or "status"
    if action in {"status", "show", "audit", "list"}:
        return format_mcp_runtime()
    if action == "refresh":
        refreshed = mcp.refresh_if_changed()
        prefix = (
            "MCP source refreshed."
            if refreshed.get("reloaded")
            else "MCP source already current."
        )
        if refreshed.get("error"):
            prefix = "MCP refresh failed closed: %s" % refreshed["error"]
        return "%s\n\n%s" % (prefix, format_mcp_runtime())
    if action in {"help", "?"}:
        return (
            "MCP runtime commands:\n"
            "  /mcp status\n"
            "  /mcp refresh\n"
            "Updated implementations and tool schemas publish atomically; a bad edit "
            "keeps the last known-good registry."
        )
    return "ERROR: unknown MCP action '%s'; try /mcp help." % action


def _training_command(arg: str) -> str:
    text = str(arg or "").strip()
    if not text or text.lower() in {"plan", "status", "hardware"}:
        return adaptive_training.command_text(text or "plan")
    if text.lower() in {"help", "?"}:
        return (
            "training commands:\n"
            "  /hardware\n"
            "  /training plan [--dry-run] [--model auto|1.5b|3b|7b]\n"
            "  /training start --confirm [planning options]\n"
            "  /training status\n"
            "  /training deploy [--adapter-dir PATH] [--llama-cpp PATH]\n"
            "  /training rollback\n"
            "Options: --allow-cpu-offload --max-vram N --max-system-ram N "
            "--context-length N --sequence-length N --batch-size N."
        )
    return adaptive_training.command_text(text)


def _selfmod_test_commands(run, explicit_tests):
    import shlex
    workspace = Path(run["workspace_path"])
    python_files = [path for path in run["files"] if path.endswith(".py") and (workspace / path).is_file()]
    syntax = [sys.executable, "-m", "py_compile", *python_files] if python_files else [sys.executable, "-c", "print('no Python syntax targets')"]
    targeted = shlex.split(explicit_tests[0], posix=os.name != "nt") if explicit_tests else [sys.executable, "-c", "raise SystemExit('explicit reproducing/targeted test required')"]
    regression = [sys.executable, "-m", "pytest", "-q"]
    smoke = [sys.executable, "-c", "import pathlib; assert pathlib.Path('.').is_dir(); print('selfmod smoke ok')"]
    commands = [("syntax", syntax), ("targeted", targeted), ("regression", regression), ("smoke", smoke)]
    if run["maintenance_authorized"]:
        security = shlex.split(explicit_tests[1], posix=os.name != "nt") if len(explicit_tests) > 1 else [sys.executable, "-c", "raise SystemExit('explicit protected security test required')"]
        commands.append(("security", security))
    return commands


def _selfmod_agent_policy(run):
    workspace = Path(run["workspace_path"]).resolve()
    allowed = {(workspace / path).resolve(strict=False) for path in run["files"]}
    mutation_tools = {"file_write", "file_edit", "file_delete"}
    path_tools = mutation_tools | {"file_read", "file_read_range", "file_find", "text_search", "directory_tree", "workspace_inventory", "script_search"}
    path_keys = {
        "file_write": "path", "file_edit": "path", "file_delete": "path",
        "file_read": "path", "file_read_range": "path",
        "directory_tree": "path", "workspace_inventory": "path",
        "file_find": "root", "text_search": "root", "script_search": "root",
    }
    inspected = set()
    counters = {"tools": 0}
    started = time.monotonic()

    def policy(tool_name, args):
        if not isinstance(args, dict):
            return "ERROR: SELFMOD POLICY: tool arguments must be an object."
        counters["tools"] += 1
        if counters["tools"] > run["budgets"]["max_tool_calls"]:
            return "ERROR: SELFMOD POLICY: tool-call budget exhausted."
        if time.monotonic() - started > run["budgets"]["max_runtime_seconds"]:
            return "ERROR: SELFMOD POLICY: total runtime budget exhausted."
        if tool_name == "workspace_run":
            cwd = Path(str(args.get("cwd") or workspace)).expanduser().resolve(strict=False)
            if cwd != workspace and workspace not in cwd.parents:
                return "ERROR: SELFMOD POLICY: commands must run inside the candidate workspace."
            command_text = " ".join(str(item) for item in (args.get("args_json") or args.get("args") or []))
            if "selfmod" in command_text.lower():
                return "ERROR: SELFMOD POLICY: recursive self-improvement is forbidden."
            return ""
        if tool_name not in path_tools:
            return ""
        if any(args.get(name) for name in ("token", "approval", "extra_roots")):
            return "ERROR: SELFMOD POLICY: filesystem authority cannot be expanded by the candidate."
        raw = args.get("path") or args.get("root") or args.get("cwd") or ""
        target = Path(str(raw)).expanduser()
        if not target.is_absolute():
            target = workspace / target
        target = target.resolve(strict=False)
        if target != workspace and workspace not in target.parents:
            return "ERROR: SELFMOD POLICY: path is outside the isolated candidate workspace."
        if tool_name in mutation_tools and target not in allowed:
            return "ERROR: SELFMOD POLICY: mutation is outside the pre-backed-up file scope."
        if tool_name not in mutation_tools:
            inspected.add(str(target))
            if len(inspected) > run["budgets"]["max_files_inspected"]:
                return "ERROR: SELFMOD POLICY: file-inspection budget exhausted."
        # Generic file tools resolve relative paths against the live checkout.
        # Pin the checked path to the candidate before dispatch so the model
        # cannot accidentally (or deliberately) mutate the live repository.
        key = path_keys.get(tool_name)
        if key:
            args[key] = str(target)
        args.pop("token", None)
        args.pop("approval", None)
        args.pop("extra_roots", None)
        return ""
    return policy


def _execute_selfmod_run(run_id, explicit_tests=None):
    run = selfmod.get_run(run_id)
    if run["phase"] == "proposed":
        selfmod.create_backup(run_id)
        run = selfmod.prepare_workspace(run_id)
    elif run["phase"] == "backed_up":
        run = selfmod.prepare_workspace(run_id)
    if run["phase"] != "editing":
        return "ERROR: selfmod run is not ready for editing: %s" % run["phase"]
    owner = selfmod.claim(run_id)
    heartbeat_stop = threading.Event()
    def heartbeat_worker():
        while not heartbeat_stop.wait(30):
            if not selfmod.heartbeat(run_id, owner):
                return
    heartbeat_thread = threading.Thread(
        target=heartbeat_worker, name="sonder-selfmod-heartbeat", daemon=True,
    )
    heartbeat_thread.start()
    previous = os.environ.get("SONDER_SELFMOD_ACTIVE")
    os.environ["SONDER_SELFMOD_ACTIVE"] = "1"
    try:
        workspace = run["workspace_path"]
        test_commands = _selfmod_test_commands(run, explicit_tests or [])
        selfmod.record_reproducer_before(run_id, test_commands[1][1])
        prompt = (
            "Implement this bounded self-improvement only inside the isolated workspace.\n"
            "Objective: %s\nEvidence: %s\nAcceptance criteria: %s\n"
            "Authorized files (no others may change): %s\nWorkspace: %s\n"
            "Inspect first, then use guarded file tools. Do not approve, deploy, alter tests outside scope, "
            "change permissions, install dependencies, invoke selfmod, or touch the live repository."
            % (run["objective"], "; ".join(run["evidence"]), "; ".join(run["criteria"]), ", ".join(run["files"]), workspace)
        )
        output = _agent_impl(
            prompt, tier="code", max_steps=min(run["budgets"]["max_tool_calls"], run["budgets"]["max_model_calls"], 20),
            allow_web=False, require_file_evidence=True, read_only=False,
            include_evidence=True, auto_checklist=True,
            tool_allowlist={"workspace_inventory", "directory_tree", "text_search", "file_read", "file_read_range", "file_write", "file_edit", "file_delete"},
            tool_policy=_selfmod_agent_policy(run),
        )
        diff = selfmod.inspect_diff(run_id)
        if not diff["changed_files"]:
            selfmod.reject(run_id, "editing agent produced no scoped diff")
            return "Selfmod rejected: editing agent produced no scoped diff.\n\n" + output
        selfmod.begin_testing(run_id)
        for kind, command in test_commands:
            selfmod.record_test(run_id, kind, command)
        selfmod.review(run_id)
        return selfmod.format_run(run_id) + "\n\nAgent evidence:\n" + output
    except Exception as exc:
        current = selfmod.get_run(run_id)
        if current["phase"] in {"editing", "testing", "reviewing"}:
            with contextlib.suppress(Exception):
                selfmod.reject(run_id, "selfmod execution failed: %s" % exc)
        return "ERROR: selfmod run failed closed: %s" % exc
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        if previous is None:
            os.environ.pop("SONDER_SELFMOD_ACTIVE", None)
        else:
            os.environ["SONDER_SELFMOD_ACTIVE"] = previous
        with contextlib.suppress(Exception):
            selfmod.release(run_id, owner)


def _selfmod_command(arg: str, *, repository_root="") -> str:
    text = str(arg or "status").strip() or "status"
    action, _, rest = text.partition(" ")
    action = action.lower()
    rest = rest.strip()
    root = Path(repository_root or Path(__file__).resolve().parent).resolve()
    try:
        if action in {"status", "show", "list"}:
            return selfmod.format_status()
        if action == "opportunities":
            return "Concrete host evidence for proposals:\n\n" + system_improvement_report()
        if action == "history":
            return selfmod.format_status()
        if action == "inspect":
            return selfmod.format_run(rest)
        if action == "diff":
            return selfmod.diff_text(rest) or "(no candidate diff)"
        if action == "tests":
            return json.dumps(selfmod.test_results(rest), indent=2, ensure_ascii=False)
        if action == "backups":
            rows = [run for run in selfmod.list_runs(100) if run.get("backup_manifest")]
            return "\n".join("%s %s %s" % (run["id"], run["phase"], run["backup_manifest"]) for run in rows) or "(no backups)"
        if action == "verify-backup":
            manifest = selfmod.verify_backup(rest)
            return "Backup verified: %s (%d file records)" % (rest, len(manifest["files"]))
        if action == "mode":
            if not rest:
                return "selfmod mode: %s" % selfmod.settings()["mode"]
            return "selfmod mode: %s" % selfmod.set_mode(rest)["mode"]
        if action in {"disable", "enable"}:
            return "selfmod enabled: %s" % selfmod.set_enabled(action == "enable")["enabled"]
        if action == "retention":
            values = rest.split()
            if len(values) != 2:
                return "usage: /selfmod retention <days> <max-gb>"
            configured = selfmod.set_retention(int(values[0]), int(float(values[1]) * 1024**3))
            return "selfmod retention: %d days, %.2f GB" % (configured["retention_days"], configured["retention_bytes"] / 1024**3)
        if action == "prune-backups":
            removed = selfmod.prune_backups()
            return "pruned backups: %s" % (", ".join(removed) or "none")
        if action in {"plan", "run"}:
            selfmod.recursive_guard()
            maintenance = "--maintenance" in rest.split()
            parsed_rest = " ".join(part for part in rest.split() if part != "--maintenance")
            objective, files, tests = selfmod.parse_plan_text(parsed_rest)
            if not files:
                return "usage: /selfmod %s <objective> --files path.py,test_path.py [--tests python -m pytest ...]" % action
            evidence = ["explicit user-authorized objective: %s" % objective, "host improvement report: %s" % system_improvement_report()[:2000]]
            run = selfmod.create_plan(
                objective, root, problem=objective, evidence=evidence, files=files,
                criteria=["explicit reproducing/targeted check passes", "syntax and regression checks do not regress", "diff remains inside declared file scope"],
                expected_benefit="resolve the explicit grounded defect", rollback_plan="restore immutable per-user backup",
                maintenance_authorized=maintenance,
            )
            if action == "plan":
                return selfmod.format_run(run["id"])
            return _execute_selfmod_run(run["id"], tests)
        if action == "resume":
            run = selfmod.resume(rest)
            return selfmod.format_run(run["id"])
        if action == "cancel":
            return selfmod.format_run(selfmod.cancel(rest)["id"])
        if action == "approve":
            return selfmod.format_run(selfmod.approve(rest, approver="explicit local/developer user")["id"])
        if action == "reject":
            run_id, _, reason = rest.partition(" ")
            return selfmod.format_run(selfmod.reject(run_id, reason or "explicit user rejection")["id"])
        if action == "deploy":
            run = selfmod.deploy(rest, health_command=[sys.executable, "-c", "import server; print(server.status())"])
            module_names = {
                Path(path).stem for path in run["files"]
                if path.endswith(".py") and "/" not in path
            }
            reloadable = module_names & set(LIVE_RELOAD_MODULES)
            if reloadable:
                _maybe_live_reload()
                failures = [
                    row for row in live_reload.snapshot(sorted(reloadable))
                    if row.get("error")
                ]
                if failures:
                    selfmod.rollback(rest, reason="in-process live reload health failed")
                    return "ERROR: live reload failed; automatic rollback completed: %s" % failures
            return selfmod.format_run(run["id"])
        if action == "rollback":
            return selfmod.format_run(selfmod.rollback(rest)["id"])
        if action in {"help", "?"}:
            return (
                "selfmod: status|opportunities|history|inspect <id>|plan <objective> --files a,b|"
                "run <objective> --files a,b --tests <command>|diff <id>|tests <id>|approve <id>|"
                "reject <id>|deploy <id>|rollback <id>|backups|verify-backup <id>|"
                "mode observe|propose|auto-low-risk|resume <id>|cancel <id>|retention <days> <GB>|prune-backups|disable|enable"
            )
        return "ERROR: unknown selfmod action; try /selfmod help"
    except (KeyError, ValueError, RuntimeError, PermissionError, OSError) as exc:
        return "ERROR: %s" % exc


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
        return sonder_stats()
    if cmd == "/context":
        return context_health()
    if cmd in ("/contextsize", "/ctxsize"):
        return set_context_size(arg.strip()) if arg.strip() else context_policy_status()
    if cmd in ("/compact", "/compaction"):
        return context_compaction_plan()
    if cmd in ("/commands", "/cmds"):
        return command_registry_list(arg.strip())
    if cmd in ("/activity", "/tools"):
        return activity_status()
    if cmd in ("/autopilot", "/auto"):
        return _autopilot_command(arg, project=project)
    if cmd in ("/runtime", "/models"):
        return _runtime_command(arg)
    if cmd in ("/hardware",):
        return _training_command("hardware")
    if cmd in ("/training", "/weighttraining"):
        return _training_command(arg)
    if cmd in ("/selfmod", "/selfmodify"):
        return _selfmod_command(arg)
    if cmd in ("/mcp", "/convergence"):
        return _mcp_command(arg)
    if cmd in ("/learning", "/learnhealth", "/metrics"):
        return learning_health_status()
    if cmd in ("/work", "/agent"):
        if not arg.strip():
            return "usage: /work <task>"
        return workbench_agent(
            prompt=arg.strip(), project=_resolve_project(project), max_steps=12,
        )
    if cmd in ("/report", "/endreport"):
        latest = activity_tracker.latest()
        return "%s\n\n%s" % (
            activity_tracker.format_end_report(latest),
            activity_tracker.format_transcript(latest),
        )
    if cmd in ("/inventory", "/workspace"):
        return workspace_inventory(path=arg.strip() or ".")
    if cmd in ("/tree", "/folders"):
        return directory_tree(path=arg.strip() or ".")
    if cmd in ("/search", "/grep"):
        search_parts = [part.strip() for part in arg.split("|", 2)]
        if not search_parts or not search_parts[0]:
            return "usage: /search <text> | <root> | <glob>"
        return text_search(
            query=search_parts[0],
            root=search_parts[1] if len(search_parts) > 1 and search_parts[1] else ".",
            glob=search_parts[2] if len(search_parts) > 2 and search_parts[2] else "*",
        )
    if cmd in ("/programs", "/programfind"):
        return program_search(query=arg.strip() or "*")
    if cmd in ("/scripts", "/scriptfind"):
        script_parts = [part.strip() for part in arg.split("|", 1)]
        return script_search(
            query=script_parts[0] or "*",
            root=script_parts[1] if len(script_parts) > 1 and script_parts[1] else ".",
        )
    if cmd in ("/image", "/inspectimage"):
        return image_inspect(path=arg.strip()) if arg.strip() else "usage: /image <path>"
    if cmd == "/mkdir":
        return directory_create(path=arg.strip()) if arg.strip() else "usage: /mkdir <path>"
    if cmd == "/runprogram":
        run_parts = [part.strip() for part in arg.split("|", 2)]
        if not run_parts or not run_parts[0]:
            return "usage: /runprogram <program> | <args-json> | <cwd>"
        return workspace_run(
            program=run_parts[0],
            args_json=run_parts[1] if len(run_parts) > 1 and run_parts[1] else "[]",
            cwd=run_parts[2] if len(run_parts) > 2 and run_parts[2] else ".",
        )
    if cmd == "/runscript":
        run_parts = [part.strip() for part in arg.split("|", 2)]
        if not run_parts or not run_parts[0]:
            return "usage: /runscript <path> | <args-json> | <cwd>"
        return script_run(
            path=run_parts[0],
            args_json=run_parts[1] if len(run_parts) > 1 and run_parts[1] else "[]",
            cwd=run_parts[2] if len(run_parts) > 2 else "",
        )
    if cmd in ("/checklist", "/plan"):
        checklist_id = arg.strip()
        if checklist_id:
            return checklist_show(checklist_id)
        current = activity_tracker.current() or activity_tracker.latest() or {}
        checklist = current.get("checklist") or {}
        return checklist_show(checklist["id"]) if checklist.get("id") else "(no checklist yet; use /work <task>)"
    if cmd in ("/permissions", "/perms"):
        return permission_policy(arg.strip())
    if cmd == "/quality":
        return memory_quality_report()
    if cmd == "/qualityfix":
        return memory_quality_repair(apply=(arg.strip().lower() == "apply"))
    if cmd in ("/privacy", "/privacyreview"):
        try:
            return memory_privacy_review(sample_limit=int(arg.strip() or 20))
        except ValueError:
            return "usage: /privacy [sample-limit]"
    if cmd == "/privacyfix":
        repair_arg = arg.strip()
        apply = False
        if repair_arg.lower().startswith("apply "):
            apply = True
            repair_arg = repair_arg[6:].strip()
        if not repair_arg:
            return "usage: /privacyfix [apply] <lesson-id[,lesson-id...]>"
        return memory_privacy_repair(lesson_ids_json=repair_arg, apply=apply)
    if cmd in ("/embeddings", "/embedfix"):
        embed_parts = arg.strip().split()
        apply = bool(embed_parts and embed_parts[0].lower() == "apply")
        if apply:
            embed_parts = embed_parts[1:]
        try:
            limit = int(embed_parts[0]) if embed_parts else 25
        except ValueError:
            return "usage: /embeddings [apply] [limit]"
        return memory_embedding_backfill(limit=limit, apply=apply)
    if cmd in ("/emotion", "/emotions", "/vectors", "/mood"):
        return emotion_command(arg)
    if cmd in ("/prefer", "/preference", "/preferences"):
        return preference_command(arg)
    if cmd in ("/improve", "/improvements"):
        return system_improvement_report()
    if cmd in ("/agents", "/masterstatus"):
        return master_status()
    if cmd in ("/capacity", "/agentcapacity"):
        try:
            requested = int(arg.strip() or 0)
        except ValueError:
            return "usage: /capacity [requested-agents]"
        return master_capacity(requested)
    if cmd in ("/agentcancel", "/cancelagents"):
        return master_cancel(arg.strip()) if arg.strip() else "usage: /agentcancel <id|prefix|all>"
    if cmd in ("/agentretry", "/retryagent"):
        retry_parts = arg.strip().split(None, 1)
        if not retry_parts:
            return "usage: /agentretry <master-id|prefix> [tier]"
        return master_retry(
            retry_parts[0], retry_parts[1] if len(retry_parts) > 1 else "",
        )
    if cmd in ("/asset", "/assets", "/assetgen", "/artifact"):
        asset_parts = arg.strip().split(None, 1)
        if len(asset_parts) != 2:
            return "usage: /asset <name> <free-form brief>"
        return artifact_generate(name=asset_parts[0], brief=asset_parts[1])
    if cmd in ("/artifactcheck", "/verifyartifact", "/groundartifact"):
        if not arg.strip():
            return "usage: /artifactcheck <path> [| recipe]"
        artifact_path, separator, recipe = arg.partition("|")
        return artifact_ground(
            path=artifact_path.strip(),
            recipe=recipe.strip() if separator else "auto",
        )
    if cmd in ("/weather", "/forecast"):
        if not arg.strip():
            return "usage: /weather <city/state or ZIP>"
        return weather_lookup(arg.strip())
    if cmd in ("/forge", "/gamesuite"):
        return game_reference_suite(name=arg.strip() or "sonder-reference")
    if cmd in ("/game", "/gamegen"):
        game_parts = arg.strip().split(None, 2)
        if len(game_parts) != 3 or "|" not in game_parts[2]:
            return "usage: /game <language> <2d|2.5d|3d> <name> | <concept>"
        game_name, _, concept = game_parts[2].partition("|")
        return game_generate_and_test(
            name=game_name.strip(), concept=concept.strip(),
            language=game_parts[0], dimension=game_parts[1],
        )
    if cmd in ("/gamefleet", "/gamecampaign"):
        campaign_args = _parse_game_campaign_command(arg)
        if campaign_args is None:
            return "usage: /gamefleet <name> | <concept> [| language | dimension]"
        return game_generation_campaign(**campaign_args)
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
    learning route is labeled 'sonder' on interactions but is gated by the same 'code'
    switch as offload's local coder, so both flip together."""
    return "code" if tier_label == "sonder" else tier_label


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


def _preference_facts(conn, limit=12):
    prefs = memory_store.preferences_for_scope(conn, "global", limit=limit)
    return ["User preference: %s" % p["text"] for p in prefs]


def _capture_preferences(conn, text, source_interaction=None, scope="global"):
    captured = []
    for pref in preference_learning.extract_preferences(text):
        key = preference_learning.preference_key(pref)
        memory_store.upsert_preference(
            conn,
            memory_store.new_id(),
            scope,
            key,
            pref,
            source_interaction=source_interaction,
            confidence=0.65,
        )
        captured.append(pref)
    return captured


def _answer(conn, prompt, model, effective_system, temperature, num_predict,
            num_ctx, session_id, project, history, trace=False,
            tier="sonder", cloud=False, augment=True):
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
        facts = _preference_facts(conn)
        if project:
            facts.extend(f["text"] for f in memory_store.facts_for_project(conn, project))
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
        _capture_preferences(conn, prompt, source_interaction=iid)
        return resp, iid, tctx
    resp, iid = orchestrator.run_with_learning(
        conn, prompt, tier, gen, retrieve_fn=retrieve_fn, history=history,
        recalls=recalls, facts=facts, session_id=session_id, task_embedding=blob,
    )
    _capture_preferences(conn, prompt, source_interaction=iid)
    return resp, iid, None


# --- chat code gate -----------------------------------------------------------
# Chat-path code answers used to ship unverified (runtime-broken code in two
# consecutive probes) while the gating infrastructure already existed for
# parallel_generate and /run. When a chat reply carries a runnable fenced
# Python block that defines real code (def/class/import), compile+smoke-run it
# in the same sandbox; on failure do one repair round-trip, then append an
# explicit NOT VERIFIED banner and record a negative outcome so broken code
# never distills into lessons. Python-only for now; opt out with
# SONDER_CODE_GATE=0.
_CODE_GATE_SIGNS = re.compile(
    r"^\s*(?:def\s+\w+|class\s+\w+|import\s+\w+|from\s+[\w.]+\s+import\s)",
    re.MULTILINE,
)
_CODE_GATE_TIMEOUT = 8


def _code_gate_enabled() -> bool:
    return os.environ.get("SONDER_CODE_GATE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _code_gate_target(reply):
    """Return the reply's runnable Python block worth gating, or None.

    Only fenced Python with real definitions/imports is gated (keeps latency
    off trivial snippet turns on this RAM-tight box), and interactive samples
    that read stdin are skipped: a smoke run would EOFError on correct code.
    """
    if "```" not in str(reply or ""):
        return None
    block = grounding.extract_runnable_code_block(reply)
    if not block or block.get("language") != "python":
        return None
    code = block.get("code") or ""
    if not _CODE_GATE_SIGNS.search(code):
        return None
    if re.search(r"\binput\s*\(", code):
        return None
    return code


def _record_code_gate_failure(interaction_id):
    """Record a negative 'failed' outcome for a reply whose code did not run.

    Best-effort: the auto-negative both keeps broken code out of lesson
    distillation and corrects the outcome-signal skew (previously ~97%
    positive because failures were simply never recorded)."""
    if not interaction_id:
        return
    try:
        conn = _open_db()
        try:
            r = reward.score("failed")
            memory_store.record_outcome_row(conn, interaction_id, "failed", r)
            memory_store.record_lesson_usage_outcome(
                conn, interaction_id, "failed", r,
            )
        finally:
            conn.close()
    except Exception:
        pass


def _apply_code_gate(reply, interaction_id=None, regenerate=None):
    """Compile+smoke-run the reply's runnable Python block before returning it.

    Returns (reply, verified):
      True  -> the block ran cleanly (reply unchanged, or the repaired reply).
      False -> still failing after one repair round-trip; the reply carries an
               explicit NOT VERIFIED banner and the captured interaction got a
               negative 'failed' outcome.
      None  -> nothing to gate, gate disabled, or inconclusive (a timeout is
               not treated as failure: long-running demos/servers are legal).
    """
    if not _code_gate_enabled():
        return reply, None
    code = _code_gate_target(reply)
    if code is None:
        return reply, None
    try:
        result = grounding.run_code_detail(
            code, timeout=_CODE_GATE_TIMEOUT, compile_first=True,
        )
    except Exception:
        return reply, None
    if result.get("ok"):
        return reply, True
    if result.get("timed_out"):
        return reply, None
    failure = (
        result.get("stderr") or result.get("stdout") or "exited with an error"
    ).strip()
    if regenerate is not None:
        repair_prompt = (
            "The Python code block in your previous answer fails when run:\n"
            "%s\n\nReturn the corrected complete answer with a fixed, "
            "runnable Python code block." % failure[:1200]
        )
        try:
            repaired = str(regenerate(repair_prompt) or "")
        except Exception:
            repaired = ""
        repaired_code = _code_gate_target(repaired) if repaired else None
        if repaired_code:
            try:
                retry = grounding.run_code_detail(
                    repaired_code, timeout=_CODE_GATE_TIMEOUT,
                    compile_first=True,
                )
            except Exception:
                retry = {"ok": False, "timed_out": False}
            if retry.get("ok"):
                return repaired, True
            if retry.get("timed_out"):
                return repaired, None
            failure = (
                retry.get("stderr") or retry.get("stdout") or failure
            ).strip() or failure
    summary = "exited with an error"
    for line in reversed(failure.splitlines()):
        if line.strip():
            summary = line.strip()
            break
    _record_code_gate_failure(interaction_id)
    return (
        "%s\n\nNOT VERIFIED: the Python code block in this answer fails when "
        "run (%s)." % (reply, summary[:300]),
        False,
    )


_existing_mcp = globals().get("_PERSISTENT_MCP")
if isinstance(_existing_mcp, reloadable_mcp.ReloadableFastMCP):
    mcp = _existing_mcp
    mcp.begin_module_refresh()
else:
    mcp = reloadable_mcp.ReloadableFastMCP("sonder-runtime")
_PERSISTENT_MCP = mcp


def _bounded_timeout(value) -> int:
    try:
        value = TIMEOUT if value is None else int(value)
    except (TypeError, ValueError):
        value = TIMEOUT
    return max(1, min(value, TIMEOUT))


def _post(path: str, payload: dict, timeout: int | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    request_timeout = _bounded_timeout(timeout)
    with urllib.request.urlopen(req, timeout=request_timeout) as resp:
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
    timeout: int = TIMEOUT,
) -> str:
    """Offload a self-contained subtask to a local-GPU or Ollama-cloud model.

    Local tiers (fast/code/general) run privately on the 6 GB 4050. The learning tiers
    (SONDER_LEARN_TIERS, default local 'code' + both cloud tiers) participate in the
    lesson loop: with learn=True (default) the call is captured and the response ends
    with a '[interaction_id: <id>]' footer you can pass to record_outcome once you know
    whether it compiled / passed tests, so a good outcome distills a lesson. The local
    'code' tier is also memory-augmented; cloud tiers answer without augmentation
    but are still captured — so a paid frontier model's grounded wins become lessons and
    fine-tuning data for the local model. 'fast'/'general' (mechanical work) and
    learn=False run the plain path: no capture, no footer, just text.

    Tiers: fast=3B (default), code=7B coder, general=7B instruct,
    cloud-code / cloud-general (METERED, prompt leaves this machine).
    Give a FULLY self-contained prompt (the model can't see this chat or your files).
    """
    _maybe_live_reload()
    request_timeout = _bounded_timeout(timeout)
    timeout = request_timeout
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
        started = time.time()
        ok = False
        usage = {}
        try:
            out = _post("/api/chat", payload, timeout=timeout)
            msg = out.get("message", {}).get("content", "")
            tokens_in = out.get("prompt_eval_count")
            tokens_out = out.get("eval_count")
            source = "ollama" if tokens_in is not None or tokens_out is not None else "estimated"
            if tokens_in is None:
                tokens_in = sum(
                    _rough_token_count(message.get("content", ""))
                    for message in messages
                )
            if tokens_out is None:
                tokens_out = _rough_token_count(msg)
            usage = {
                "tokens_in": int(tokens_in or 0),
                "tokens_out": int(tokens_out or 0),
                "token_source": source,
            }
            ok = True
            return msg if msg else "(empty response) raw=%s" % json.dumps(out)[:500]
        except urllib.error.URLError as e:
            return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                    "running? (the tray app / `ollama serve`)" % (BASE, e))
        finally:
            activity_tracker.record_model_call(
                model=model,
                prompt_chars=len(prompt or ""),
                history_messages=0,
                tokens_in=usage.get("tokens_in", 0),
                tokens_out=usage.get("tokens_out", 0),
                token_source=usage.get("token_source", ""),
                ok=ok,
                elapsed_ms=int((time.time() - started) * 1000),
            )

    # Learning path. Local tiers are answered by the selected local model behind
    # Sonder Runtime's learning route and augmented with lessons. Cloud tiers answer
    # cleanly without augmentation; grounded good outcomes can still be captured and
    # distilled into lessons for later local retrieval.
    retrieve_kwargs = {}
    if _is_cloud_tier(tier, model):
        gen = _make_generate(
            model, system, temperature, num_predict, num_ctx,
            cloud=True, timeout=request_timeout,
        )
        retrieve_kwargs["retrieve_fn"] = _no_retrieve
    else:
        learning_model = resolve_sonder_model(_STRICT_DEFAULT)
        if learning_model is None:
            return ("ERROR: `sonder:latest` Ollama alias not found. Run setup_alias.py, or call "
                    "with strict=False to fall back to the base coder.")
        gen = _make_generate(
            learning_model, system, temperature, num_predict, num_ctx,
            timeout=request_timeout,
        )
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


def _env_location_consent() -> bool:
    """Opt-in approximate-IP-location consent for the local MCP/REPL surfaces.

    Off by default to preserve the privacy contract. Set
    SONDER_LOCATION_CONSENT=1 to allow server-side approximate location lookup
    on this machine's own chat surfaces.
    """
    return os.environ.get("SONDER_LOCATION_CONSENT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _session_messages_light(conn, session_id, max_turns=None):
    """Recent session turns as chat messages, without summarization side effects."""
    msgs = []
    for task, resp in memory_store.session_history(
        conn, session_id, MAX_TURNS if max_turns is None else max_turns
    ):
        msgs.append({"role": "user", "content": task})
        msgs.append({"role": "assistant", "content": resp})
    return msgs


def _route_chat_web(prompt, session, project, location_consent):
    """Pre-model web routing for the local chat surfaces (MCP tool + REPL).

    Mirrors the serve handler's chat_web_response dispatch so the local model is
    never asked to answer weather/capability/current-info prompts it has no
    tools for (it would wrongly claim to have no internet access). Gated to
    non-work prompts so coding requests that merely mention e.g. "weather"
    still reach the model. A routed reply is stored on the session thread (so a
    bare follow-up location like "Chicago, IL" still routes on the next turn)
    but is NOT captured as a learnable interaction: no footer is returned, so
    record_outcome/lesson distillation can never ingest canned tool output, and
    the row has neither embedding nor outcome so recall/training skip it too.
    An explicit imperative search ("search the web for X", "look it up
    online") overrides the work gate: intents.classify_work also matches
    "search ... for ..." phrasings, but an explicit web-search order must
    reach the live tools, not the offline model.
    Returns the routed reply, or None to continue to the model.
    """
    if intents.classify_work(prompt) and not web_intents.explicit_search(prompt):
        return None
    session_id = _resolve_session(session)
    history = None
    if session_id:
        conn = _open_db()
        try:
            history = _session_messages_light(conn, session_id)
        finally:
            conn.close()
    reply = chat_web_response(
        prompt,
        history=history,
        tier="code",
        location_consent=location_consent,
        # This is the machine owner's own local surface (stdio MCP / REPL), so
        # a server-side lookup is allowed -- but still only behind the explicit
        # consent flag above.
        allow_server_location_lookup=location_consent,
    )
    if reply is None:
        return None
    if session_id:
        conn = _open_db()
        try:
            memory_store.touch_session(conn, session_id, _resolve_project(project))
            memory_store.log_interaction(
                conn, memory_store.new_id(), prompt, "", reply, "web-routed",
                session_id=session_id,
            )
        except Exception:
            pass
        finally:
            conn.close()
    return reply


def _sonder_impl(
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
    location_consent: bool = None,
) -> str:
    """Ask through Sonder Runtime's local learning loop.

    This is the interactive front door to the same learning loop the fleet uses:
    the prompt is augmented with project facts, lessons distilled from past work, and
    similar past solutions, answered locally on the 4050, captured, and returned with
    a '[interaction_id: <id>]' footer. After you learn how it went, call
    record_outcome(<id>, "tests_passed" | "used" | "copied" | "edited" |
    "accepted" | "compiled" | "rejected" | "failed") so Sonder Runtime can learn
    over time. The route uses the selected coder base model or the `sonder:latest`
    Ollama alias when it exists.

    `tier` picks which model answers (default "" / "sonder" = the local learning route).
    Pass any tier name (e.g. "cloud-code") to route this call to that model instead —
    cloud/non-learning tiers answer CLEAN (no lesson/fact injection) but
    are still captured, so a stronger model's grounded good outcomes distill into
    lessons for future local retrieval. Conversation memory (session) is threaded either
    way. The turn is always captured (the tool is the deliberate learning front door);
    LEARN_TIERS governs the automatic capture in offload / the serve layer instead.

    CONVERSATION MEMORY IS ON BY DEFAULT. Successive calls remember each other: with
    no `session`, the shared "default" thread is used, so follow-ups have context.
    Pass a distinct `session` id to keep an isolated thread (recommended: one id per
    conversation), or session="none" for a one-off single-turn answer. Threads persist
    in memory.db across restarts; older turns are auto-summarized to stay in the local
    context window (the most recent turns are kept verbatim). Use sonder_sessions()
    to list threads.

    `project` scopes durable facts (see sonder_remember_fact); those facts are
    always injected. No project -> the "default" project; project="none" -> no facts.

    trace=True instructs the model to externalize its step-by-step reasoning
    ('## Reasoning' then '## Answer'), and appends a TRACE block showing the SYSTEM's
    actual decision context (retrieved lessons, exact augmented prompt, model/params).

    strict=True (or env SONDER_STRICT=1) pins this call to the stable
    `sonder:latest` Ollama alias, erroring if it isn't installed instead of falling back.

    persona selects one of personas.names() (e.g. "explainer", "reviewer", "teacher")
    to steer tone; its system prompt is prepended ahead of `system`/trace instructions.

    Chat prompts with an explicit web intent (weather, "do you have internet?",
    current-info) are answered by the live tool dispatch (chat_web_response)
    instead of plain generation, exactly like the serve/app surface.
    location_consent opts in to approximate IP location for "my area" weather
    (None = env SONDER_LOCATION_CONSENT, default off).
    """
    _maybe_live_reload()
    command = control_command(prompt, session=session, project=project)
    if command is not None:
        return _append_activity(command)
    location_consent = (
        _env_location_consent() if location_consent is None else bool(location_consent)
    )
    web_reply = _route_chat_web(prompt, session, project, location_consent)
    if web_reply is not None:
        return _append_activity(web_reply)
    tgt_model, cloud, augment, tier_label = _serve_target(tier, strict)
    if tier_label == "cloud-disabled":
        return _cloud_disabled_message()
    if tier_label is None:
        return "ERROR: unknown tier '%s'. Valid: sonder, %s." % (tier, _valid_tier_names())
    if tgt_model is None:
        return ("ERROR: `sonder:latest` Ollama alias not found. Run setup_alias.py, or call "
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

    replacement = _web_denial_guard(
        prompt, response, history=history,
        location_consent=location_consent,
        allow_server_location_lookup=location_consent,
    )
    if replacement is not None:
        # The refusal turn was already captured by _answer; purge it so it can
        # never distill into lessons or the training export.
        _discard_interaction(iid)
        return _append_activity(replacement)
    if web_tools.enabled() and web_intents.denies_web_access(response):
        # Guard miss (no re-dispatch possible), but the reply still denies web
        # access while web tools are actually enabled: keep the reply visible,
        # yet drop the captured interaction and suppress the footer so the
        # refusal never poisons lessons or the training export.
        _discard_interaction(iid)
        return _append_activity(response)

    def _code_repair(repair_prompt):
        gen = _make_generate(
            tgt_model, effective_system, temperature, num_predict,
            num_ctx_eff, cloud=cloud,
        )
        repair_history = list(history or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response},
        ]
        return gen(repair_prompt, repair_history)

    response, _code_verified = _apply_code_gate(
        response, interaction_id=iid, regenerate=_code_repair,
    )

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


@mcp.tool()
def sonder(
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
    location_consent: bool = None,
) -> str:
    """Ask through Sonder Runtime and show observable activity for the response."""
    command = control_command(prompt, session=session, project=project)
    if command is not None:
        return command
    label = "sonder:%s" % ((tier or "sonder").strip() or "sonder")
    with activity_tracker.response_span(
        label,
        prompt,
        surface="terminal/mcp",
        model=tier or "sonder",
        session=session,
        project=project,
    ) as response:
        result = _sonder_impl(
            prompt,
            system=system,
            temperature=temperature,
            num_predict=num_predict,
            num_ctx=num_ctx,
            context_size=context_size,
            trace=trace,
            strict=strict,
            persona=persona,
            session=session,
            project=project,
            tier=tier,
            location_consent=location_consent,
        )
    return _append_activity(result, response=response, replace=True)


def _answer_with_history_impl(
    prompt,
    history,
    trace=False,
    strict=None,
    tier=None,
    context_size="",
    session="",
    project="",
):
    """Answer a turn using caller-supplied prior `history` (list of {role, content}).

    For the OpenAI-compatible serve layer, where the chat UI owns the conversation:
    history comes from the request, not the DB. Optional session/project tags
    still scope captured interactions and project facts.

    `tier` maps the request's OpenAI `model` field to a target (see _serve_target):
    default/"sonder" is the local learning route (augmented with facts + lessons);
    any other tier (e.g. a paid cloud model) answers without augmentation. The
    turn is always captured so record_outcome can ground it and distill lessons — so
    the runtime can learn from whichever model route you select. Returns the reply
    (with footer).
    """
    _maybe_live_reload()
    command = control_command(prompt, history=history, session=session, project=project)
    if command is not None:
        return _append_activity(command)
    model, cloud, augment, tier_label = _serve_target(tier, strict)
    if tier_label == "cloud-disabled":
        return _cloud_disabled_message()
    if tier_label is None:
        return "ERROR: unknown model '%s'. Valid: sonder, %s." % (
            tier, _valid_tier_names())
    if model is None:
        return ("ERROR: `sonder:latest` Ollama alias not found. Run setup_alias.py, or call "
                "with strict=False to fall back to the base coder.")
    effective_system = _build_system("", trace, "")
    # Honor LEARN_TIERS here too. Serve conversation memory is client-side (the app
    # resends history each request), so a non-learning model can skip capture entirely:
    # no interaction row, no footer, nothing distilled. This lets a user exclude e.g.
    # cloud from learning and have the app respect it. The local route is gated via 'code'.
    learn = _should_learn(_canonical_learn_tier(tier_label), True)
    req_ctx = _context_requested(context_size or SESSION_NUM_CTX)
    session_id = _resolve_session(session) if (session or "").strip() else None
    project_id = _resolve_project(project)
    conn = _open_db()
    try:
        if session_id:
            memory_store.touch_session(conn, session_id, project_id)
        if learn:
            capture_project = project_id if augment else None
            response, iid, trace_ctx = _answer(
                conn, prompt, model, effective_system, 0.2, 1024, req_ctx,
                session_id, capture_project, history or None, trace=trace,
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
    # The serve handler already routes web intents pre-model (no double routing
    # here); this is only the post-hoc net for denial phrasings it missed.
    replacement = _web_denial_guard(prompt, response, history=history)
    if replacement is not None:
        _discard_interaction(iid)
        return _append_activity(replacement)
    if web_tools.enabled() and web_intents.denies_web_access(response):
        # Guard miss: the reply denies web access while web tools are enabled.
        # Drop the captured refusal and return it footer-less so it can never
        # reach lessons or the training export.
        _discard_interaction(iid)
        return _append_activity(response)

    def _code_repair(repair_prompt):
        gen = _make_generate(model, effective_system, 0.2, 1024, req_ctx,
                             cloud=cloud)
        repair_history = list(history or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response},
        ]
        return gen(repair_prompt, repair_history)

    response, _code_verified = _apply_code_gate(
        response, interaction_id=iid, regenerate=_code_repair,
    )
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
    return _append_activity(response)


def answer_with_history(
    prompt,
    history,
    trace=False,
    strict=None,
    tier=None,
    context_size="",
    session="",
    project="",
):
    label = "chat:%s" % ((tier or "sonder").strip() or "sonder")
    with activity_tracker.response_span(
        label,
        prompt,
        surface="chat-api",
        model=tier or "sonder",
        session=session,
        project=project,
    ) as response:
        result = _answer_with_history_impl(
            prompt,
            history,
            trace=trace,
            strict=strict,
            tier=tier,
            context_size=context_size,
            session=session,
            project=project,
        )
    return _append_activity(result, response=response, replace=True)


@mcp.tool()
def record_outcome(interaction_id: str, signal: str) -> str:
    """Feed a real-world outcome back into sonder's learning loop.

    Call this after a sonder/offload response once you know how it went.
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
    ("hello", "print exactly: sonder-ok"),
    ("sum", "compute 12 + 30 and print exactly: 42"),
    ("loop", "print the numbers 1, 2, and 3 each on its own line"),
    ("string", "reverse the string 'sonder' and print exactly: rednos"),
    ("branch", "if 17 is prime print exactly: prime"),
    ("list", "compute the sum of [2, 4, 6, 8] and print exactly: 20"),
]


def _campaign_expected(task_name):
    return {
        "hello": "sonder-ok",
        "sum": "42",
        "loop": "1\n2\n3",
        "string": "rednos",
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
                response = sonder(
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
def sonder_stats() -> str:
    """Report what sonder has learned so far.

    Read-only observability into the learning loop's SQLite memory: how many
    interactions have been logged, how outcomes break down by signal, and the
    most recently distilled lessons. Makes no model call and needs no Ollama —
    it only reads memory.db, so it works even if the Ollama server is down.
    """
    _maybe_live_reload()
    conn = _open_db()
    try:
        n_interactions = memory_store.count_interactions(conn)
        token_totals = memory_store.interaction_token_totals(conn)
        token_by_tier = memory_store.interaction_token_totals_by_tier(conn)
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
        "sonder learning stats",
        "  lessons: %d" % n_lessons,
        "  interactions: %d | outcomes: %d" % (n_interactions, n_outcomes),
        "  tokens: in=%d out=%d total=%d" % (
            token_totals["tokens_in"],
            token_totals["tokens_out"],
            token_totals["tokens_total"],
        ),
        "  token rows: exact=%d estimated_legacy=%d" % (
            token_totals["exact_rows"],
            token_totals["estimated_rows"],
        ),
        "  outcomes by signal: %s" % signals_line,
    ]
    if token_by_tier:
        lines.append("  tokens by tier:")
        for row in token_by_tier[:8]:
            lines.append(
                "    - %s: in=%d out=%d total=%d interactions=%d exact=%d estimated=%d" % (
                    row["tier"], row["tokens_in"], row["tokens_out"],
                    row["tokens_total"], row["interactions"],
                    row["exact_rows"], row["estimated_rows"],
                )
            )
    if lessons:
        lines.append("  recent lessons:")
        for lesson in lessons:
            lines.append("    - %s" % lesson["text"])
    else:
        lines.append("  recent lessons: (none yet)")
    return "\n".join(lines)


def learning_health_data() -> dict:
    """Return structured outcome grounding, lesson provenance, and hygiene metrics."""
    _maybe_live_reload()
    conn = _open_db()
    try:
        return learning_health.build_report(conn)
    finally:
        conn.close()


@mcp.tool()
def learning_health_status() -> str:
    """Show outcome coverage, positive signals, lesson provenance, and memory hygiene."""
    return learning_health.format_report(learning_health_data())


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
    plus the recent turns that Sonder keeps in the prompt.
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
        preference_count = conn.execute(
            "SELECT COUNT(*) FROM preferences WHERE enabled=1"
        ).fetchone()[0]
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
    memory_items = lesson_count + fact_count + preference_count + outcome_count
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
        "preferences": preference_count,
        "interactions": interaction_count,
        "outcomes": outcome_count,
        "memory_percent": round(memory_ratio * 100.0, 1),
        "memory_bar": _health_bar(memory_ratio),
        "db_path": _DB_PATH,
        "state_home": str(sonder_paths.default_home()),
    }


def format_context_health(data: dict) -> str:
    lines = [
        "sonder context health",
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
        "  memory  %s %s lessons, %s facts, %s prefs, %s interactions, %s outcomes" % (
            data.get("memory_bar", ""),
            data.get("lessons", 0),
            data.get("facts", 0),
            data.get("preferences", 0),
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
def activity_status(include_events: bool = True) -> str:
    """Show active and most recent observable response activity."""
    _maybe_live_reload()
    snap = activity_tracker.snapshot()
    lines = [
        "sonder activity",
        "  active responses: %s" % snap.get("active_count", 0),
        "  total tool calls since start: %s" % snap.get("total_tool_calls", 0),
    ]
    active = snap.get("active") or []
    if active:
        lines.append("  active:")
        for row in active[-8:]:
            last = row.get("last_event") or {}
            lines.append(
                "    %s %s tools=%s models=%s tokens=%s/%s last=%s" % (
                    row.get("id"),
                    row.get("label"),
                    row.get("tool_calls", 0),
                    row.get("model_calls", 0),
                    row.get("tokens_in", 0),
                    row.get("tokens_out", 0),
                    last.get("kind", "starting"),
                )
            )
    latest = snap.get("latest")
    if latest:
        lines.extend(["", activity_tracker.format_response(latest)])
    elif include_events:
        lines.append("  latest: (none yet)")
    return "\n".join(lines)


@mcp.tool()
def context_policy_status(context_size: str = "") -> str:
    """Show requested virtual context and actual Ollama native num_ctx."""
    _maybe_live_reload()
    return context_policy.format_policy(context_size or SESSION_NUM_CTX)


@mcp.tool()
def set_context_size(context_size: str) -> str:
    """Select Sonder's requested virtual context size, up to 1m by default."""
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
    parent_id: str = "",
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
            parent_id=parent_id,
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
    lines = ["sonder tasks"]
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
        "sonder context compaction plan",
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
    """Preview when/how Sonder should summarize or split context."""
    _maybe_live_reload()
    return format_context_compaction_plan(context_compaction_plan_data(session, project))


@mcp.tool()
def permission_policy(tool_name: str = "") -> str:
    """Show local permission rules, or the matching rule for one tool."""
    _maybe_live_reload()
    return permission_rules.format_policy(sonder_paths.default_home(), tool_name)


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
    env_ok = os.environ.get("SONDER_ALLOW_PERMISSION_EDITS", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if not ok and not env_ok:
        return (
            "ERROR: permission edits require a developer token or "
            "SONDER_ALLOW_PERMISSION_EDITS=1."
        )
    try:
        permission_rules.add_rule(sonder_paths.default_home(), pattern, action, note)
    except Exception as e:
        return "ERROR: %s" % e
    return permission_rules.format_policy(sonder_paths.default_home())


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


def _parse_lesson_ids(value):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            values = []
        elif text.startswith("["):
            values = json.loads(text)
        else:
            values = [part for part in re.split(r"[\s,]+", text) if part]
    else:
        values = value
    if not isinstance(values, list):
        raise ValueError("lesson IDs must be a JSON list or comma-separated text")
    if len(values) > 50:
        raise ValueError("at most 50 lesson IDs can be reviewed at once")
    out = []
    for raw in values:
        lesson_id = str(raw or "").strip()
        if not lesson_id or len(lesson_id) > 128 or any(ord(ch) < 32 for ch in lesson_id):
            raise ValueError("invalid lesson ID")
        if lesson_id not in out:
            out.append(lesson_id)
    return out


@mcp.tool()
def memory_privacy_review(sample_limit: int = 20) -> str:
    """List redacted path/credential-like lessons without revealing raw values."""
    _maybe_live_reload()
    sample_limit = _safe_limit(sample_limit, 20, 100)
    conn = _open_db()
    try:
        findings = memory_quality.privacy_findings(conn, limit=sample_limit)
        total = memory_quality.audit(conn).get("path_or_secret_like", 0)
    finally:
        conn.close()
    lines = [
        "memory privacy review",
        "  flagged: %d | showing: %d" % (total, len(findings)),
        "  previews are redacted; no raw credential/path values are shown.",
    ]
    for row in findings:
        lines.append("  %s [%s] %s" % (
            row["id"], ",".join(row.get("reasons") or []),
            row.get("preview") or "<empty>",
        ))
    if not findings:
        lines.append("  (no privacy-like lessons found)")
    else:
        lines.append(
            "  cleanup: memory_privacy_repair(lesson_ids_json=[...], apply=False), then apply=True."
        )
    return "\n".join(lines)


@mcp.tool()
def memory_privacy_repair(lesson_ids_json: str = "[]", apply: bool = False) -> str:
    """Delete only explicitly selected, currently privacy-flagged lessons; dry-run by default."""
    _maybe_live_reload()
    try:
        lesson_ids = _parse_lesson_ids(lesson_ids_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return "ERROR: %s" % exc
    if not lesson_ids:
        return "ERROR: provide one or more lesson IDs from memory_privacy_review."
    conn = _open_db()
    try:
        plan = memory_quality.privacy_cleanup_plan(conn, lesson_ids)
        deleted = memory_quality.apply_privacy_cleanup(conn, plan) if apply else 0
    finally:
        conn.close()
    lines = [
        "memory privacy repair",
        "  mode: %s" % ("apply" if apply else "dry-run"),
        "  eligible flagged lessons: %d" % len(plan["eligible"]),
        "  not flagged: %d | missing: %d | deleted: %d" % (
            len(plan["not_flagged"]), len(plan["missing"]), deleted,
        ),
    ]
    for row in plan["eligible"]:
        lines.append("  %s [%s] %s" % (
            row["id"], ",".join(row.get("reasons") or []),
            row.get("preview") or "<empty>",
        ))
    if plan["not_flagged"]:
        lines.append("  refused unflagged IDs: %s" % ", ".join(plan["not_flagged"]))
    if plan["missing"]:
        lines.append("  missing IDs: %s" % ", ".join(plan["missing"]))
    if not apply and plan["eligible"]:
        lines.append("  reviewed only; rerun the same explicit IDs with apply=True to delete.")
    return "\n".join(lines)


@mcp.tool()
def memory_embedding_backfill(limit: int = 25, apply: bool = False) -> str:
    """Backfill missing lesson vectors with the configured local embedding model."""
    _maybe_live_reload()
    if _is_cloud_model_name(embeddings.EMBED_MODEL):
        return (
            "ERROR: embedding backfill requires a local model; configured model "
            "%r looks cloud-hosted." % embeddings.EMBED_MODEL
        )
    limit = _safe_limit(limit, 25, 100)
    conn = _open_db()
    updated = 0
    failed = []
    try:
        rows = memory_store.lessons_without_embeddings(conn, limit=limit)
        if apply:
            for row in rows:
                try:
                    vector = embeddings.embed(row.get("text") or "", timeout=30)
                    if not isinstance(vector, (list, tuple)) or not vector:
                        failed.append(row["id"])
                        continue
                    blob = embeddings.to_blob(vector)
                    if memory_store.set_lesson_embedding(conn, row["id"], blob):
                        updated += 1
                except (OSError, TypeError, ValueError, OverflowError):
                    failed.append(row["id"])
        remaining = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE embedding IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    lines = [
        "memory embedding backfill",
        "  mode: %s | local model: %s" % (
            "apply" if apply else "dry-run", embeddings.EMBED_MODEL,
        ),
        "  selected: %d | updated: %d | failed: %d | remaining: %d" % (
            len(rows), updated, len(failed), remaining,
        ),
    ]
    if rows:
        lines.append("  lesson IDs: %s" % ", ".join(row["id"] for row in rows))
    if failed:
        lines.append("  failed IDs: %s" % ", ".join(failed))
    if not apply and rows:
        lines.append("  dry-run only; rerun with apply=True to call the local embedding model.")
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
    if cloud_allowed():
        lines.append(
            "cloud tiers are available; opt into cloud learning explicitly with "
            "SONDER_LEARN_TIERS"
        )
    else:
        lines.append(
            "cloud tiers require SONDER_ALLOW_CLOUD=1; override learning with "
            "SONDER_LEARN_TIERS"
        )
    return "\n".join(lines)


def improvement_report_data(session: str = "", project: str = "") -> dict:
    """Machine-readable next-step report for system self-improvement."""
    _maybe_live_reload()
    context = context_health_data(session=session, project=project)
    conn = _open_db()
    try:
        learning_state = learning_health.build_report(conn)
    finally:
        conn.close()

    quality = learning_state["quality"]
    interactions = learning_state["interactions"]
    outcomes = learning_state["outcomes"]
    lesson_count = learning_state["lessons"]
    fact_count = learning_state["facts"]
    acceptance = learning_state["positive_percent"] / 100.0
    issues = []
    try:
        autopilot = autopilot_store.snapshot(include_finished=False, limit=100)
    except Exception:
        autopilot = {
            "active_runs": 0,
            "resumable_runs": 0,
            "runs": [],
            "database": autopilot_store.database_path(),
        }
    mcp_state = mcp_runtime_data()

    def add(area, severity, title, action):
        issues.append({
            "area": area,
            "severity": severity,
            "title": title,
            "action": action,
        })

    if interactions and learning_state["outcome_coverage_percent"] < 35.0:
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
            "Ask through Sonder Runtime or run /train so answers can become local lessons.",
        )
    if lesson_count < 10:
        add(
            "memory",
            "medium",
            "Lesson memory is still thin.",
            "Run grounded practice or teach examples from known-good work.",
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
            "Run /privacy for redacted IDs, then dry-run memory_privacy_repair before any explicit cleanup.",
        )
    if quality.get("no_embedding", 0):
        add(
            "memory",
            "medium",
            "%d lessons are missing semantic embeddings." % quality["no_embedding"],
            "Run memory_embedding_backfill(apply=False), then backfill a bounded batch with apply=True.",
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
            "Run self_heal_check and repair the store before large practice batches.",
        )
    if context.get("status") == "hot":
        add(
            "context",
            "medium",
            "The active conversation is near the context limit.",
            "Start a new session or let summaries compress older turns before continuing.",
        )
    autonomous_attention = sum(
        1 for row in autopilot.get("runs", [])
        if row.get("status") in ("blocked", "interrupted")
    )
    if autonomous_attention:
        add(
            "autonomy",
            "medium",
            "%d autonomous run(s) need explicit review or resume."
            % autonomous_attention,
            "Inspect /autopilot status, then deliberately resume, cancel, or revise the goal.",
        )
    if mcp_state.get("last_error"):
        add(
            "runtime",
            "high",
            "The latest MCP source refresh failed closed.",
            "Run /mcp status, fix the reported source error, then use /mcp refresh; the last known-good tools remain active.",
        )
    elif mcp_state.get("source_changed"):
        add(
            "runtime",
            "medium",
            "The MCP process has newer source waiting to be loaded.",
            "Run /mcp refresh or any MCP tool/list request to publish the update atomically.",
        )
    if mcp_state.get("last_notification_error"):
        add(
            "runtime",
            "low",
            "The MCP client did not accept the latest tool-list notification.",
            "Use /mcp status and reconnect only if the client does not relist tools automatically.",
        )
    if not cloud_allowed():
        add(
            "deployment",
            "info",
            "Hosted tiers are disabled, preserving the local privacy promise.",
            "Enable hosted/cloud tiers only when you intentionally want prompts to leave this machine.",
        )
    manifest_text = tool_manifest()
    if "ground_artifact" not in manifest_text or "artifact_ground" not in manifest_text:
        add(
            "grounding",
            "medium",
            "General or format-specific artifact grounding is not advertised.",
            "Expose ground_artifact and artifact_ground so both in-memory content and real files can be validated.",
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
            "no_embedding": quality.get("no_embedding", 0),
            "path_or_secret_like": quality.get("path_or_secret_like", 0),
            "fts_issues": quality.get("missing_fts", 0) + quality.get("orphan_fts", 0),
        },
        "learning_health": learning_state,
        "autopilot": {
            "active": autopilot.get("active_runs", 0),
            "resumable": autopilot.get("resumable_runs", 0),
            "database": autopilot.get("database", ""),
        },
        "mcp_runtime": mcp_state,
        "issues": issues,
    }


def format_improvement_report(report: dict) -> str:
    lines = [
        "sonder improvement report",
        "  readiness score: %s/100" % report.get("score", 0),
        "  learning: %s interactions, %s outcomes, %s%% covered, %s%% positive" % (
            report.get("interactions", 0),
            report.get("outcomes", 0),
            report.get("learning_health", {}).get("outcome_coverage_percent", 0),
            report.get("acceptance_percent", 0),
        ),
        "  memory: %s lessons, %s facts, duplicate rows=%s, vague=%s, missing embeddings=%s" % (
            report.get("lessons", 0),
            report.get("facts", 0),
            report.get("memory_quality", {}).get("duplicates", 0),
            report.get("memory_quality", {}).get("vague", 0),
            report.get("memory_quality", {}).get("no_embedding", 0),
        ),
        "  context: %s | hosted/cloud: %s" % (
            report.get("context_status", "unknown"),
            "enabled" if report.get("cloud_allowed") else "disabled",
        ),
        "  autonomy: %s active | %s resumable" % (
            report.get("autopilot", {}).get("active", 0),
            report.get("autopilot", {}).get("resumable", 0),
        ),
        "  mcp: %s | %s tools | %s atomic refreshes" % (
            report.get("mcp_runtime", {}).get("status", "unknown"),
            report.get("mcp_runtime", {}).get("registered_tools", 0),
            report.get("mcp_runtime", {}).get("refresh_count", 0),
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


def _master_timeout(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(15, min(value, TIMEOUT))


def _orchestrator_worker(tier: str, learn: bool = False, timeout: int = 150):
    response_id = activity_tracker.current_response_id()

    def worker(prompt: str) -> str:
        with activity_tracker.bind_response(response_id):
            return offload(
                prompt=prompt,
                tier=tier,
                temperature=0.2,
                num_predict=1400,
                learn=learn,
                timeout=timeout,
            )
    return worker


def _orchestrator_agent_worker(tier: str, max_steps: int = 8):
    response_id = activity_tracker.current_response_id()

    def worker(prompt: str) -> str:
        with activity_tracker.bind_response(response_id):
            return _agent_impl(
                prompt,
                tier=tier,
                max_steps=max_steps,
                allow_web=False,
                require_file_evidence=True,
                read_only=True,
                include_evidence=True,
            )
    return worker


def _master_grounded_build(
    task: str, mode: str, tier: str, intent: dict, retry_of: str = "",
) -> str:
    """Execute an explicit greenfield creative request through a verified forge."""
    kind = intent["kind"]

    def build(_prompt: str) -> str:
        if kind == "artifact":
            return artifact_generate(
                name=intent["name"],
                brief=intent["brief"],
                kinds=intent["kinds"],
                dimension=intent["dimension"],
                theme=intent["theme"],
            )
        if kind == "game_campaign":
            total = int(intent.get("total") or 4)
            workers = master_orchestrator.parallel_worker_slots(total)
            return game_generation_campaign(
                name=intent["name"],
                concept=intent["concept"],
                total=total,
                language=(
                    intent["language"] if intent.get("language_explicit") else ""
                ),
                dimension=(
                    intent["dimension"] if intent.get("dimension_explicit") else ""
                ),
                theme=intent["theme"],
                tier=tier,
                max_workers=workers,
                timeout=30,
                repair_rounds=1,
                use_reference_fallback=True,
            )
        return game_generate_and_test(
            name=intent["name"],
            concept=intent["concept"],
            language=intent["language"],
            dimension=intent["dimension"],
            theme=intent["theme"],
            tier=tier,
            timeout=30,
            repair_rounds=1,
            use_reference_fallback=True,
        )

    result = master_orchestrator.run_inline(
        task,
        build,
        metadata={
            "tier": tier, "mode": "forge-%s" % kind,
            "retry_of": retry_of,
        },
    )
    return "\n".join([
        "master grounded build complete",
        "  route: %s | master=%s | requested mode=%s" % (
            kind.replace("_", "-"), result["master_id"], mode,
        ),
        "  contract: persistent files + deterministic verification",
        "",
        result["output"],
    ]).strip()


@mcp.tool()
def master_orchestrate(
    task: str,
    mode: str = "ask",
    agents: int = 3,
    tier: str = "auto",
    learn: bool = False,
    retry_of: str = "",
) -> str:
    """Run a master pass inline or with hardware-scheduled delegated agents.

    mode="ask" returns the choice prompt. mode="inline" keeps work in the master
    lane. mode="delegate" queues bounded subagents across RAM/CPU-safe worker
    slots, then audits and merges their outputs. mode="fleet" queues the full
    hardware-derived breadth ceiling. Status is visible through master_status().
    """
    _maybe_live_reload()
    task = (task or "").strip()
    mode = (mode or "ask").strip().lower()
    mode = {
        "delagte": "delegate",
        "delegte": "delegate",
        "paralell": "parallel",
        "inlne": "inline",
        "workflow": "fleet",
    }.get(mode, mode)
    if master_orchestrator.requests_fleet(task):
        if mode in ("ask", "choose", "prompt", "delegate", "delegated", "agents", "parallel"):
            mode = "fleet"
            agents = master_orchestrator.max_agents()
    if mode in ("ask", "choose", "prompt"):
        delegate_count = master_orchestrator.clamp_agent_count(agents, default=3)
        delegate_capacity = master_orchestrator.capacity(delegate_count)
        fleet_capacity = master_orchestrator.capacity(master_orchestrator.max_agents())
        return (
            "Master orchestrator ready.\n"
            "Choose execution mode:\n"
            "  inline   - master handles the task directly.\n"
            "  delegate - queue %d agent(s) across %d safe worker slot(s), audit, then merge.\n"
            "  fleet    - queue the hardware ceiling (%d agents) across %d safe worker slot(s).\n"
            "Keywords fleet, swarm, spawn as many agents, parallel agents, and\n"
            "parallel workflow select fleet automatically.\n"
            "Call master_orchestrate(task, mode='inline'|'delegate'|'fleet') or chat `/master inline ...`."
        ) % (
            delegate_count,
            delegate_capacity["worker_slots"],
            master_orchestrator.max_agents(),
            fleet_capacity["worker_slots"],
        )
    if not task:
        return "ERROR: empty task."
    tier = _runtime_lane_tier("fleet", tier)
    audit_tier = _runtime_lane_tier("review")
    creative_intent = creative_router.classify(task, mode=mode)
    if creative_intent:
        return _master_grounded_build(
            task, mode, tier, creative_intent, retry_of=retry_of,
        )
    needs_repo_tools = master_orchestrator.requires_repository_tools(task)
    worker = (
        _orchestrator_agent_worker(tier)
        if needs_repo_tools
        else _orchestrator_worker(
            tier,
            learn=learn,
            timeout=_master_timeout("SONDER_MASTER_AGENT_TIMEOUT", 150),
        )
    )
    if mode in ("inline", "master"):
        result = master_orchestrator.run_inline(
            task,
            worker,
            metadata={"tier": tier, "mode": "inline", "retry_of": retry_of},
        )
        return result["output"]
    if mode in ("delegate", "delegated", "agents", "parallel", "fleet", "swarm", "fanout"):
        if mode in ("fleet", "swarm", "fanout"):
            agents = master_orchestrator.max_agents()
        result = master_orchestrator.run_delegated(
            task,
            worker_fn=worker,
            audit_fn=_orchestrator_worker(
                audit_tier,
                learn=False,
                timeout=_master_timeout("SONDER_MASTER_AUDIT_TIMEOUT", 120),
            ),
            agents=agents,
            metadata={
                "tier": tier,
                "audit_tier": audit_tier,
                "mode": mode,
                "retry_of": retry_of,
            },
        )
        lines = [
            "master orchestration complete",
            "mode: %s | master=%s | agents=%d" % (
                "fleet" if mode in ("fleet", "swarm", "fanout") else "delegated",
                result["master_id"], len(result.get("agents") or [])),
            "worker slots used: %d (bounded concurrent model calls)" % result.get("worker_slots", 1),
            "",
            result["output"],
        ]
        return "\n".join(lines).strip()
    return "ERROR: unknown mode '%s'. Use ask, inline, delegate, or fleet." % mode


@mcp.tool()
def master_status(include_finished: bool = True, limit: int = 20) -> str:
    """Show live master/subagent activity, token estimates, and recent actions."""
    _maybe_live_reload()
    return master_orchestrator.format_snapshot(
        master_orchestrator.snapshot(include_finished=include_finished, limit=limit)
    )


@mcp.tool()
def master_capacity(requested_agents: int = 0) -> str:
    """Show queued-agent ceiling and current RAM/CPU-bounded worker slots."""
    _maybe_live_reload()
    try:
        value = int(requested_agents or 0)
    except (TypeError, ValueError):
        value = 0
    requested = value if value > 0 else None
    return master_orchestrator.format_capacity(
        master_orchestrator.capacity(requested)
    )


@mcp.tool()
def master_cancel(agent_id: str) -> str:
    """Cooperatively cancel one active agent/master prefix or all active agents."""
    _maybe_live_reload()
    selector = str(agent_id or "").strip()
    if not selector:
        return "ERROR: agent_id is required; pass an exact ID, unique prefix, or 'all'."
    result = master_orchestrator.request_cancel(selector)
    if not result["matched"]:
        return "ERROR: no active agent matched %r." % selector
    lines = [
        "master cancellation requested",
        "  selector: %s | matched: %d" % (selector, result["matched"]),
        "  queued cancelled: %d | active model calls awaiting return: %d" % (
            result["queued"], result["model_calls"],
        ),
        "  running agents signalled: %d" % result["running"],
        "  cooperative: active Ollama/HTTP calls cannot be force-killed; late results are discarded.",
    ]
    lines.append("  agents: %s" % ", ".join(result["agent_ids"]))
    return "\n".join(lines)


@mcp.tool()
def master_retry(agent_id: str, tier: str = "") -> str:
    """Explicitly rerun one interrupted/failed/cancelled persisted master task."""
    _maybe_live_reload()
    selector = str(agent_id or "").strip()
    if not selector:
        return "ERROR: agent_id is required; pass an exact master ID or unique prefix."
    candidate = master_orchestrator.recovery_candidate(selector)
    if not candidate:
        return "ERROR: no unambiguous persisted master matched %r." % selector
    status = candidate.get("status") or ""
    if status not in ("interrupted", "failed", "cancelled"):
        return "ERROR: master %s is %s; only interrupted/failed/cancelled work can be retried." % (
            candidate["id"], status or "unknown",
        )
    task = (candidate.get("task") or "").strip()
    if not task:
        return "ERROR: persisted master %s has no recoverable task text." % candidate["id"]
    mode = (candidate.get("mode") or "delegated").lower()
    if mode not in (
        "inline", "master", "delegate", "delegated", "agents", "parallel",
        "fleet", "swarm", "fanout",
    ):
        mode = "delegated"
    agents = int(candidate.get("requested_agents") or 3)
    retry_tier = str(tier or "code").strip() or "code"
    result = master_orchestrate(
        task=task, mode=mode, agents=agents, tier=retry_tier, learn=False,
        retry_of=candidate["id"],
    )
    return "\n".join([
        "persisted master retry",
        "  source: %s [%s] | mode: %s | agents: %d" % (
            candidate["id"], status, mode, agents,
        ),
        "  tier: %s (explicit/local-safe default; original=%s)" % (
            retry_tier, candidate.get("tier") or "unknown",
        ),
        "",
        result,
    ]).strip()


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
        "sonder admin status",
        "  accounts: %d" % count,
        "  auth mode: %s" % ("api-key" if os.environ.get("SONDER_API_KEY") else "local-open"),
        "  require account: %s" % os.environ.get("SONDER_REQUIRE_ACCOUNT", "0"),
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
        "sonder debug inspect",
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


def _file_developer_allowed(token: str = "") -> bool:
    if not token:
        return False
    account = _admin_account_from_token(token)
    ok, _ = admin_auth.require(account, "developer")
    return ok


def _file_bypass_allowed(token: str = "", approval: str = "") -> bool:
    if file_ops.bypass_enabled():
        return True
    expected = os.environ.get("SONDER_FILE_APPROVAL_CODE", "").strip()
    if expected and approval and approval == expected:
        return True
    return _file_developer_allowed(token)


def _format_file_result(title: str, data: dict) -> str:
    lines = [title]
    for key, value in data.items():
        if key == "text":
            continue
        lines.append("  %s: %s" % (key, value))
    if "text" in data:
        lines.extend(["", data["text"]])
    return "\n".join(lines)


def _checklist_data(conn, checklist_id: str) -> dict:
    parent = memory_store.get_task(conn, checklist_id)
    if not parent:
        raise ValueError("no checklist '%s'" % checklist_id)
    items = memory_store.task_children(conn, parent["id"])
    done = sum(1 for item in items if item.get("status") == "done")
    return {
        "id": parent["id"],
        "title": parent.get("title", ""),
        "status": parent.get("status", "pending"),
        "project": parent.get("project", ""),
        "owner": parent.get("owner", ""),
        "items": items,
        "summary": "%d/%d complete" % (done, len(items)),
    }


def _format_checklist(data: dict) -> str:
    symbols = {"done": "[x]", "in_progress": "[~]", "blocked": "[!]", "canceled": "[-]"}
    lines = [
        "sonder checklist %s" % data.get("id", "")[:8],
        "  %s [%s] %s" % (
            data.get("title", ""), data.get("status", "pending"),
            data.get("summary", "0/0 complete"),
        ),
    ]
    for index, item in enumerate(data.get("items") or [], 1):
        lines.append("  %s %d. %s  (%s)" % (
            symbols.get(item.get("status"), "[ ]"), index,
            item.get("title", ""), item.get("id", "")[:8],
        ))
    if not data.get("items"):
        lines.append("  (no checklist items)")
    return "\n".join(lines)


@mcp.tool()
def checklist_create(
    title: str,
    items_json: str,
    project: str = "",
    owner: str = "agent",
    priority: int = 1,
) -> str:
    """Create a persistent parent task with ordered checklist items."""
    _maybe_live_reload()
    started = time.time()
    try:
        items = json.loads(items_json) if isinstance(items_json, str) else items_json
        if not isinstance(items, list) or not items:
            raise ValueError("items_json must be a non-empty JSON list")
        if len(items) > 20:
            raise ValueError("a checklist supports at most 20 items")
        normalized_items = []
        for item in items:
            if isinstance(item, dict):
                item_title = str(item.get("title", "")).strip()
                detail = str(item.get("detail", ""))
            else:
                item_title = str(item).strip()
                detail = ""
            if not item_title:
                raise ValueError("checklist item titles cannot be empty")
            normalized_items.append((item_title, detail))
        conn = _open_db()
        try:
            parent = memory_store.create_task(
                conn, title=title, detail="work checklist", status="in_progress",
                priority=priority, project=project, owner=owner,
            )
            for item_title, detail in normalized_items:
                memory_store.create_task(
                    conn, title=item_title, detail=detail, status="pending",
                    priority=priority, project=project, owner=owner,
                    parent_id=parent["id"],
                )
            data = _checklist_data(conn, parent["id"])
        finally:
            conn.close()
    except Exception as exc:
        _record_direct_tool("checklist_create", {"title": title}, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    _record_direct_tool(
        "checklist_create", {"title": title, "items": len(items)},
        ok=True, started=started, summary=data["summary"],
    )
    activity_tracker.set_checklist(data)
    return _format_checklist(data)


@mcp.tool()
def checklist_show(checklist_id: str) -> str:
    """Show a checklist with Codex-style pending/active/done markers."""
    _maybe_live_reload()
    started = time.time()
    try:
        conn = _open_db()
        try:
            data = _checklist_data(conn, checklist_id)
        finally:
            conn.close()
    except Exception as exc:
        _record_direct_tool(
            "checklist_show", {"checklist_id": checklist_id},
            ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    activity_tracker.set_checklist(data)
    output = _format_checklist(data)
    _record_direct_tool(
        "checklist_show", {"checklist_id": checklist_id},
        ok=True, started=started, summary=data["summary"], output=output,
    )
    return output


@mcp.tool()
def checklist_update(
    checklist_id: str,
    item: str,
    status: str,
    note: str = "",
) -> str:
    """Update one checklist item by 1-based index or id prefix."""
    _maybe_live_reload()
    started = time.time()
    try:
        conn = _open_db()
        try:
            data = _checklist_data(conn, checklist_id)
            children = data["items"]
            selected = None
            value = str(item or "").strip()
            if value.isdigit() and 1 <= int(value) <= len(children):
                selected = children[int(value) - 1]
            else:
                matches = [row for row in children if row["id"].startswith(value)]
                if len(matches) == 1:
                    selected = matches[0]
            if not selected:
                raise ValueError("no unique checklist item '%s'" % item)
            memory_store.update_task(
                conn, selected["id"], status=status, note=note or "checklist update",
            )
            data = _checklist_data(conn, checklist_id)
            states = [row.get("status") for row in data["items"]]
            parent_status = (
                "done" if states and all(state in ("done", "canceled") for state in states)
                else "blocked" if "blocked" in states
                else "in_progress"
            )
            memory_store.update_task(
                conn, data["id"], status=parent_status,
                note="checklist %s" % data["summary"],
            )
            data = _checklist_data(conn, checklist_id)
        finally:
            conn.close()
    except Exception as exc:
        _record_direct_tool(
            "checklist_update", {"checklist_id": checklist_id, "item": item, "status": status},
            ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    _record_direct_tool(
        "checklist_update", {"checklist_id": checklist_id, "item": item, "status": status},
        ok=True, started=started, summary=data["summary"],
    )
    activity_tracker.set_checklist(data)
    return _format_checklist(data)


def _record_file_activity(default_action: str, data: dict) -> None:
    if not isinstance(data, dict):
        return
    action = data.get("action") or default_action
    activity_tracker.record_file_change(
        action,
        data.get("path", ""),
        lines_added=data.get("lines_added", 0),
        lines_edited=data.get("lines_edited", 0),
        lines_deleted=data.get("lines_deleted", 0),
        bytes_written=data.get("bytes", 0),
        dry_run=data.get("dry_run", False),
        summary="%s bytes" % data.get("bytes", 0) if data.get("bytes") else "",
    )


def _record_direct_tool(
    name: str, args=None, ok=True, started=None, summary="", command="", output="",
) -> None:
    if activity_tracker.inside_tool_call():
        return
    elapsed_ms = int((time.time() - started) * 1000) if started else 0
    activity_tracker.record_tool_result(
        name,
        args or {},
        ok=ok,
        elapsed_ms=elapsed_ms,
        summary=summary,
        command=command,
        output=output,
    )


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
    started = time.time()
    try:
        data = file_ops.find_files(
            query=query,
            root=root,
            max_results=max_results,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as e:
        _record_direct_tool("file_find", {"query": query, "root": root}, ok=False, started=started, summary=str(e))
        return "ERROR: %s" % e
    _record_direct_tool(
        "file_find",
        {"query": query, "root": root},
        ok=True,
        started=started,
        summary="%d result(s)" % len(data["results"]),
    )
    activity_tracker.record_event(
        "file_find",
        summary="%s result(s) for %s" % (len(data["results"]), data["query"]),
        path=data.get("root", ""),
    )
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
    started = time.time()
    try:
        data = file_ops.read_file(
            path,
            max_bytes=max_bytes,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as e:
        _record_direct_tool("file_read", {"path": path}, ok=False, started=started, summary=str(e))
        return "ERROR: %s" % e
    _record_direct_tool("file_read", {"path": path}, ok=True, started=started, summary="%s bytes" % data.get("bytes", 0))
    activity_tracker.record_event(
        "file_read",
        summary="%s bytes%s" % (
            data.get("bytes", 0),
            " truncated" if data.get("truncated") else "",
        ),
        path=data.get("path", ""),
    )
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
    started = time.time()
    try:
        data = file_ops.write_file(
            path,
            content,
            mode=mode,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as e:
        _record_direct_tool("file_write", {"path": path, "mode": mode}, ok=False, started=started, summary=str(e))
        return "ERROR: %s" % e
    _record_direct_tool("file_write", {"path": path, "mode": mode}, ok=True, started=started, summary=data.get("action", "write"))
    for created_directory in data.get("created_directories", []):
        activity_tracker.record_file_change(
            "create_directory", created_directory, summary="parent created by file_write",
        )
    _record_file_activity("write", data)
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
    started = time.time()
    try:
        data = file_ops.edit_file(
            path,
            old,
            new,
            count=count,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as e:
        _record_direct_tool("file_edit", {"path": path, "count": count}, ok=False, started=started, summary=str(e))
        return "ERROR: %s" % e
    _record_direct_tool("file_edit", {"path": path, "count": count}, ok=True, started=started, summary="%s replacement(s)" % data.get("replacements", 0))
    _record_file_activity("edit", data)
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
    started = time.time()
    try:
        data = file_ops.delete_path(
            path,
            recursive=recursive,
            dry_run=dry_run,
            confirm=confirm,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as e:
        _record_direct_tool("file_delete", {"path": path, "dry_run": dry_run}, ok=False, started=started, summary=str(e))
        return "ERROR: %s" % e
    _record_direct_tool("file_delete", {"path": path, "dry_run": dry_run}, ok=not data.get("dry_run", False), started=started, summary="deleted" if data.get("deleted") else "dry-run")
    _record_file_activity("delete", data)
    return _format_file_result("file delete", data)


def _format_run_result(title: str, data: dict) -> str:
    lines = [
        title,
        "  command: %s" % json.dumps(data.get("command") or [], ensure_ascii=False),
        "  cwd: %s" % data.get("cwd", ""),
        "  ok: %s" % data.get("ok", False),
        "  returncode: %s" % data.get("returncode"),
        "  timed_out: %s" % data.get("timed_out", False),
        "  elapsed_ms: %s" % data.get("elapsed_ms", 0),
    ]
    if data.get("stdout"):
        lines.extend(["stdout:", data["stdout"].rstrip()])
    if data.get("stderr"):
        lines.extend(["stderr:", data["stderr"].rstrip()])
    if data.get("stdout_truncated") or data.get("stderr_truncated"):
        lines.append("  output truncated: true")
    return "\n".join(lines)


@mcp.tool()
def workspace_inventory(
    path: str = ".",
    max_entries: int = 20000,
    timeout_seconds: float = 10.0,
    top_n: int = 15,
    include_hidden: bool = False,
    include_ignored: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Summarize a guarded workspace with explicit traversal budgets."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "path": path, "max_entries": max_entries,
        "timeout_seconds": timeout_seconds, "top_n": top_n,
        "include_hidden": include_hidden, "include_ignored": include_ignored,
    }
    try:
        data = workbench.workspace_inventory(
            path,
            max_entries=max_entries,
            timeout_seconds=timeout_seconds,
            top_n=top_n,
            include_hidden=include_hidden,
            include_ignored=include_ignored,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool(
            "workspace_inventory", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    lines = [
        "workspace inventory: %s" % data["root"],
        "  files: %d | directories: %d | bytes: %d" % (
            data["files"], data["directories"], data["bytes"],
        ),
        "  scanned: %d entries in %dms | skipped: %d" % (
            data["entries_scanned"], data["elapsed_ms"], data["skipped_entries"],
        ),
    ]
    if data["truncated"]:
        lines.append("  truncated: %s" % data["truncation_reason"])
    if data["skipped_by_reason"]:
        lines.append("  skipped reasons: %s" % ", ".join(
            "%s=%s" % item for item in data["skipped_by_reason"].items()
        ))
    if data["manifests"]:
        lines.append("manifests:")
        lines.extend("  %s" % value for value in data["manifests"])
    if data["extensions"]:
        lines.append("top extensions:")
        lines.extend(
            "  %(extension)s  %(files)d file(s)  %(bytes)d bytes" % row
            for row in data["extensions"]
        )
    if data["largest_files"]:
        lines.append("largest files:")
        lines.extend(
            "  %(bytes)d  %(relative)s" % row for row in data["largest_files"]
        )
    if data["top_areas"]:
        lines.append("top areas:")
        lines.extend(
            "  %(bytes)d bytes  %(files)d file(s)  %(path)s" % row
            for row in data["top_areas"]
        )
    output = "\n".join(lines)
    _record_direct_tool(
        "workspace_inventory", args, ok=True, started=started,
        summary="%d files, %d bytes" % (data["files"], data["bytes"]),
        output=output,
    )
    return output


@mcp.tool()
def directory_tree(
    path: str = ".",
    depth: int = 2,
    max_entries: int = 200,
    include_hidden: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """List a bounded guarded folder tree with file sizes."""
    _maybe_live_reload()
    started = time.time()
    args = {"path": path, "depth": depth, "max_entries": max_entries}
    try:
        data = workbench.directory_tree(
            path, depth=depth, max_entries=max_entries,
            include_hidden=include_hidden, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool("directory_tree", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    lines = ["directory tree: %s" % data["root"]]
    for item in data["entries"]:
        indent = "  " * max(1, int(item.get("depth", 1)))
        marker = "[D]" if item["type"] == "dir" else "[F]"
        size = "" if item["type"] == "dir" else " (%s bytes)" % item["bytes"]
        lines.append("%s%s %s%s" % (indent, marker, item["relative"], size))
    if data["truncated"]:
        lines.append("  ... truncated at %d entries" % len(data["entries"]))
    output = "\n".join(lines)
    _record_direct_tool(
        "directory_tree", args, ok=True, started=started,
        summary="%d entries" % len(data["entries"]), output=output,
    )
    return output


@mcp.tool()
def directory_create(
    path: str,
    parents: bool = True,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Create a guarded directory and optional parent directories."""
    _maybe_live_reload()
    started = time.time()
    args = {"path": path, "parents": parents}
    try:
        data = file_ops.make_directory(
            path, parents=parents, exist_ok=True, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
            developer_authorized=_file_developer_allowed(token),
        )
    except Exception as exc:
        _record_direct_tool("directory_create", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_file_result("directory create", data)
    _record_direct_tool(
        "directory_create", args, ok=True, started=started,
        summary=data["action"], output=output,
    )
    if data.get("created"):
        _record_file_activity("create_directory", data)
    return output


@mcp.tool()
def file_read_range(
    path: str,
    start_line: int = 1,
    end_line: int = 200,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Read a bounded 1-based line range from a guarded text file."""
    _maybe_live_reload()
    started = time.time()
    args = {"path": path, "start_line": start_line, "end_line": end_line}
    try:
        data = workbench.read_line_range(
            path, start_line=start_line, end_line=end_line,
            extra_roots=extra_roots, bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool("file_read_range", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    lines = ["file range: %s lines %s-%s" % (data["path"], data["start_line"], data["end_line"])]
    lines.extend("%6d  %s" % (row["line"], row["text"]) for row in data["lines"])
    output = "\n".join(lines)
    _record_direct_tool(
        "file_read_range", args, ok=True, started=started,
        summary="%d lines" % len(data["lines"]), output=output,
    )
    return output


@mcp.tool()
def text_search(
    query: str,
    root: str = ".",
    glob: str = "*",
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
    max_entries: int = 20000,
    timeout_seconds: float = 10.0,
    include_hidden: bool = False,
    include_ignored: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Search text inside guarded workspace files with line evidence."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "query": query, "root": root, "glob": glob, "regex": regex,
        "max_entries": max_entries, "timeout_seconds": timeout_seconds,
    }
    try:
        data = workbench.text_search(
            query, root=root, glob=glob, regex=regex,
            case_sensitive=case_sensitive, max_results=max_results,
            max_entries=max_entries, timeout_seconds=timeout_seconds,
            include_hidden=include_hidden, include_ignored=include_ignored,
            extra_roots=extra_roots, bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool("text_search", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    lines = [
        "text search: %r under %s (%d files scanned)" %
        (data["query"], data["root"], data["files_scanned"]),
    ]
    lines.extend(
        "  %(relative)s:%(line)s:%(column)s: %(text)s" % row
        for row in data["matches"]
    )
    if not data["matches"]:
        lines.append("  (no matches)")
    if data["truncated"]:
        lines.append("  ... truncated: %s" % (data.get("truncation_reason") or "limit"))
    output = "\n".join(lines)
    _record_direct_tool(
        "text_search", args, ok=True, started=started,
        summary="%d matches" % len(data["matches"]), output=output,
    )
    return output


@mcp.tool()
def script_search(
    query: str = "*",
    root: str = ".",
    max_results: int = 100,
    max_entries: int = 20000,
    timeout_seconds: float = 10.0,
    include_hidden: bool = False,
    include_ignored: bool = False,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Find runnable scripts under guarded roots and identify their runner."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "query": query, "root": root, "max_entries": max_entries,
        "timeout_seconds": timeout_seconds,
    }
    try:
        data = workbench.script_search(
            query, root=root, max_results=max_results, extra_roots=extra_roots,
            max_entries=max_entries, timeout_seconds=timeout_seconds,
            include_hidden=include_hidden, include_ignored=include_ignored,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool("script_search", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    lines = ["script search: %s under %s" % (data["query"], data["root"])]
    lines.extend("  %(runner)s  %(relative)s" % row for row in data["results"])
    if not data["results"]:
        lines.append("  (no scripts found)")
    if data["truncated"]:
        lines.append("  ... truncated: %s" % (data.get("truncation_reason") or "limit"))
    output = "\n".join(lines)
    _record_direct_tool(
        "script_search", args, ok=True, started=started,
        summary="%d scripts" % len(data["results"]), output=output,
    )
    return output


@mcp.tool()
def program_search(query: str = "*", max_results: int = 100) -> str:
    """Search PATH and Windows App Paths for installed programs."""
    _maybe_live_reload()
    started = time.time()
    args = {"query": query, "max_results": max_results}
    try:
        data = workbench.program_search(query, max_results=max_results)
    except Exception as exc:
        _record_direct_tool("program_search", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    lines = ["program search: %s" % data["query"]]
    lines.extend("  %(name)s  [%(source)s]  %(path)s" % row for row in data["results"])
    if not data["results"]:
        lines.append("  (no programs found)")
    output = "\n".join(lines)
    _record_direct_tool(
        "program_search", args, ok=True, started=started,
        summary="%d programs" % len(data["results"]), output=output,
    )
    return output


@mcp.tool()
def workspace_run(
    program: str,
    args_json: str = "[]",
    cwd: str = ".",
    stdin: str = "",
    timeout: int = 30,
    max_output: int = 128000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Run a program as a bounded argv list; no shell command strings."""
    _maybe_live_reload()
    started = time.time()
    args = {"program": program, "args_json": args_json, "cwd": cwd, "timeout": timeout}
    try:
        data = workbench.run_program(
            program, args_json=args_json, cwd=cwd, stdin=stdin,
            timeout=timeout, max_output=max_output, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool("workspace_run", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("workspace run", data)
    _record_direct_tool(
        "workspace_run", args, ok=data["ok"], started=started,
        summary="exit %s" % data.get("returncode"),
        command=data["command"], output=output,
    )
    return output


@mcp.tool()
def script_run(
    path: str,
    args_json: str = "[]",
    cwd: str = "",
    stdin: str = "",
    timeout: int = 30,
    max_output: int = 128000,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Run a guarded script with its known interpreter and bounded output."""
    _maybe_live_reload()
    started = time.time()
    args = {"path": path, "args_json": args_json, "cwd": cwd, "timeout": timeout}
    try:
        data = workbench.run_script(
            path, args_json=args_json, cwd=cwd, stdin=stdin, timeout=timeout,
            max_output=max_output, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool("script_run", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_run_result("script run", data)
    _record_direct_tool(
        "script_run", args, ok=data["ok"], started=started,
        summary="exit %s" % data.get("returncode"),
        command=data["command"], output=output,
    )
    return output


@mcp.tool()
def image_inspect(
    path: str,
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Inspect guarded image headers, dimensions, size, and hash."""
    _maybe_live_reload()
    started = time.time()
    args = {"path": path}
    try:
        data = workbench.image_inspect(
            path, extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
    except Exception as exc:
        _record_direct_tool("image_inspect", args, ok=False, started=started, summary=str(exc))
        return "ERROR: %s" % exc
    output = _format_file_result("image inspection", data)
    _record_direct_tool(
        "image_inspect", args, ok=True, started=started,
        summary="%s %sx%s" % (data["format"], data.get("width"), data.get("height")),
        output=output,
    )
    return output


@mcp.tool()
def sonder_sessions(limit: int = 20) -> str:
    """List sonder conversation threads, most recently used first.

    Each line shows the session id (pass it as `session` to sonder to resume),
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
    lines = ["sonder sessions (most recent first):"]
    for s in sessions:
        lines.append("  %s  [%d turns]  %s  (updated %s)" % (
            s["session_id"], s["turn_count"], s.get("title") or "(untitled)",
            s.get("updated_ts") or "?",
        ))
    return "\n".join(lines)


@mcp.tool()
def sonder_remember_fact(text: str, project: str = "") -> str:
    """Store a durable fact sonder should ALWAYS know for a project.

    Unlike lessons (earned from good outcomes), facts are asserted directly and are
    injected into every sonder call for that project — a mini project brief the
    model carries itself (toolchain, conventions, key paths, gotchas). No `project`
    stores it under the "default" project. Use sonder(..., project="<name>") to
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

    This gives Claude/Codex a Claude-like execution tool through the sonder-runtime MCP
    server. Supported languages: python, javascript/js/node, powershell/ps1,
    cpp/c++, and csharp/cs. Code runs on this machine with the same permissions as the MCP server, so treat it
    like a local terminal: use it for small checks, experiments, and diagnostics,
    not for untrusted code. Execution is bounded by a timeout (1-60s), output is
    trimmed, and cwd is confined to this project workspace.
    """
    _maybe_live_reload()
    started = time.time()
    ok = False
    try:
        result = code_runner.run_code(
            code=code,
            language=language,
            stdin=stdin,
            timeout=timeout,
            cwd=cwd or None,
        )
        ok = bool(result.get("ok")) if isinstance(result, dict) else True
    except ValueError as e:
        _record_direct_tool(
            "run_code",
            {"language": language, "timeout": timeout},
            ok=False, started=started,
            summary=str(e),
        )
        return "ERROR: %s" % e
    output = code_runner.format_result(result)
    _record_direct_tool(
        "run_code",
        {"language": language, "timeout": timeout},
        ok=ok, started=started,
        summary=("ok" if ok else "failed"),
        output=output,
    )
    return output


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
    started = time.time()
    ok = False
    try:
        result = code_runner.run_project(
            files_json=files_json,
            commands_json=commands_json,
            stdin=stdin,
            timeout=timeout,
        )
        ok = bool(result.get("ok")) if isinstance(result, dict) else True
    except ValueError as e:
        _record_direct_tool(
            "run_project",
            {"timeout": timeout},
            ok=False, started=started,
            summary=str(e),
        )
        return "ERROR: %s" % e
    output = code_runner.format_project_result(result)
    _record_direct_tool(
        "run_project",
        {"timeout": timeout},
        ok=ok, started=started,
        summary=("ok" if ok else "failed"),
        output=output,
    )
    return output


@mcp.tool()
def artifact_generate(
    name: str,
    brief: str,
    kinds: str = "auto",
    dimension: str = "auto",
    theme: str = "auto",
    seed: int | None = None,
    output_dir: str = "",
) -> str:
    """Generate a deterministic in-house artifact set from a free-form brief.

    Supported outputs include raster images, SVG vectors/diagrams, palettes,
    Markdown and editable DOCX briefs, JSON/CSV data, editable XLSX workbooks,
    HTML mockups, editable PPTX decks, animated GIFs, synchronized AVI video,
    WAV and MIDI music, SRT and WebVTT captions, EDL timelines, OBJ/MTL models,
    self-contained textured humanoid GLBs with full morph frames and sequenced
    clips, and JSON scenes. ``kinds`` may be auto, all, or a comma-separated
    subset. No external model, service, package, or downloaded asset is required.
    """
    _maybe_live_reload()
    started = time.time()
    try:
        result = assetgen.generate_artifacts(
            name=name,
            brief=brief,
            kinds=kinds,
            dimension=dimension,
            theme=theme,
            seed=seed,
            output_dir=output_dir,
        )
    except (OSError, ValueError) as exc:
        _record_direct_tool(
            "artifact_generate", {"name": name, "kinds": kinds}, ok=False,
            started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    activity_tracker.record_file_change(
        "create", result["root"], bytes_written=result.get("total_bytes", 0),
        summary="generated artifact pack",
    )
    output = assetgen.format_pack(result)
    _record_direct_tool(
        "artifact_generate", {"name": name, "kinds": kinds}, ok=True,
        started=started,
        summary="%d files" % len(result.get("files", [])),
        output=output,
    )
    return output


@mcp.tool()
def artifact_verify(path: str) -> str:
    """Verify every generated file against its manifest and format contract."""
    _maybe_live_reload()
    try:
        result = assetgen.verify_pack(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "ERROR: %s" % exc
    lines = [
        "artifact verification: %s" % ("PASS" if result["ok"] else "FAIL"),
        "  checked: %d" % result["checked"],
        "  deterministic checks: %d passed, %d failed"
        % (
            result["grounding"].get("passed_checks", 0),
            result["grounding"].get("failed_checks", 0),
        ),
        "  root: %s" % result["root"],
    ]
    lines.extend("  - %s" % failure for failure in result["failures"])
    return "\n".join(lines)


@mcp.tool()
def game_reference_suite(
    name: str = "sonder-reference",
    theme: str = "arcane",
    seed: int = 1337,
    max_workers: int = 2,
    timeout: int = 30,
) -> str:
    """Build and run known-good 2D/2.5D/3D games across four languages.

    The persistent projects use Python, JavaScript, C++, and C# standard
    libraries only. Each consumes generated assets, simulates bounded gameplay,
    writes a software-rendered PPM frame, prints GAME_OK, and exits.
    """
    _maybe_live_reload()
    try:
        result = game_forge.run_reference_suite(
            name=name, theme=theme, seed=seed,
            max_workers=max_workers, timeout=timeout,
        )
    except (OSError, ValueError) as exc:
        return "ERROR: %s" % exc
    return game_forge.format_suite(result)


def _resolve_repair_rounds(repair_rounds, language) -> int:
    """Clamp explicit repair_rounds to [0, 2]; None picks a language default.

    C++ candidates default to 2 repair rounds (header/toolchain issues usually
    take more than one grounded retry); every other language defaults to 1."""
    if repair_rounds is None:
        try:
            normalized = game_forge.normalize_language(language)
        except ValueError:
            normalized = ""
        return 2 if normalized == "cpp" else 1
    return max(0, min(int(repair_rounds), 2))


def _game_generate_result(
    name: str,
    concept: str,
    language: str,
    dimension: str,
    theme: str,
    seed: int,
    tier: str,
    timeout: int,
    repair_rounds: int | None,
    use_reference_fallback: bool = True,
) -> dict:
    repair_rounds = _resolve_repair_rounds(repair_rounds, language)
    project = game_forge.prepare_project(name, language, dimension, theme, seed)
    base_prompt = game_forge.generation_prompt(project, concept)
    try:
        baseline = game_forge.reference_source(project["language"], project["dimension"])
    except ValueError:
        baseline = ""
    if baseline:
        base_prompt += (
            "\n\nUse this complete, tested standard-library program as your starting scaffold. "
            "Preserve its asset validation, bounded execution, frame writer, and GAME_OK "
            "contract while adapting mechanics and visuals to the requested concept. Do not "
            "add any third-party import, package, or engine.\n```%s\n%s\n```"
            % (project["language"], baseline.rstrip())
        )
    attempts = []
    repair_note = ""
    final_iid = None
    for attempt in range(repair_rounds + 1):
        prompt = base_prompt
        if repair_note:
            prompt += (
                "\n\nThe previous candidate failed this exact grounded check:\n%s\n"
                "Return a corrected complete program." % repair_note[:1800]
            )
        response = sonder(
            prompt,
            tier=tier,
            session="none",
            temperature=0.32 if attempt == 0 else 0.15,
            num_predict=1800,
        )
        iid = parse_interaction_id(response)
        final_iid = iid or final_iid
        code = grounding.extract_code_block(response, project["language"])
        if not code:
            run = {"ok": False, "output": "no %s code block returned" % project["language"]}
        else:
            code = game_forge.autofix_standard_library(code, project["language"])
            forbidden = game_forge.validate_in_house(code, project["language"])
            if forbidden:
                # Actionable remediation (which tokens, and HOW to replace
                # them with standard-library equivalents) so the repair round
                # actually converges instead of re-tripping the same token.
                run = {
                    "ok": False,
                    "output": game_forge.forbidden_remediation(
                        forbidden, project["language"],
                    ),
                }
            else:
                contract = game_forge.contract_issues(code, project["language"])
                if contract:
                    run = {
                        "ok": False,
                        "output": "game contract violation(s): %s" % "; ".join(contract),
                    }
                else:
                    run = game_forge.run_project(project, code, timeout)
        attempts.append({
            "attempt": attempt + 1,
            "ok": bool(run.get("ok")),
            "output": run.get("output", ""),
            "iid": iid,
            "source": run.get("source", project["source"]),
            "frame": run.get("frame", project["frame"]),
        })
        if run.get("ok"):
            if iid:
                attempts[-1]["record"] = record_outcome(iid, "tests_passed")
            break
        repair_note = run.get("output") or "unknown game verification failure"
    model_ok = bool(attempts and attempts[-1]["ok"])
    if attempts and not model_ok and final_iid:
        attempts[-1]["record"] = record_outcome(final_iid, "failed")
    fallback_used = False
    if not model_ok and use_reference_fallback:
        try:
            fallback_code = game_forge.reference_source(
                project["language"], project["dimension"],
            )
            fallback_run = game_forge.run_project(project, fallback_code, timeout)
            fallback_used = True
            attempts.append({
                "attempt": len(attempts) + 1,
                "kind": "verified-reference-fallback",
                "ok": bool(fallback_run.get("ok")),
                "output": fallback_run.get("output", ""),
                "iid": None,
                "source": fallback_run.get("source", project["source"]),
                "frame": fallback_run.get("frame", project["frame"]),
            })
        except (OSError, ValueError) as exc:
            attempts.append({
                "attempt": len(attempts) + 1,
                "kind": "verified-reference-fallback",
                "ok": False,
                "output": "reference fallback unavailable: %s" % exc,
                "iid": None,
            })
    return {
        "ok": bool(attempts and attempts[-1]["ok"]),
        "model_ok": model_ok,
        "fallback_used": fallback_used,
        "name": name,
        "language": project["language"],
        "dimension": project["dimension"],
        "root": project["root"],
        "attempts": attempts,
    }


@mcp.tool()
def game_generate_and_test(
    name: str,
    concept: str,
    language: str = "python",
    dimension: str = "2d",
    theme: str = "arcane",
    seed: int = 1337,
    tier: str = "code",
    timeout: int = 30,
    repair_rounds: int | None = None,
    use_reference_fallback: bool = True,
) -> str:
    """Have Sonder create, execute, repair, and ground a persistent game.

    Generated games must use only standard-library/OS-native APIs, consume an
    in-house artifact pack, render frame.ppm, emit GAME_OK, and terminate within
    the bounded timeout. Passing/failed outcomes are recorded into learning.
    repair_rounds=None picks a language default: 2 for C++, 1 otherwise.
    """
    _maybe_live_reload()
    started = time.time()
    try:
        result = _game_generate_result(
            name, concept, language, dimension, theme, seed, tier,
            max(2, min(int(timeout), 60)), repair_rounds,
            use_reference_fallback=use_reference_fallback,
        )
    except (OSError, ValueError) as exc:
        _record_direct_tool(
            "game_generate_and_test",
            {"name": name, "language": language, "dimension": dimension},
            ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    lines = [
        "generated game: %s" % ("PASS" if result["ok"] else "FAIL"),
        "  target: %s / %s" % (result["language"], result["dimension"]),
        "  attempts: %d" % len(result["attempts"]),
        "  model result: %s | reference fallback: %s" % (
            "PASS" if result.get("model_ok") else "FAIL",
            "used" if result.get("fallback_used") else "not used",
        ),
        "  root: %s" % result["root"],
    ]
    for attempt in result["attempts"]:
        lines.append("  [%s] attempt %d (%s) iid=%s" % (
            "PASS" if attempt["ok"] else "FAIL",
            attempt["attempt"], attempt.get("kind", "model"), attempt.get("iid") or "-",
        ))
        if attempt.get("output"):
            lines.append(str(attempt["output"])[:1200])
        if attempt.get("record"):
            lines.append(str(attempt["record"])[:500])
    output = "\n".join(lines)
    if result.get("root"):
        activity_tracker.record_file_change(
            "create", result["root"], summary="generated persistent game project",
        )
    _record_direct_tool(
        "game_generate_and_test",
        {"name": name, "language": language, "dimension": dimension},
        ok=bool(result.get("ok")), started=started,
        summary="runnable" if result.get("ok") else "verification failed",
        output=output,
    )
    return output


@mcp.tool()
def game_generation_campaign(
    name: str,
    concept: str = "compact action game with one complete gameplay loop",
    total: int = 6,
    theme: str = "arcane",
    tier: str = "code",
    max_workers: int = 2,
    timeout: int = 30,
    repair_rounds: int | None = None,
    use_reference_fallback: bool = True,
    language: str = "",
    dimension: str = "",
) -> str:
    """Run a bounded parallel game campaign with optional target constraints.

    By default jobs rotate across Python, JavaScript, C++, and C# plus 2D,
    2.5D, and 3D. An explicit language and/or dimension constrains the matrix.
    Every candidate receives its own artifact pack, is compiled/executed, must
    emit GAME_OK and a valid frame.ppm, and records a grounded outcome.
    """
    _maybe_live_reload()
    # Repeat the fully verified reference matrix. This keeps every default fleet
    # job recoverable if a model draft fails while still covering all supported
    # languages and 2D, isometric 2.5D, and 3D execution.
    try:
        target_language = (
            game_forge.normalize_language(language) if str(language).strip() else ""
        )
        target_dimension = (
            game_forge.normalize_dimension(dimension) if str(dimension).strip() else ""
        )
    except ValueError as exc:
        return "ERROR: %s" % exc
    language_order = tuple(dict.fromkeys(
        item_language for item_language, _ in game_forge.DEFAULT_MATRIX
    ))
    if target_language and target_dimension:
        matrix = ((target_language, target_dimension),)
    elif target_language:
        matrix = tuple(
            (target_language, item_dimension)
            for item_dimension in ("2d", "2.5d", "3d")
        )
    elif target_dimension:
        matrix = tuple(
            (item_language, target_dimension) for item_language in language_order
        )
    else:
        matrix = game_forge.DEFAULT_MATRIX
    total = max(1, min(int(total or 1), 12))
    workers = max(1, min(int(max_workers or 1), 4, total))
    timeout = max(2, min(int(timeout or 30), 60))
    results = [None] * total
    response_id = activity_tracker.current_response_id()

    def one(index):
        with activity_tracker.bind_response(response_id):
            job_language, job_dimension = matrix[index % len(matrix)]
            suffix = "iso" if job_dimension == "2.5d" else job_dimension
            project_name = "%s-%s-%s-%d" % (
                assetgen._safe_slug(name), job_language, suffix, index + 1,
            )
            try:
                return _game_generate_result(
                    project_name, concept, job_language, job_dimension, theme,
                    1337 + index, tier, timeout, repair_rounds,
                    use_reference_fallback=use_reference_fallback,
                )
            except Exception as exc:
                return {
                    "ok": False, "name": project_name, "language": job_language,
                    "dimension": job_dimension, "root": "", "attempts": [
                        {"attempt": 1, "ok": False, "output": "ERROR: %s" % exc, "iid": None}
                    ],
                }

    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, index): index for index in range(total)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    elapsed = round(time.time() - started, 3)
    passed = sum(1 for result in results if result and result.get("ok"))
    model_passed = sum(1 for result in results if result and result.get("model_ok"))
    fallback_passed = sum(
        1 for result in results
        if result and result.get("ok") and result.get("fallback_used")
    )
    lines = [
        "greenfield game campaign: %d/%d runnable in %.3fs "
        "(model=%d, reference-fallback=%d, workers=%d, target=%s/%s)" % (
            passed, total, elapsed, model_passed, fallback_passed, workers,
            target_language or "mixed", target_dimension or "mixed",
        ),
    ]
    for result in results:
        final = result["attempts"][-1]
        lines.append("[%s] %s/%s model=%s fallback=%s attempts=%d root=%s" % (
            "PASS" if result.get("ok") else "FAIL",
            result.get("language"), result.get("dimension"),
            "PASS" if result.get("model_ok") else "FAIL",
            "yes" if result.get("fallback_used") else "no",
            len(result.get("attempts") or []), result.get("root") or "-",
        ))
        if final.get("output"):
            lines.append(str(final["output"])[:900])
        if final.get("record"):
            lines.append(str(final["record"])[:400])
    output = "\n".join(lines)
    for result in results:
        if result and result.get("root"):
            activity_tracker.record_file_change(
                "create", result["root"],
                summary="generated campaign game project",
            )
    _record_direct_tool(
        "game_generation_campaign",
        {
            "name": name, "total": total, "workers": workers,
            "language": target_language, "dimension": target_dimension,
        },
        ok=passed == total, started=started,
        summary="%d/%d runnable" % (passed, total), output=output,
    )
    return output


def _loop_text_result(action_type, text):
    text = text or ""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return {
        "ok": not text.startswith("ERROR:"),
        "type": action_type,
        "summary": first_line[:200],
        "output": text,
    }


def _loop_verdict_result(action_type, text, success_prefix):
    result = _loop_text_result(action_type, text)
    result["ok"] = bool(text) and text.startswith(success_prefix)
    return result


def _loop_dispatch(action):
    action_type = (action.get("type") or action.get("action") or "code").strip().lower()
    activity_tracker.record_tool_call(
        "loop:%s" % action_type,
        {k: v for k, v in (action or {}).items() if k not in {"code", "content", "files"}},
        summary="loop action queued",
    )
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
    if action_type in ("artifact", "artifact_generate", "assetgen"):
        return _loop_text_result("artifact_generate", artifact_generate(
            name=action.get("name", "generated-artifact"),
            brief=action.get("brief", action.get("prompt", "")),
            kinds=action.get("kinds", "auto"),
            dimension=action.get("dimension", "auto"),
            theme=action.get("theme", "auto"),
            seed=action.get("seed"),
            output_dir=action.get("output_dir", ""),
        ))
    if action_type in ("artifact_ground", "artifact_check"):
        return _loop_verdict_result("artifact_ground", artifact_ground(
            path=action.get("path", ""),
            recipe=action.get("recipe", "auto"),
            requirements_json=action.get("requirements", action.get("requirements_json", "")),
        ), "artifact grounding: PASS")
    if action_type in ("game_reference", "game_reference_suite"):
        return _loop_text_result("game_reference_suite", game_reference_suite(
            name=action.get("name", "sonder-reference"),
            theme=action.get("theme", "arcane"),
            seed=action.get("seed", 1337),
            max_workers=action.get("max_workers", 2),
            timeout=action.get("timeout", 30),
        ))
    if action_type in ("game", "game_generate", "game_generate_and_test"):
        return _loop_text_result("game_generate_and_test", game_generate_and_test(
            name=action.get("name", "generated-game"),
            concept=action.get("concept", action.get("prompt", "")),
            language=action.get("language", "python"),
            dimension=action.get("dimension", "2d"),
            theme=action.get("theme", "arcane"),
            seed=action.get("seed", 1337),
            tier=action.get("tier", "code"),
            timeout=action.get("timeout", 30),
            repair_rounds=action.get("repair_rounds"),
        ))
    if action_type in ("game_campaign", "game_generation_campaign"):
        return _loop_text_result("game_generation_campaign", game_generation_campaign(
            name=action.get("name", "game-fleet"),
            concept=action.get("concept", action.get("prompt", "compact action game")),
            total=action.get("total", 6),
            language=action.get("language", ""),
            dimension=action.get("dimension", ""),
            theme=action.get("theme", "arcane"),
            tier=action.get("tier", "code"),
            max_workers=action.get("max_workers", 2),
            timeout=action.get("timeout", 30),
            repair_rounds=action.get("repair_rounds"),
        ))
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
    if action_type == "sonder":
        return _loop_text_result("sonder", sonder(
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
    if action_type == "learning_health":
        return _loop_text_result("learning_health", learning_health_status())
    if action_type == "memory_quality_report":
        return _loop_text_result("memory_quality_report", memory_quality_report(
            sample_limit=action.get("sample_limit", 5),
        ))
    if action_type == "memory_quality_repair":
        return _loop_text_result("memory_quality_repair", memory_quality_repair(
            apply=action.get("apply", False),
        ))
    if action_type == "memory_privacy_review":
        return _loop_text_result("memory_privacy_review", memory_privacy_review(
            sample_limit=action.get("sample_limit", 20),
        ))
    if action_type == "memory_privacy_repair":
        return _loop_text_result("memory_privacy_repair", memory_privacy_repair(
            lesson_ids_json=action.get("lesson_ids", action.get("lesson_ids_json", [])),
            apply=action.get("apply", False),
        ))
    if action_type == "memory_embedding_backfill":
        return _loop_text_result("memory_embedding_backfill", memory_embedding_backfill(
            limit=action.get("limit", 25), apply=action.get("apply", False),
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
    if action_type in ("master_capacity", "agent_capacity"):
        return _loop_text_result("master_capacity", master_capacity(
            requested_agents=action.get("requested_agents", action.get("agents", 0)),
        ))
    if action_type in ("master_cancel", "agent_cancel"):
        return _loop_text_result("master_cancel", master_cancel(
            agent_id=action.get("agent_id", action.get("selector", "")),
        ))
    if action_type in ("master_retry", "agent_retry"):
        return _loop_text_result("master_retry", master_retry(
            agent_id=action.get("agent_id", action.get("selector", "")),
            tier=action.get("tier", ""),
        ))
    if action_type in ("master", "master_orchestrate"):
        return _loop_text_result("master_orchestrate", master_orchestrate(
            task=action.get("task", action.get("prompt", "")),
            mode=action.get("mode", "ask"),
            agents=action.get("agents", 3),
            tier=action.get("tier", "auto"),
            learn=action.get("learn", False),
        ))
    if action_type in ("work", "agent", "workbench_agent"):
        return _loop_text_result("workbench_agent", workbench_agent(
            prompt=action.get("task", action.get("prompt", "")),
            tier=action.get("tier", "auto"),
            max_steps=action.get("max_steps", 12),
            allow_web=action.get("allow_web", True),
            project=action.get("project", ""),
            allow_location=action.get("allow_location", False),
        ))
    if action_type == "workspace_inventory":
        return _loop_text_result("workspace_inventory", workspace_inventory(
            path=action.get("path", action.get("root", ".")),
            max_entries=action.get("max_entries", 20000),
            timeout_seconds=action.get("timeout_seconds", 10.0),
            top_n=action.get("top_n", 15),
            include_hidden=action.get("include_hidden", False),
            include_ignored=action.get("include_ignored", False),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "directory_tree":
        return _loop_text_result("directory_tree", directory_tree(
            path=action.get("path", "."),
            depth=action.get("depth", 2),
            max_entries=action.get("max_entries", 200),
            include_hidden=action.get("include_hidden", False),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "directory_create":
        return _loop_text_result("directory_create", directory_create(
            path=action.get("path", ""),
            parents=action.get("parents", True),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "file_read_range":
        return _loop_text_result("file_read_range", file_read_range(
            path=action.get("path", ""),
            start_line=action.get("start_line", 1),
            end_line=action.get("end_line", 200),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "text_search":
        return _loop_text_result("text_search", text_search(
            query=action.get("query", ""),
            root=action.get("root", "."),
            glob=action.get("glob", "*"),
            regex=action.get("regex", False),
            case_sensitive=action.get("case_sensitive", False),
            max_results=action.get("max_results", 100),
            max_entries=action.get("max_entries", 20000),
            timeout_seconds=action.get("timeout_seconds", 10.0),
            include_hidden=action.get("include_hidden", False),
            include_ignored=action.get("include_ignored", False),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "script_search":
        return _loop_text_result("script_search", script_search(
            query=action.get("query", "*"),
            root=action.get("root", "."),
            max_results=action.get("max_results", 100),
            max_entries=action.get("max_entries", 20000),
            timeout_seconds=action.get("timeout_seconds", 10.0),
            include_hidden=action.get("include_hidden", False),
            include_ignored=action.get("include_ignored", False),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "program_search":
        return _loop_text_result("program_search", program_search(
            query=action.get("query", "*"),
            max_results=action.get("max_results", 100),
        ))
    if action_type == "workspace_run":
        return _loop_text_result("workspace_run", workspace_run(
            program=action.get("program", ""),
            args_json=action.get("args", action.get("args_json", [])),
            cwd=action.get("cwd", "."),
            stdin=action.get("stdin", ""),
            timeout=action.get("timeout", 30),
            max_output=action.get("max_output", 128000),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "script_run":
        return _loop_text_result("script_run", script_run(
            path=action.get("path", ""),
            args_json=action.get("args", action.get("args_json", [])),
            cwd=action.get("cwd", ""),
            stdin=action.get("stdin", ""),
            timeout=action.get("timeout", 30),
            max_output=action.get("max_output", 128000),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "image_inspect":
        return _loop_text_result("image_inspect", image_inspect(
            path=action.get("path", ""),
            token=action.get("token", ""),
            approval=action.get("approval", ""),
            extra_roots=action.get("extra_roots", ""),
        ))
    if action_type == "checklist_create":
        items = action.get("items", action.get("items_json", []))
        return _loop_text_result("checklist_create", checklist_create(
            title=action.get("title", "Workflow checklist"),
            items_json=items if isinstance(items, str) else json.dumps(items),
            project=action.get("project", ""),
            owner=action.get("owner", "workflow"),
            priority=action.get("priority", 1),
        ))
    if action_type == "checklist_update":
        return _loop_text_result("checklist_update", checklist_update(
            checklist_id=action.get("checklist_id", action.get("id", "")),
            item=str(action.get("item", "")),
            status=action.get("status", "in_progress"),
            note=action.get("note", ""),
        ))
    if action_type == "checklist_show":
        return _loop_text_result("checklist_show", checklist_show(
            checklist_id=action.get("checklist_id", action.get("id", "")),
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
    if action_type == "emotion_update":
        payload = action.get("vectors", action.get("vectors_json", {}))
        return _loop_text_result("emotion_update", update_emotion_vectors(
            vectors_json=payload if isinstance(payload, str) else json.dumps(payload),
            mode=action.get("mode", "merge"),
        ))
    if action_type == "emotion_tune":
        return _loop_text_result("emotion_tune", tune_emotion_vectors(
            feedback_text=action.get("feedback_text", action.get("text", "")),
            step=action.get("step", 0.1),
        ))
    if action_type == "learn_preference":
        return _loop_text_result("learn_preference", learn_preference(
            text=action.get("text", ""),
            scope=action.get("scope", "global"),
        ))
    if action_type == "preferences_status":
        return _loop_text_result("preferences_status", preferences_status(
            include_disabled=action.get("include_disabled", False),
            limit=action.get("limit", 50),
        ))
    if action_type == "memory_search":
        return _loop_text_result("memory_search", memory_search(
            query=action.get("query", ""),
            limit=action.get("limit", 10),
        ))
    if action_type == "ground_artifact":
        return _loop_verdict_result("ground_artifact", ground_artifact(
            artifact=action.get("artifact", ""),
            checks_json=json.dumps(action.get("checks", [])),
        ), "grounding: passed")
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
    if action_type == "weather_lookup":
        return _loop_text_result("weather_lookup", weather_lookup(
            location=action.get("location", ""),
            forecast_days=action.get("forecast_days", 3),
            units=action.get("units", "auto"),
        ))
    if action_type == "approximate_location_lookup":
        return _loop_text_result(
            "approximate_location_lookup",
            approximate_location_lookup(consent=action.get("consent") is True),
        )
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
        "output": "Valid action types: code, project, artifact_generate, artifact_ground, game_reference_suite, game_generate_and_test, game_generation_campaign, offload, sonder, master_orchestrate, master_status, master_capacity, master_cancel, master_retry, file_policy, workspace_inventory, directory_tree, text_search, script_search, program_search, workspace_run, script_run, image_inspect, file_find, file_read, file_write, file_edit, file_delete, status, diagnostics, context_health, learning_health, memory_quality_report, memory_quality_repair, memory_privacy_review, memory_privacy_repair, memory_embedding_backfill, improvement_report, self_heal_check, self_heal_repair, profile_status, emotion_status, emotion_update, emotion_tune, learn_preference, preferences_status, memory_search, ground_artifact, apply_learned, web_search, web_fetch, weather_lookup, approximate_location_lookup, unload, sleep.",
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
      - {"type":"artifact_generate","name":"brand-kit","brief":"fiery logo, music, and 3D mascot","kinds":"auto"}
      - {"type":"game_reference_suite","name":"reference-suite"}
      - {"type":"game_generate_and_test","name":"arena","concept":"isometric action RPG","language":"cpp","dimension":"2.5d"}
      - {"type":"game_generation_campaign","name":"game-fleet","concept":"action roguelite","total":6,"language":"cpp","dimension":"2.5d","max_workers":2}
      - {"type":"offload","prompt":"...","tier":"fast|code|general|cloud-code|cloud-general"}
      - {"type":"sonder","prompt":"...","session":"none"}
      - {"type":"sonder","prompt":"...","context_size":"1m"}
      - {"type":"master_orchestrate","task":"...","mode":"inline|delegate|fleet","agents":3}
      - {"type":"master_status"}
      - {"type":"master_capacity","requested_agents":32}
      - {"type":"master_cancel","agent_id":"master-id|prefix|all"}
      - {"type":"master_retry","agent_id":"master-id|prefix","tier":"code"}
      - {"type":"workspace_inventory","path":".","max_entries":20000,"timeout_seconds":10}
      - {"type":"file_find","query":"*.py","root":"."}
      - {"type":"file_read","path":"README.md"}
      - {"type":"file_write","path":"notes.txt","content":"...","mode":"create|overwrite|append"}
      - {"type":"file_edit","path":"notes.txt","old":"before","new":"after"}
      - {"type":"file_delete","path":"notes.txt","dry_run":true}
      - {"type":"web_search","query":"...","limit":5}
      - {"type":"web_fetch","url":"https://...","max_chars":8000}
      - {"type":"weather_lookup","location":"Chicago, IL","forecast_days":3}
      - {"type":"approximate_location_lookup","consent":true}
      - {"type":"memory_search","query":"..."}
      - {"type":"memory_privacy_review","sample_limit":20}
      - {"type":"memory_embedding_backfill","limit":25,"apply":false}
      - {"type":"emotion_update","vectors":{"warmth":0.5,"brevity":0.2}}
      - {"type":"emotion_tune","text":"be warmer but more concise"}
      - {"type":"learn_preference","text":"User prefers concise status updates."}
      - {"type":"preferences_status"}
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

    Uses a stdlib HTML parser against SONDER_SEARCH_URL (default:
    DuckDuckGo HTML). Disable with SONDER_WEB_TOOLS=0.
    """
    _maybe_live_reload()
    started = time.time()
    try:
        results = web_tools.web_search(query, limit=limit)
    except Exception as e:
        _record_direct_tool(
            "web_search",
            {"query": query, "limit": limit},
            ok=False, started=started,
            summary=str(e),
        )
        return "ERROR: %s" % e
    output = web_tools.format_search_results(results)
    _record_direct_tool(
        "web_search",
        {"query": query, "limit": limit},
        ok=True, started=started,
        summary="%d result(s)" % len(results),
        output=output,
    )
    return output


@mcp.tool()
def web_fetch(url: str, max_chars: int = 8000) -> str:
    """Fetch a public HTTP/HTTPS URL as readable text.

    Blocks localhost/private-network literal IPs and trims output. Disable with
    SONDER_WEB_TOOLS=0.
    """
    _maybe_live_reload()
    started = time.time()
    try:
        out = web_tools.web_fetch(url, max_chars=max_chars)
    except Exception as e:
        _record_direct_tool(
            "web_fetch",
            {"url": url, "max_chars": max_chars},
            ok=False, started=started,
            summary=str(e),
        )
        return "ERROR: %s" % e
    _record_direct_tool(
        "web_fetch",
        {"url": url, "max_chars": max_chars},
        ok=not out.startswith("ERROR:"), started=started,
        summary="%d chars" % len(out),
        output=out,
    )
    return out


@mcp.tool()
def weather_lookup(
    location: str,
    forecast_days: int = 3,
    units: str = "auto",
) -> str:
    """Get current conditions and a short forecast for a city or postal code."""
    _maybe_live_reload()
    started = time.time()
    args = {
        "location": location, "forecast_days": forecast_days, "units": units,
    }
    try:
        result = web_tools.weather_lookup(
            location, forecast_days=forecast_days, units=units,
        )
        output = web_tools.format_weather(result)
    except Exception as exc:
        _record_direct_tool(
            "weather_lookup", args, ok=False, started=started, summary=str(exc),
        )
        return "ERROR: %s" % exc
    _record_direct_tool(
        "weather_lookup", args, ok=True, started=started,
        summary="forecast for %s" % result.get("query", location), output=output,
    )
    return output


@mcp.tool()
def approximate_location_lookup(consent: bool = False) -> str:
    """Resolve this machine's public IP to a place after explicit consent."""
    _maybe_live_reload()
    started = time.time()
    args = {"consent": bool(consent)}
    if not consent:
        message = "explicit location consent is required"
        _record_direct_tool(
            "approximate_location_lookup", args, ok=False, started=started,
            summary=message,
        )
        return "ERROR: %s" % message
    try:
        location = web_tools.approximate_location_lookup()
        output = web_tools.format_approximate_location(location)
    except Exception as exc:
        _record_direct_tool(
            "approximate_location_lookup", args, ok=False, started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    _record_direct_tool(
        "approximate_location_lookup", args, ok=True, started=started,
        summary=web_tools.location_label(location), output=output,
    )
    return output


def _chat_location(
    location_consent=False,
    location_hint=None,
    allow_server_location_lookup=False,
):
    if not location_consent:
        raise ValueError("approximate location is not enabled")
    started = time.time()
    source = "client_hint" if location_hint else "server_lookup"
    args = {"consent": True, "source": source}
    try:
        if location_hint:
            location = web_tools.normalize_location_hint(location_hint)
        elif allow_server_location_lookup:
            location = web_tools.approximate_location_lookup()
        else:
            raise ValueError("the client did not provide an approximate location")
        output = web_tools.format_approximate_location(location)
    except Exception as exc:
        _record_direct_tool(
            "approximate_location_lookup", args, ok=False, started=started,
            summary=str(exc),
        )
        raise
    _record_direct_tool(
        "approximate_location_lookup", args, ok=True, started=started,
        summary=(
            "%s (%s)" % (web_tools.location_label(location), source)
        ),
        output=output,
    )
    return location


# System prompt for web-routed research runs (chat_web_response): web tools
# only, no workspace discovery, stop as soon as the results answer.
_RESEARCH_AGENT_SYSTEM = (
    "You are answering a live-information question with public web tools. "
    "Use web_search to locate an authoritative source unless the user already "
    "supplied its URL. ALWAYS call web_fetch on the best source before "
    "answering, even if a search snippet looks sufficient. Never fill a "
    "missing version, price, office-holder, or "
    "date from model memory. Workspace and file tools are outside this run's "
    "allowlist. Cite fetched URLs in the final answer. As soon as the fetched "
    "source answers the question, return {\"final\": ...} immediately instead "
    "of calling more tools."
)


def chat_web_response(
    prompt: str,
    history=None,
    tier: str = "code",
    location_consent: bool = False,
    location_hint=None,
    allow_server_location_lookup: bool = False,
) -> str | None:
    """Handle explicit web chat intent before the plain model fallback."""
    _maybe_live_reload()
    # This function is the shared boundary for HTTP, MCP, and REPL chat. Keep
    # developer/work requests on the execution/model path even when their text
    # mentions a volatile noun ("build a current-price widget"). Explicit web
    # search orders remain authoritative and intentionally bypass this gate.
    if intents.classify_work(prompt) and not web_intents.explicit_search(prompt):
        return None
    intent = web_intents.classify(prompt, history=history)
    if intent is None:
        return None
    if intent["kind"] == "capability":
        if web_tools.enabled():
            return (
                "Yes. Live public web search, page fetch, and weather tools are enabled. "
                "Ask me to search the web or give me a city/state or ZIP for weather."
            )
        return (
            "This Sonder build has web tools, but they are disabled in the current "
            "runtime by SONDER_WEB_TOOLS."
        )
    if not web_tools.enabled():
        return "Web tools are disabled in the current runtime by SONDER_WEB_TOOLS."
    location = None
    if intent["kind"] == "location" or intent.get("needs_location"):
        if not location_consent:
            return (
                "Approximate location is off. Enable `Allow approximate IP location` "
                "in Settings, or tell me your city/state or ZIP directly."
            )
        try:
            location = _chat_location(
                location_consent=location_consent,
                location_hint=location_hint,
                allow_server_location_lookup=allow_server_location_lookup,
            )
        except Exception as exc:
            return (
                "Approximate location is enabled, but lookup did not return a usable "
                "place (%s). You can still send a city/state or ZIP." % exc
            )
        if intent["kind"] == "location":
            return web_tools.format_approximate_location(location)
    if intent["kind"] == "weather":
        requested_location = intent.get("location", "")
        prefix = ""
        if not requested_location and location_consent:
            try:
                location = _chat_location(
                    location_consent=location_consent,
                    location_hint=location_hint,
                    allow_server_location_lookup=allow_server_location_lookup,
                )
                requested_location = web_tools.location_label(location)
                prefix = web_tools.format_approximate_location(location) + "\n\n"
            except Exception as exc:
                return (
                    "Approximate location is enabled, but lookup did not return a "
                    "usable place (%s). Send a city/state or ZIP instead." % exc
                )
        if not requested_location:
            return (
                "I can use the live weather tool, but I need a location. Enable "
                "`Allow approximate IP location` in Settings, or send a city/state or "
                "ZIP, for example: `Chicago, IL` or `60601`."
            )
        return prefix + weather_lookup(requested_location)
    query = intent.get("query", prompt)
    # Current-info/news phrasing is conversational ("current news headline");
    # searching it verbatim ranks literal-match domains (current.com) first.
    # Construct a purposeful, dated provider query and hand it to the agent as
    # a suggestion while keeping the original question as the task.
    task = query
    search_query = web_tools.build_search_query(query, intent.get("kind", "research"))
    if search_query and search_query != query:
        task = (
            "Answer this question using live web results: %s\n"
            "Suggested web_search query (conversational filler already "
            "removed; use it or refine it): %s" % (query, search_query)
        )
    if intent.get("needs_location"):
        task = (
            "%s\n\nThe user explicitly enabled approximate IP location. Their "
            "approximate city/region is %s. Use only that place label, disclose that "
            "it may be inaccurate, and do not claim precise location."
            % (task, web_tools.location_label(location))
        )
    # Live-information questions get a web-only toolset and a research system
    # prompt: the default workspace-agent prompt invites text_search /
    # workspace discovery, which wastes serialized local-model steps on a pure
    # web question (observed: a spurious local text_search after web results
    # already answered the prompt).
    return _agent_impl(
        task,
        tier=tier or "code",
        max_steps=5,
        allow_web=True,
        required_tool_names=("web_fetch",),
        tool_allowlist=(
            "web_search", "web_fetch", "weather_lookup",
            "approximate_location_lookup",
        ),
        system=_RESEARCH_AGENT_SYSTEM,
    )


def _discard_interaction(interaction_id):
    """Purge a captured interaction so it can never reach the learning loop.

    Used when a model reply turned out to be a web-access refusal: the row was
    already logged by the answer path, and merely withholding the footer still
    leaves a poisoned task/response pair in the store. Best-effort; failures
    are swallowed (the row without an outcome is skipped by training exports
    anyway)."""
    if not interaction_id:
        return
    try:
        conn = _open_db()
        try:
            memory_store.delete_interaction(conn, interaction_id)
        finally:
            conn.close()
    except Exception:
        pass


def _web_denial_guard(
    prompt,
    reply,
    history=None,
    tier: str = "code",
    location_consent: bool = False,
    allow_server_location_lookup: bool = False,
):
    """Replace a reply that wrongly claims no web access with a tool-backed one.

    Post-hoc safety net for denial phrasings the pre-model regexes missed.
    Deliberately narrow to avoid rewriting legitimate answers: web tools must
    actually be enabled, the reply must match web_intents.denies_web_access
    (a claimed lack of access) or web_intents.fabricated_tool_call (a fenced
    block that fakes running web_search/web_fetch instead of denying access),
    AND the prompt itself must carry a positive web intent. Returns the
    replacement text (never captured / no footer) or None to keep the reply.
    """
    if not (
        web_intents.denies_web_access(reply)
        or web_intents.fabricated_tool_call(reply)
    ):
        return None
    if not web_tools.enabled():
        return None
    if web_intents.classify(prompt, history=history) is None:
        return None
    try:
        return chat_web_response(
            prompt,
            history=history,
            tier=tier,
            location_consent=location_consent,
            allow_server_location_lookup=allow_server_location_lookup,
        )
    except Exception:
        return None


def mcp_runtime_data() -> dict:
    """Return the loaded/current MCP source and tool-registry convergence state."""
    return mcp.runtime_snapshot()


def format_mcp_runtime(data: dict | None = None) -> str:
    data = mcp_runtime_data() if data is None else data
    loaded = str(data.get("loaded_digest") or "")[:12] or "unknown"
    current = str(data.get("current_digest") or "")[:12] or "unknown"
    lines = [
        "sonder MCP runtime",
        "  status: %s | live source refresh: %s"
        % (
            data.get("status", "unknown"),
            "on" if data.get("enabled") else "off",
        ),
        "  tools: %s | atomic refreshes: %s | last surface changed: %s"
        % (
            data.get("registered_tools", 0),
            data.get("refresh_count", 0),
            "yes" if data.get("last_surface_changed") else "no",
        ),
        "  MCP tool-list updates: %s"
        % ("advertised" if data.get("protocol_list_changed") else "not advertised"),
        "  source: %s" % (data.get("path") or "(unknown)"),
        "  loaded/current: %s / %s" % (loaded, current),
    ]
    if data.get("last_refresh_ts"):
        lines.append("  last refresh unix time: %s" % data["last_refresh_ts"])
    if data.get("last_error"):
        lines.append(
            "  ERROR: %s (last known-good registry remains active)" % data["last_error"]
        )
    if data.get("last_notification_error"):
        lines.append("  notification warning: %s" % data["last_notification_error"])
    return "\n".join(lines)


@mcp.tool()
def artifact_ground(
    path: str,
    recipe: str = "auto",
    requirements_json: str = "",
    token: str = "",
    approval: str = "",
    extra_roots: str = "",
) -> str:
    """Ground an artifact path with deterministic format-specific recipes.

    Recipes include auto, bundle, writing/markdown/text, data/JSON/CSV,
    editable Office DOCX/XLSX/PPTX packages, AVI video, animated GIF, MIDI,
    SRT/WebVTT captions, EDL timelines, UI/HTML/SVG, PNG/PPM images, WAV audio,
    and OBJ models. Requirements are an optional JSON object with fields such as
    required_files, required_kinds, required_text, required_headings,
    required_fields, required_columns, min_paragraphs, min_rows, min_slides,
    min_frames, min_notes, min_cues, min_events, required_sheet_names, and
    no_external_dependencies.
    """
    _maybe_live_reload()
    started = time.time()
    try:
        resolved = file_ops.resolve_path(
            path,
            extra_roots=extra_roots,
            bypass=_file_bypass_allowed(token, approval),
        )
        requirements = artifact_grounding.parse_requirements(requirements_json)
        result = artifact_grounding.validate(resolved, recipe, requirements)
    except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        _record_direct_tool(
            "artifact_ground",
            {"path": path, "recipe": recipe},
            ok=False,
            started=started,
            summary=str(exc),
        )
        return "ERROR: %s" % exc
    output = artifact_grounding.format_result(result)
    _record_direct_tool(
        "artifact_ground",
        {"path": str(resolved), "recipe": result.get("recipe", recipe)},
        ok=bool(result.get("ok")),
        started=started,
        summary="%s; %s files; %s failed checks"
        % (
            "passed" if result.get("ok") else "failed",
            result.get("checked_files", 0),
            result.get("failed_checks", 0),
        ),
        output=output,
    )
    return output


@mcp.tool()
def mcp_runtime_status() -> str:
    """Show live MCP source/tool convergence and fail-closed refresh state."""
    return format_mcp_runtime()


@mcp.tool()
def live_reload_status() -> str:
    """Show helper-module and atomic MCP tool-registry live reload state."""
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
    lines.extend(["", format_mcp_runtime()])
    lines.append(
        "note: updated MCP implementations and tool schemas swap atomically; invalid source keeps the last known-good registry."
    )
    return "\n".join(lines)


@mcp.tool()
def system_profile_text() -> str:
    """Read the editable standing instructions injected into sonder.

    The profile lives in system_profile.md by default and is read on every
    sonder/serve request, so edits take effect without restarting the proxy or
    REPL. Empty means no extra standing instructions are injected.
    """
    _maybe_live_reload()
    text, path = system_profile.ensure_profile()
    return "profile: %s\n\n%s" % (path, text or "(empty)")


@mcp.tool()
def update_system_profile(text: str, mode: str = "append") -> str:
    """Append, replace, or clear sonder's editable standing instructions.

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

    Values are clamped to [-1.0, 1.0]. mode: merge (default), replace, clear,
    or reset/defaults.
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


@mcp.tool()
def tune_emotion_vectors(feedback_text: str, step: float = 0.1) -> str:
    """Live-tune emotion/tone vectors from plain-language feedback.

    Examples:
      "be warmer but more concise"
      "more rigorous, less playful, warmth=0.4"

    This applies small bounded deltas, writes emotion_vectors.json, and the next
    model request picks up the change without restarting.
    """
    _maybe_live_reload()
    feedback_text = (feedback_text or "").strip()
    if not feedback_text:
        return "ERROR: feedback_text is empty."
    try:
        vectors, path, deltas, explicit, matched = emotion_vectors.tune_from_text(
            feedback_text,
            step=step,
        )
    except ValueError as e:
        return "ERROR: %s" % e
    if not deltas and not explicit:
        return (
            "No emotion vector cues found. Try phrases like 'warmer', "
            "'more concise', 'more rigorous', or explicit assignments like warmth=0.5."
        )
    changes = []
    if deltas:
        changes.append("inferred deltas: " + ", ".join(
            "%s=%+0.2f" % (name, deltas[name]) for name in sorted(deltas)
        ))
    if explicit:
        changes.append("explicit set: " + ", ".join(
            "%s=%+0.2f" % (name, explicit[name]) for name in sorted(explicit)
        ))
    if matched:
        changes.append("matched cues: " + "; ".join(matched[:8]))
    return "Tuned emotion vectors (%s).\n%s\n\n%s" % (
        path,
        "\n".join(changes),
        emotion_vectors.format_vectors(vectors),
    )


def emotion_command(arg: str = "") -> str:
    """Handle REPL/serve `/emotion` commands with live file-backed updates."""
    text = (arg or "").strip()
    if not text or text.lower() in ("status", "list", "show"):
        return emotion_vector_status()
    lower = text.lower()
    if lower in ("reset", "defaults", "default"):
        return update_emotion_vectors("{}", mode="reset")
    if lower in ("clear", "off"):
        return update_emotion_vectors("{}", mode="clear")
    if lower.startswith("set "):
        text = text[4:].strip()
    if lower.startswith("tune "):
        return tune_emotion_vectors(text[5:].strip())
    assignments = emotion_vectors.parse_assignments(text)
    if assignments:
        return update_emotion_vectors(json.dumps(assignments), mode="merge")
    return tune_emotion_vectors(text)


@mcp.tool()
def learn_preference(text: str, scope: str = "global") -> str:
    """Teach Sonder a durable user preference immediately.

    This is for behavior, style, workflow defaults, names, and recurring user
    expectations. Learned preferences are injected into future local-model
    prompts and apply without restarting.
    """
    _maybe_live_reload()
    extracted = preference_learning.extract_preferences(text)
    text = extracted[0] if extracted else preference_learning.normalize_preference(text)
    if not text:
        return "ERROR: preference text is empty."
    key = preference_learning.preference_key(text)
    conn = _open_db()
    try:
        memory_store.upsert_preference(
            conn,
            memory_store.new_id(),
            scope or "global",
            key,
            text,
            confidence=0.8,
        )
        rows = memory_store.preferences_for_scope(conn, scope or "global", limit=20)
    finally:
        conn.close()
    return "Learned preference: %s\n\n%s" % (
        text,
        preference_learning.format_preferences(rows),
    )


@mcp.tool()
def preferences_status(include_disabled: bool = False, limit: int = 50) -> str:
    """List learned user preferences that shape future responses."""
    _maybe_live_reload()
    limit = _safe_limit(limit, 50, 200)
    conn = _open_db()
    try:
        rows = memory_store.all_preferences(
            conn,
            limit=limit,
            include_disabled=bool(include_disabled),
        )
    finally:
        conn.close()
    return "learned preferences\n%s" % preference_learning.format_preferences(rows)


def preference_command(arg: str = "") -> str:
    text = (arg or "").strip()
    if not text or text.lower() in ("list", "status", "show"):
        return preferences_status()
    lower = text.lower()
    if lower.startswith("forget "):
        target = text[7:].strip()
        if not target:
            return "usage: /prefer forget <id-or-key>"
        conn = _open_db()
        try:
            changed = memory_store.set_preference_enabled(conn, target, False)
        finally:
            conn.close()
        return "forgot %d matching preference(s)" % changed
    if lower.startswith("learn "):
        text = text[6:].strip()
    return learn_preference(text)


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
        preferences = [dict(r) for r in conn.execute(
            "SELECT id, scope, key, text, confidence, evidence_count FROM preferences "
            "WHERE text LIKE ? ESCAPE '\\' AND enabled=1 "
            "ORDER BY confidence DESC, evidence_count DESC, updated_ts DESC LIMIT ?",
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
    lines.extend(
        "  - %s: %s" % (lesson["id"], lesson["text"][:220])
        for lesson in lessons
    )
    lines.append("facts (%d):" % len(facts))
    lines.extend("  - %s/%s: %s" % (f["project"], f["id"], f["text"][:220]) for f in facts)
    lines.append("preferences (%d):" % len(preferences))
    lines.extend("  - %s/%s: %s" % (
        p["scope"], p["id"], p["text"][:220],
    ) for p in preferences)
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
    sonder will try to extract one concrete lesson into memory. Use grounded
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
            "preferences": memory_store.all_preferences(conn, limit=limit),
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
    """List the sonder-runtime MCP tools and what they are for."""
    tools = {
        "agent": "Run a Claude-like tool-calling loop that can use local tools and web tools.",
        "autopilot_start/autopilot_status/autopilot_resume/autopilot_pause/autopilot_cancel": "Run a restart-persistent local goal with evidence-aware checkpoints, bounded replans, host tool gates, and explicit lifecycle control.",
        "runtime_policy_status/runtime_policy_update": "Inspect or guarded-edit shared hot-reloadable local model mappings and execution-lane tiers; cloud opt-in stays separate.",
        "mcp_runtime_status/live_reload_status": "Audit atomic MCP source/tool convergence, refresh history, list-change signaling, and fail-closed reload errors.",
        "master_orchestrate/master_status/master_capacity/master_cancel/master_retry": "Run restart-safe hardware-scheduled orchestration, inspect capacity/activity, cancel fleets, and explicitly retry interrupted work.",
        "admin_register/admin_login/admin_accounts/admin_set_account": "Manage hosted accounts, roles, bans, tiers, and developer flags.",
        "admin_status/debug_inspect/admin_private_chain_of_thought": "Inspect admin/debug state and safely deny private chain-of-thought exposure.",
        "sonder": "Ask through Sonder Runtime's local learning loop.",
        "offload": "Route a self-contained task to a configured local/cloud tier.",
        "web_search/web_fetch/weather_lookup/approximate_location_lookup": "Search/fetch public pages, get sourced weather, or resolve an explicitly consented approximate IP location without retaining the IP.",
        "workspace_inventory/directory_tree/directory_create/text_search/file_read_range": "Budgeted guarded workspace inventory, folder discovery, creation, text search, and bounded line-range reads.",
        "file_policy/file_find/file_read/file_write/file_edit/file_delete": "Guarded filesystem find/read/create/edit/delete with approval bypass support.",
        "program_search/script_search/workspace_run/script_run/image_inspect": "Discover installed programs and workspace scripts, run bounded argv-only processes, and inspect image metadata.",
        "task_create/task_list/task_update/task_show/checklist_create/checklist_update/checklist_show": "Visible todo and ordered checklist state shared by console, app, agents, and MCP.",
        "workbench_agent": "Run an autonomous local tool loop with a guaranteed checklist, exact action transcript, validation gate, and end report.",
        "command_registry_list": "Inspect available slash commands by category, name, or risk.",
        "activity_status": "Inspect active/latest response activity, tool calls, and file changes.",
        "permission_policy/permission_rule_set": "Inspect or guarded-edit local permission rules for tool actions.",
        "context_compaction_plan": "Preview when to summarize, split sessions, or reduce live context.",
        "run_code": "Run a bounded Python/JS/PowerShell/C++/C# snippet.",
        "ground_artifact": "Validate in-memory non-code content with exact/contains/regex/JSON checks.",
        "artifact_ground": "Validate files or bundles with inferred writing, data, editable Office/media/timelines, UI, image, audio, and static or animated humanoid model recipes.",
        "run_project": "Run a bounded temporary multi-file project with optional build commands.",
        "artifact_generate/artifact_verify": "Create and verify stdlib-only images, animated GIF/AVI video, SVGs, Office files, MIDI/WAV audio, captions, EDL timelines, data, web mockups, OBJ and textured humanoid GLBs with full morph frames and clip sequences, scenes, and themed packs from a free-form brief.",
        "game_reference_suite/game_generate_and_test/game_generation_campaign": "Build, execute, repair, and ground persistent in-house 2D/2.5D/3D game projects and fleets.",
        "loop": "Repeat bounded code/model/system actions.",
        "workflow_list/save/run/delete": "Manage reusable loop workflows.",
        "system_profile_text/update_system_profile": "Read or edit standing instructions.",
        "emotion_vector_status/update_emotion_vectors/tune_emotion_vectors": "Read, edit, or live-tune tone vectors.",
        "learn_preference/preferences_status": "Read or teach durable user behavior/workflow preferences.",
        "memory_search/memory_export/session_export": "Inspect local memory.",
        "learning_health_status": "Inspect grounded outcome coverage, signal quality, lesson provenance, distillation yield, and memory hygiene.",
        "memory_quality_report/memory_quality_repair": "Audit and dry-run/prune exact duplicate lessons.",
        "memory_privacy_review/memory_privacy_repair": "Review redacted privacy findings and explicitly dry-run/remove selected flagged lessons.",
        "memory_embedding_backfill": "Dry-run or backfill missing semantic vectors with the local embedding model.",
        "system_improvement_report": "Suggest next improvements from learning, memory, context, and deployment signals.",
        "context_policy_status/set_context_size": "Show or select requested virtual context up to 1m while clamping Ollama native num_ctx.",
        "learn_from_example/apply_learned": "Teach from examples and preview lesson application.",
        "self_heal_check/self_heal_repair": "Detect and safely repair common local breakage.",
        "context_health/diagnostics/live_reload_status/status/unload": "Observe and manage runtime health.",
        "record_outcome": "Feed grounded outcomes back into learning.",
        "sonder_stats/sonder_sessions/sonder_remember_fact": "Memory observability and durable facts.",
    }
    return "\n".join("  %s: %s" % item for item in sorted(tools.items()))


AGENT_TOOL_HELP = """Available tools:
- run_code: {"code": "...", "language": "python|js|powershell|cpp|csharp", "stdin": "", "timeout": 10}
- run_project: {"files_json": {"files": {"src/main.cpp": "..."}}, "commands_json": [{"cmd": ["g++", "src/main.cpp", "-o", "app"]}], "stdin": "", "timeout": 60}
- artifact_generate: {"name": "brand-kit", "brief": "fiery logo, DOCX report, AVI video, MIDI score, captions, textured humanoid 3D mascot with full morph frames and sequenced Idle Walk Run clips", "kinds": "auto|all|icon,vector,diagram,document,docx,data,spreadsheet,presentation,animation,video,music,midi,captions,timeline,web,model,rigged_model", "dimension": "auto|2d|2.5d|3d", "theme": "auto|ember|verdant|arcane|frost"}
- artifact_verify: {"path": "artifacts/generated/brand-kit"}
- artifact_ground: {"path": "artifacts/generated/brand-kit", "recipe": "auto|bundle|writing|data|office|docx|xlsx|pptx|avi|gif|glb|midi|srt|vtt|edl|ui|markdown|json|csv|html|svg|png|ppm|wav|obj", "requirements_json": {"required_files": ["rigged.glb"], "min_vertices": 384, "min_triangles": 192, "min_joints": 17, "min_animations": 6, "min_animation_sequences": 2, "min_skeletal_animations": 4, "min_morph_animations": 2, "min_morph_targets": 2, "min_images": 3, "min_textures": 3, "min_texcoord_sets": 1, "required_animation_clips": ["Idle", "Walk", "Run", "Breathe", "Focus"], "require_humanoid_rig": true, "require_animation_clip_metadata": true, "require_morph_normals": true, "require_morph_tangents": true, "require_embedded_images": true, "require_material_textures": true, "require_named_animations": true, "require_named_morph_targets": true, "require_power_of_two_images": true, "require_tangents": true, "no_external_dependencies": true}}
- game_reference_suite: {"name": "reference-suite", "theme": "arcane", "max_workers": 2, "timeout": 30}
- game_generate_and_test: {"name": "arena", "concept": "isometric action RPG", "language": "python|javascript|cpp|csharp", "dimension": "2d|2.5d|3d", "theme": "arcane", "repair_rounds": 1}
- game_generation_campaign: {"name": "game-fleet", "concept": "action roguelite", "total": 6, "language": "", "dimension": "", "theme": "arcane", "max_workers": 2, "repair_rounds": 1}
- web_search: {"query": "...", "limit": 5}
- web_fetch: {"url": "https://...", "max_chars": 8000}
- weather_lookup: {"location": "Chicago, IL|60601", "forecast_days": 3, "units": "auto|metric|imperial"}
- approximate_location_lookup: {"consent": true} (only after the user explicitly enables or requests IP location)
- file_policy: {}
- workspace_inventory: {"path": ".", "max_entries": 20000, "timeout_seconds": 10, "top_n": 15}
- directory_tree: {"path": ".", "depth": 2, "max_entries": 200}
- directory_create: {"path": "output/reports", "parents": true}
- file_find: {"query": "*.py", "root": ".", "max_results": 50}
- file_read: {"path": "README.md"}
- file_read_range: {"path": "server.py", "start_line": 1, "end_line": 200}
- text_search: {"query": "TODO", "root": ".", "glob": "*.py", "regex": false, "max_results": 100}
- file_write: {"path": "notes.txt", "content": "...", "mode": "create|overwrite|append"}
- file_edit: {"path": "notes.txt", "old": "before", "new": "after", "count": 1}
- file_delete: {"path": "notes.txt", "dry_run": true}
- script_search: {"query": "build", "root": ".", "max_results": 100}
- program_search: {"query": "python", "max_results": 50}
- workspace_run: {"program": "git", "args_json": ["status", "--short"], "cwd": ".", "timeout": 30}
- script_run: {"path": "scripts/check.py", "args_json": [], "cwd": ".", "timeout": 30}
- image_inspect: {"path": "artifacts/generated/demo/icon.png"}
- task_create: {"title": "...", "detail": "...", "priority": 2, "project": "...", "owner": "..."}
- task_list: {"status": "pending|in_progress|blocked|done|canceled", "project": "", "include_done": false, "limit": 50}
- task_update: {"task_id": "...", "status": "in_progress|blocked|done", "note": "..."}
- task_show: {"task_id": "..."}
- checklist_create: {"title": "...", "items_json": ["Inspect", "Implement", "Validate", "Report"], "project": "..."}
- checklist_update: {"checklist_id": "...", "item": "1|id-prefix", "status": "in_progress|done|blocked", "note": "..."}
- checklist_show: {"checklist_id": "..."}
- command_registry_list: {"filter_text": "filesystem|dangerous|context"}
- activity_status: {}
- permission_policy: {"tool_name": "file_delete"}
- context_compaction_plan: {"session": "", "project": ""}
- memory_search: {"query": "...", "limit": 10}
- ground_artifact: {"artifact": "...", "checks_json": [{"type": "contains", "text": "..."}]}
- apply_learned: {"task": "...", "limit": 5}
- workflow_run: {"name": "...", "max_iterations": 1}
- diagnostics: {}
- context_health: {}
- learning_health_status: {}
- context_policy_status: {"context_size": "1m"}
- set_context_size: {"context_size": "256k"}
- memory_quality_report: {"sample_limit": 5}
- memory_quality_repair: {"apply": false}
- memory_privacy_review: {"sample_limit": 20}
- memory_privacy_repair: {"lesson_ids_json": ["lesson-id"], "apply": false}
- memory_embedding_backfill: {"limit": 25, "apply": false}
- system_improvement_report: {}
- master_orchestrate: {"task": "...", "mode": "ask|inline|delegate|fleet", "agents": 3, "tier": "code"}
- master_status: {}
- master_capacity: {"requested_agents": 0}
- master_cancel: {"agent_id": "master-id|prefix|all"}
- master_retry: {"agent_id": "master-id|prefix", "tier": "code"}
- self_heal_check: {}
- self_heal_repair: {"apply": false}
- status: {}
- system_profile_text: {}
- emotion_vector_status: {}
- update_emotion_vectors: {"vectors_json": {"warmth": 0.5, "brevity": 0.2}, "mode": "merge|replace|clear|reset"}
- tune_emotion_vectors: {"feedback_text": "be warmer but more concise", "step": 0.1}
- learn_preference: {"text": "User prefers concise status updates.", "scope": "global"}
- preferences_status: {"include_disabled": false, "limit": 20}
- tool_manifest: {}
- offload: {"prompt": "...", "tier": "fast|code|general|cloud-code|cloud-general"}

Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}, "reason": "short reason"}
or
{"final": "your final answer"}
"""


REPOSITORY_READ_ONLY_TOOLS = frozenset({
    "file_policy", "workspace_inventory", "directory_tree", "file_find", "file_read", "file_read_range",
    "text_search", "script_search", "program_search", "image_inspect", "command_registry_list",
    "activity_status", "permission_policy", "context_compaction_plan",
    "diagnostics", "context_health", "learning_health_status", "context_policy_status", "artifact_ground",
    "memory_quality_report", "memory_privacy_review", "system_improvement_report", "master_status", "master_capacity",
    "self_heal_check", "status", "system_profile_text",
    "emotion_vector_status", "preferences_status", "tool_manifest",
    "memory_search", "web_search", "web_fetch", "weather_lookup",
})
REPOSITORY_READ_ONLY_FORBIDDEN_ARGS = frozenset({
    "token", "approval", "extra_roots",
})
REPOSITORY_AGENT_TOOL_HELP = """Available tools:
- file_policy: {}
- workspace_inventory: {"path": ".", "max_entries": 20000, "timeout_seconds": 10, "top_n": 15}
- directory_tree: {"path": ".", "depth": 2, "max_entries": 200}
- file_find: {"query": "*.py", "root": ".", "max_results": 50}
- file_read: {"path": "README.md", "max_bytes": 256000}
- file_read_range: {"path": "server.py", "start_line": 1, "end_line": 200}
- text_search: {"query": "TODO", "root": ".", "glob": "*.py", "max_results": 100}
- script_search: {"query": "build", "root": ".", "max_results": 100}
- program_search: {"query": "python", "max_results": 50}
- image_inspect: {"path": "docs/example.png"}
- memory_search: {"query": "...", "limit": 10}
- web_search: {"query": "...", "limit": 5}
- web_fetch: {"url": "https://...", "max_chars": 8000}
- weather_lookup: {"location": "Chicago, IL|60601", "forecast_days": 3, "units": "auto|metric|imperial"}
- command_registry_list: {"filter_text": "filesystem|context|status"}
- activity_status: {}
- permission_policy: {"tool_name": "file_read"}
- context_compaction_plan: {"session": "", "project": ""}
- diagnostics: {}
- context_health: {"session": "", "project": ""}
- learning_health_status: {}
- artifact_ground: {"path": "artifacts/generated/report", "recipe": "auto", "requirements_json": {}}
- context_policy_status: {"context_size": "32k"}
- memory_quality_report: {"sample_limit": 5}
- memory_privacy_review: {"sample_limit": 20}
- system_improvement_report: {"session": "", "project": ""}
- master_status: {}
- master_capacity: {"requested_agents": 0}
- self_heal_check: {}
- status: {}
- system_profile_text: {}
- emotion_vector_status: {}
- preferences_status: {"include_disabled": false, "limit": 20}
- tool_manifest: {}

Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}, "reason": "short reason"}
or
{"final": "your final answer"}
"""


def _agent_tool_help(read_only=False):
    return REPOSITORY_AGENT_TOOL_HELP if read_only else AGENT_TOOL_HELP


def _repository_read_only_error(tool_name, args):
    if not isinstance(args, dict):
        return "ERROR: repository read-only tool args must be a JSON object."
    if tool_name not in REPOSITORY_READ_ONLY_TOOLS:
        return "ERROR: tool '%s' is not allowed by the repository read-only policy." % tool_name
    forbidden = sorted(REPOSITORY_READ_ONLY_FORBIDDEN_ARGS.intersection(args))
    if forbidden:
        return (
            "ERROR: repository read-only tool '%s' forbids argument(s): %s."
            % (tool_name, ", ".join(forbidden))
        )
    try:
        if tool_name in {"file_read", "file_read_range", "image_inspect"}:
            file_ops.resolve_repository_read_path(
                args.get("path", ""),
                allow_workspace_root=False,
                reject_sensitive=True,
            )
        elif tool_name in {"workspace_inventory", "directory_tree", "file_find", "text_search", "script_search"}:
            file_ops.resolve_repository_read_path(
                args.get("path", "") or args.get("root", "") or ".",
                allow_workspace_root=True,
                reject_sensitive=True,
            )
    except (PermissionError, ValueError) as exc:
        return "ERROR: repository read-only path rejected: %s" % exc
    return ""


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


_AGENT_OBSERVATION_PROMPT_CHARS = 9000
_AGENT_DECISION_REPAIR_LIMIT = 2
_AGENT_NEGATIVE_CLAIM_RE = re.compile(
    r"\b(?:does not|doesn't|did not|could not|cannot|can't)\s+"
    r"(?:contain|include|find|locate|exist)\b|"
    r"\b(?:not found|no matches?|none found|missing from)\b",
    re.IGNORECASE,
)
_AGENT_CLAIM_REVIEW_TOOLS = frozenset({
    "text_search", "file_read_range", "file_find",
})
_AGENT_QUOTED_ANCHOR_RE = re.compile(
    r"`([^`\r\n]{2,120})`|\"([^\"\r\n]{2,120})\"|\'([^\'\r\n]{2,120})\'"
)
_AGENT_HEADING_ANCHOR_RE = re.compile(
    r"\b(?:its|the|a|an)\s+"
    r"([A-Z][A-Za-z0-9_.:-]*(?:\s+[A-Za-z0-9_.:-]+){0,5})\s+heading\b"
)
_AGENT_TASK_PATH_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9_.-]+\.(?:md|txt|py|dart|js|ts|json|yaml|yml|toml|"
    r"cpp|cc|cxx|h|hpp|cs|html|css|svg))(?![\w.-])",
    re.IGNORECASE,
)
_AGENT_SEARCH_QUERY_RE = re.compile(r"text search:\s*'([^'\r\n]+)'", re.IGNORECASE)


def _clip_agent_prompt_text(text, limit):
    """Keep useful context from both ends of a long tool observation."""
    text = str(text or "")
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    if limit <= 48:
        return text[:limit]
    marker = "\n...[observation compacted by host]...\n"
    remaining = limit - len(marker)
    head = max(1, (remaining * 2) // 3)
    tail = max(1, remaining - head)
    return text[:head] + marker + text[-tail:]


def _agent_observation_prompt(
    observations, max_chars=_AGENT_OBSERVATION_PROMPT_CHARS,
):
    """Build a bounded model-facing window while the host retains full evidence."""
    values = [str(item or "") for item in observations if str(item or "").strip()]
    if not values:
        return ""
    max_chars = max(512, int(max_chars))
    full = "Tool observations so far:\n" + "\n\n".join(values)
    if len(full) <= max_chars:
        return full

    summary_budget = min(1400, max_chars // 5)
    recent_header = "Recent tool observations (full host ledger retained):\n"
    recent_budget = max(256, max_chars - summary_budget - len(recent_header) - 4)
    selected = []
    selected_chars = 0
    first_selected = len(values)
    for index in range(len(values) - 1, -1, -1):
        value = values[index]
        separator = 2 if selected else 0
        if selected_chars + separator + len(value) <= recent_budget:
            selected.insert(0, value)
            selected_chars += separator + len(value)
            first_selected = index
            continue
        if not selected:
            selected.append(_clip_agent_prompt_text(value, recent_budget))
            first_selected = index
        break

    recent = recent_header + "\n\n".join(selected)
    older = values[:first_selected]
    if not older:
        return _clip_agent_prompt_text(recent, max_chars)

    summary_lines = []
    for item in older[-8:]:
        first_line = next((line.strip() for line in item.splitlines() if line.strip()), "")
        summary_lines.append("- " + _clip_agent_prompt_text(first_line, 180))
    omitted = max(0, len(older) - len(summary_lines))
    summary_header = "Earlier observation summaries (%d compacted" % len(older)
    if omitted:
        summary_header += ", %d older omitted" % omitted
    summary = summary_header + "):\n" + "\n".join(summary_lines)
    summary = _clip_agent_prompt_text(summary, summary_budget)
    result = summary + "\n\n" + recent
    if len(result) <= max_chars:
        return result
    # Preserve the recent window if header arithmetic changes in future edits.
    return _clip_agent_prompt_text(result, max_chars)


def _agent_generate_decision(
    gen,
    step_prompt,
    repair_limit=_AGENT_DECISION_REPAIR_LIMIT,
    require_final=False,
):
    """Generate one structurally valid agent decision with bounded format repair."""
    repair_limit = max(0, min(4, int(repair_limit)))
    raw = gen(step_prompt)
    error = None
    for attempt in range(repair_limit + 1):
        try:
            decision = _extract_agent_json(raw)
            if not isinstance(decision, dict):
                raise ValueError("agent decision must be a JSON object")
            if require_final and "final" not in decision:
                raise ValueError("agent finalization response must contain 'final'")
            if not require_final and "final" not in decision and not decision.get("tool"):
                raise ValueError("agent decision omitted both 'tool' and 'final'")
            return decision, raw, None
        except Exception as exc:
            error = exc
        if attempt >= repair_limit:
            break
        valid_shape = (
            '{"final":"answer"}'
            if require_final else
            '{"tool":"name","args":{},"reason":"brief"} or {"final":"answer"}'
        )
        repair_prompt = (
            step_prompt
            + "\n\nHOST FORMAT REPAIR %d/%d: Your previous response was invalid. "
            "Return exactly one JSON object and no prose or Markdown. Use %s.\n"
            "Parser error: %s\nPrevious response excerpt:\n%s"
            % (
                attempt + 1,
                repair_limit,
                valid_shape,
                error,
                str(raw or "")[:1000],
            )
        )
        raw = gen(repair_prompt)
    return None, raw, error


def _agent_task_exact_anchors(task: str) -> list[str]:
    """Extract explicit literals and named headings worth exact negative search."""
    text = str(task or "")
    anchors = []
    for match in _AGENT_QUOTED_ANCHOR_RE.finditer(text):
        anchor = next((value for value in match.groups() if value), "").strip()
        if anchor and len(anchor.split()) <= 12:
            anchors.append(anchor)
    for match in _AGENT_HEADING_ANCHOR_RE.finditer(text):
        anchor = match.group(1).strip().rstrip(".:")
        if anchor:
            anchors.append(anchor)
    deduped = []
    seen = set()
    for anchor in anchors:
        key = re.sub(r"\s+", " ", anchor).strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(anchor)
    return deduped[:6]


def _agent_exact_negative_action(task: str, observations) -> dict | None:
    """Require exact anchor queries before accepting a negative existence claim."""
    anchors = _agent_task_exact_anchors(task)
    if not anchors:
        return None
    exact_queries = set()
    for observation in observations:
        text = str(observation or "")
        if "ERROR:" in text:
            continue
        for match in _AGENT_SEARCH_QUERY_RE.finditer(text):
            exact_queries.add(re.sub(r"\s+", " ", match.group(1)).strip().lower())
    missing = next(
        (
            anchor for anchor in anchors
            if re.sub(r"\s+", " ", anchor).strip().lower() not in exact_queries
        ),
        None,
    )
    if not missing:
        return None
    args = {
        "query": missing,
        "root": ".",
        "regex": False,
        "max_results": 20,
    }
    paths = _AGENT_TASK_PATH_RE.findall(str(task or ""))
    if paths:
        args["glob"] = paths[0]
    return {
        "decision": "continue",
        "reason": "the exact task anchor %r has not been searched" % missing,
        "tool": "text_search",
        "args": args,
    }


def _agent_negative_claim_review(
    task: str,
    final: str,
    observations,
    model: str,
    cloud: bool = False,
) -> dict:
    """Audit negative existence claims without letting the reviewer invent facts."""
    if not _AGENT_NEGATIVE_CLAIM_RE.search(str(final or "")):
        return {"decision": "accept", "reason": "no negative existence claim"}
    exact_action = _agent_exact_negative_action(task, observations)
    if exact_action:
        return exact_action
    system = _build_system(
        "You are a local evidence reviewer. Return exactly one JSON object and no "
        "prose or chain-of-thought. Decide only accept or continue. Accept a negative "
        "existence claim only when tool evidence searched the exact shortest useful "
        "anchor across the relevant scope. Reject a paraphrased/descriptive search "
        "query, a clipped read that did not reach the target, or a scope mismatch. "
        "Never rewrite the answer or invent evidence; continue must return exactly "
        "one structured read-only evidence action using text_search, file_read_range, "
        "or file_find.",
        False,
        "",
    )
    review_prompt = (
        "Task:\n%s\n\nProposed final:\n%s\n\n%s\n\n"
        "JSON schema: {\"decision\":\"accept|continue\",\"reason\":\"brief\","
        "\"tool\":\"text_search|file_read_range|file_find or empty\","
        "\"args\":{}}"
        % (
            str(task or "")[:8000],
            str(final or "")[:4000],
            _agent_observation_prompt(observations, max_chars=7000),
        )
    )
    gen = _make_generate(model, system, 0.0, 260, 4096, cloud=cloud)
    correction = ""
    last_error = "invalid claim review"
    for _attempt in range(2):
        raw = gen(review_prompt + correction)
        try:
            payload = _extract_agent_json(raw)
            if not isinstance(payload, dict):
                raise ValueError("claim review must be a JSON object")
            decision = str(payload.get("decision") or "").strip().lower()
            if decision not in {"accept", "continue"}:
                raise ValueError("claim review decision must be accept or continue")
            reason = re.sub(r"\s+", " ", str(payload.get("reason") or "")).strip()
            tool = str(payload.get("tool") or "").strip()
            args = payload.get("args") or {}
            if not reason:
                raise ValueError("claim review needs a reason")
            if not isinstance(args, dict):
                raise ValueError("claim review args must be a JSON object")
            if decision == "continue" and tool not in _AGENT_CLAIM_REVIEW_TOOLS:
                raise ValueError(
                    "continued claim review needs an approved read-only tool"
                )
            if decision == "accept":
                tool, args = "", {}
            return {
                "decision": decision,
                "reason": reason[:500],
                "tool": tool,
                "args": args,
            }
        except (TypeError, ValueError) as exc:
            last_error = str(exc)
            correction = (
                "\n\nHOST SCHEMA ERROR: %s. Return corrected JSON only."
                % last_error
            )
    return {
        "decision": "continue",
        "reason": "negative claim review failed safely: %s" % last_error,
        "tool": "",
        "args": {},
    }


def _agent_dispatch(
    tool_name, args, allow_web=True, read_only=False, allow_location=False,
):
    tool_name = (tool_name or "").strip()
    args = args or {}
    if not isinstance(args, dict):
        return "ERROR: tool args must be a JSON object"
    if read_only:
        policy_error = _repository_read_only_error(tool_name, args)
        if policy_error:
            return policy_error
        if tool_name in {"command_registry_list", "tool_manifest"}:
            return _agent_tool_help(read_only=True)
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
    if tool_name in ("artifact_generate", "assetgen"):
        return artifact_generate(
            name=args.get("name", "generated-artifact"),
            brief=args.get("brief", args.get("prompt", "")),
            kinds=args.get("kinds", "auto"),
            dimension=args.get("dimension", "auto"),
            theme=args.get("theme", "auto"),
            seed=args.get("seed"),
            output_dir=args.get("output_dir", ""),
        )
    if tool_name == "artifact_verify":
        return artifact_verify(args.get("path", ""))
    if tool_name == "artifact_ground":
        return artifact_ground(
            path=args.get("path", ""),
            recipe=args.get("recipe", "auto"),
            requirements_json=args.get("requirements_json", args.get("requirements", "")),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "game_reference_suite":
        return game_reference_suite(
            name=args.get("name", "sonder-reference"),
            theme=args.get("theme", "arcane"),
            seed=args.get("seed", 1337),
            max_workers=args.get("max_workers", 2),
            timeout=args.get("timeout", 30),
        )
    if tool_name in ("game_generate_and_test", "game_generate"):
        return game_generate_and_test(
            name=args.get("name", "generated-game"),
            concept=args.get("concept", args.get("prompt", "")),
            language=args.get("language", "python"),
            dimension=args.get("dimension", "2d"),
            theme=args.get("theme", "arcane"),
            seed=args.get("seed", 1337),
            tier=args.get("tier", "code"),
            timeout=args.get("timeout", 30),
            repair_rounds=args.get("repair_rounds"),
        )
    if tool_name in ("game_generation_campaign", "game_campaign"):
        return game_generation_campaign(
            name=args.get("name", "game-fleet"),
            concept=args.get("concept", args.get("prompt", "compact action game")),
            total=args.get("total", 6),
            language=args.get("language", ""),
            dimension=args.get("dimension", ""),
            theme=args.get("theme", "arcane"),
            tier=args.get("tier", "code"),
            max_workers=args.get("max_workers", 2),
            timeout=args.get("timeout", 30),
            repair_rounds=args.get("repair_rounds"),
        )
    if tool_name == "web_search":
        if not allow_web:
            return "ERROR: web access disabled for this agent run"
        return web_search(args.get("query", ""), args.get("limit", 5))
    if tool_name == "web_fetch":
        if not allow_web:
            return "ERROR: web access disabled for this agent run"
        return web_fetch(args.get("url", ""), args.get("max_chars", 8000))
    if tool_name == "weather_lookup":
        if not allow_web:
            return "ERROR: web access disabled for this agent run"
        return weather_lookup(
            args.get("location", ""),
            args.get("forecast_days", 3),
            args.get("units", "auto"),
        )
    if tool_name == "approximate_location_lookup":
        if not allow_web:
            return "ERROR: web access disabled for this agent run"
        if not allow_location:
            return (
                "ERROR: approximate location requires host-verified user consent "
                "for this agent run"
            )
        return approximate_location_lookup(bool(args.get("consent", False)))
    if tool_name == "memory_search":
        return memory_search(args.get("query", ""), args.get("limit", 10))
    if tool_name == "file_policy":
        return file_policy(
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "workspace_inventory":
        return workspace_inventory(
            path=args.get("path", args.get("root", ".")),
            max_entries=args.get("max_entries", 20000),
            timeout_seconds=args.get("timeout_seconds", 10.0),
            top_n=args.get("top_n", 15),
            include_hidden=args.get("include_hidden", False),
            include_ignored=args.get("include_ignored", False),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "directory_tree":
        return directory_tree(
            path=args.get("path", args.get("root", ".")),
            depth=args.get("depth", 2),
            max_entries=args.get("max_entries", 200),
            include_hidden=args.get("include_hidden", False),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "directory_create":
        return directory_create(
            path=args.get("path", ""),
            parents=args.get("parents", True),
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
    if tool_name == "file_read_range":
        return file_read_range(
            path=args.get("path", ""),
            start_line=args.get("start_line", args.get("start", 1)),
            end_line=args.get("end_line", args.get("end", 200)),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "text_search":
        return text_search(
            query=args.get("query", args.get("pattern", "")),
            root=args.get("root", "."),
            glob=args.get("glob", "*"),
            regex=args.get("regex", False),
            case_sensitive=args.get("case_sensitive", False),
            max_results=args.get("max_results", 100),
            max_entries=args.get("max_entries", 20000),
            timeout_seconds=args.get("timeout_seconds", 10.0),
            include_hidden=args.get("include_hidden", False),
            include_ignored=args.get("include_ignored", False),
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
    if tool_name == "script_search":
        return script_search(
            query=args.get("query", "*"),
            root=args.get("root", "."),
            max_results=args.get("max_results", 100),
            max_entries=args.get("max_entries", 20000),
            timeout_seconds=args.get("timeout_seconds", 10.0),
            include_hidden=args.get("include_hidden", False),
            include_ignored=args.get("include_ignored", False),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "program_search":
        return program_search(
            query=args.get("query", "*"),
            max_results=args.get("max_results", 100),
        )
    if tool_name == "workspace_run":
        return workspace_run(
            program=args.get("program", ""),
            args_json=args.get("args_json", args.get("args", [])),
            cwd=args.get("cwd", "."),
            stdin=args.get("stdin", ""),
            timeout=args.get("timeout", 30),
            max_output=args.get("max_output", 128000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "script_run":
        return script_run(
            path=args.get("path", ""),
            args_json=args.get("args_json", args.get("args", [])),
            cwd=args.get("cwd", ""),
            stdin=args.get("stdin", ""),
            timeout=args.get("timeout", 30),
            max_output=args.get("max_output", 128000),
            token=args.get("token", ""),
            approval=args.get("approval", ""),
            extra_roots=args.get("extra_roots", ""),
        )
    if tool_name == "image_inspect":
        return image_inspect(
            path=args.get("path", ""),
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
            parent_id=args.get("parent_id", ""),
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
    if tool_name == "checklist_create":
        items = args.get("items_json", args.get("items", []))
        return checklist_create(
            title=args.get("title", "Work checklist"),
            items_json=json.dumps(items) if not isinstance(items, str) else items,
            project=args.get("project", ""),
            owner=args.get("owner", "agent"),
            priority=args.get("priority", 1),
        )
    if tool_name == "checklist_update":
        return checklist_update(
            checklist_id=args.get("checklist_id", args.get("id", "")),
            item=str(args.get("item", args.get("item_id", ""))),
            status=args.get("status", ""),
            note=args.get("note", ""),
        )
    if tool_name == "checklist_show":
        return checklist_show(args.get("checklist_id", args.get("id", "")))
    if tool_name == "command_registry_list":
        return command_registry_list(args.get("filter_text", args.get("filter", "")))
    if tool_name == "activity_status":
        return activity_status(include_events=args.get("include_events", True))
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
    if tool_name == "learning_health_status":
        return learning_health_status()
    if tool_name == "context_policy_status":
        return context_policy_status(args.get("context_size", ""))
    if tool_name == "set_context_size":
        return set_context_size(args.get("context_size", ""))
    if tool_name == "memory_quality_report":
        return memory_quality_report(sample_limit=args.get("sample_limit", 5))
    if tool_name == "memory_quality_repair":
        return memory_quality_repair(apply=args.get("apply", False))
    if tool_name == "memory_privacy_review":
        return memory_privacy_review(sample_limit=args.get("sample_limit", 20))
    if tool_name == "memory_privacy_repair":
        return memory_privacy_repair(
            lesson_ids_json=args.get("lesson_ids_json", args.get("lesson_ids", [])),
            apply=args.get("apply", False),
        )
    if tool_name == "memory_embedding_backfill":
        return memory_embedding_backfill(
            limit=args.get("limit", 25), apply=args.get("apply", False),
        )
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
    if tool_name in ("master_capacity", "agent_capacity"):
        return master_capacity(
            requested_agents=args.get("requested_agents", args.get("agents", 0)),
        )
    if tool_name in ("master_cancel", "agent_cancel"):
        return master_cancel(
            agent_id=args.get("agent_id", args.get("selector", "")),
        )
    if tool_name in ("master_retry", "agent_retry"):
        return master_retry(
            agent_id=args.get("agent_id", args.get("selector", "")),
            tier=args.get("tier", ""),
        )
    if tool_name in ("master_orchestrate", "master"):
        return master_orchestrate(
            task=args.get("task", args.get("prompt", "")),
            mode=args.get("mode", "ask"),
            agents=args.get("agents", 3),
            tier=args.get("tier", "auto"),
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
    if tool_name == "update_emotion_vectors":
        payload = args.get("vectors_json", args.get("vectors", {}))
        return update_emotion_vectors(
            json.dumps(payload) if not isinstance(payload, str) else payload,
            mode=args.get("mode", "merge"),
        )
    if tool_name == "tune_emotion_vectors":
        return tune_emotion_vectors(
            feedback_text=args.get("feedback_text", args.get("text", "")),
            step=args.get("step", 0.1),
        )
    if tool_name == "learn_preference":
        return learn_preference(
            text=args.get("text", ""),
            scope=args.get("scope", "global"),
        )
    if tool_name == "preferences_status":
        return preferences_status(
            include_disabled=args.get("include_disabled", False),
            limit=args.get("limit", 50),
        )
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


def _agent_activity_command(tool_name, args):
    args = args if isinstance(args, dict) else {}
    if tool_name == "workspace_run":
        return "%s %s" % (
            args.get("program", ""),
            json.dumps(args.get("args_json", args.get("args", [])), ensure_ascii=False),
        )
    if tool_name == "script_run":
        return "%s %s" % (
            args.get("path", ""),
            json.dumps(args.get("args_json", args.get("args", [])), ensure_ascii=False),
        )
    path = args.get("path") or args.get("root") or ""
    if path:
        return str(path)
    if args.get("query"):
        return "query=%s" % args["query"]
    return ""


def _agent_dispatch_observed(
    tool_name, args, allow_web=True, read_only=False, allow_location=False,
):
    started = time.time()
    ok = False
    observation = ""
    try:
        with activity_tracker.tool_dispatch_context():
            dispatch_options = {"allow_web": allow_web}
            if allow_location:
                dispatch_options["allow_location"] = True
            if read_only:
                observation = _agent_dispatch(
                    tool_name, args, read_only=True, **dispatch_options,
                )
            else:
                observation = _agent_dispatch(tool_name, args, **dispatch_options)
        ok = not str(observation).startswith("ERROR:")
        return observation
    finally:
        activity_tracker.record_tool_result(
            tool_name,
            args,
            ok=ok,
            elapsed_ms=int((time.time() - started) * 1000),
            summary=observation.splitlines()[0] if observation else "",
            command=_agent_activity_command(tool_name, args),
            output=observation,
        )


_WORK_MUTATION_TOOLS = frozenset({
    "directory_create", "file_write", "file_edit", "file_delete",
    "artifact_generate", "game_generate_and_test", "game_generation_campaign",
    "memory_quality_repair", "memory_privacy_repair", "memory_embedding_backfill",
})
_WORK_VALIDATION_TOOLS = frozenset({
    "workspace_run", "script_run", "run_code", "run_project", "ground_artifact", "artifact_ground",
    "artifact_verify", "game_reference_suite", "game_generate_and_test",
    "game_generation_campaign", "self_heal_check", "workspace_inventory", "directory_tree", "file_find",
    "file_read", "file_read_range", "text_search", "image_inspect",
    "memory_quality_report", "memory_privacy_review",
})


def _agent_observation_ok(observation):
    text = str(observation or "")
    lowered = text.lower()
    first = next((line.strip().lower() for line in text.splitlines() if line.strip()), "")
    return not (
        text.startswith("ERROR:")
        or "  ok: false" in lowered
        or first.endswith(": fail")
        or first.startswith("validation_failed")
        or "[fail]" in lowered
    )


def _agent_tool_observation_ok(tool_name, observation):
    """Apply evidence-quality checks that are specific to a tool contract."""
    if str(tool_name or "") == "web_fetch" and observation is None:
        return False
    if not _agent_observation_ok(observation):
        return False
    if str(tool_name or "") != "web_fetch":
        return True
    # A transport-level success with an empty page is not grounding. Require
    # at least one readable letter or digit before a fetch can satisfy the
    # research agent's required-tool evidence gate. Keep the generic success
    # predicate unchanged because empty/zero-ish output is valid for several
    # execution and inspection tools.
    text = str(observation or "").strip()
    return bool(text and any(character.isalnum() for character in text))


def _agent_normalized_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(str(file_ops.resolve_path(text)))
    except (OSError, PermissionError, ValueError):
        return os.path.normcase(os.path.abspath(text))


def _agent_mutation_record(tool_name, args):
    args = args if isinstance(args, dict) else {}
    path = args.get("path", "")
    if tool_name == "artifact_generate":
        path = args.get("output_dir") or os.path.join(
            "artifacts", "generated", str(args.get("name", "generated-artifact")),
        )
    elif tool_name in {"game_generate_and_test", "game_generation_campaign"}:
        path = os.path.join("games", str(args.get("name", "generated-game")))
    return {
        "tool": tool_name,
        "path": _agent_normalized_path(path),
    }


def _agent_validation_covers(tool_name, args, mutations, observation=""):
    """Require validators to touch changed disk state, not equivalent draft code."""
    args = args if isinstance(args, dict) else {}
    records = [record for record in mutations if record.get("tool")]
    if not records:
        return True
    paths = [record["path"] for record in records if record.get("path")]
    target = _agent_normalized_path(args.get("path", args.get("artifact", "")))

    if tool_name in {
        "game_reference_suite", "game_generate_and_test", "game_generation_campaign",
    }:
        return True
    if tool_name in {"memory_quality_report", "memory_privacy_review"}:
        return all(record["tool"] in {
            "memory_quality_repair", "memory_privacy_repair",
            "memory_embedding_backfill",
        } for record in records)
    if tool_name in {"artifact_verify", "artifact_ground", "ground_artifact"}:
        return any(
            record["tool"] == "artifact_generate"
            and (not target or target.startswith(record.get("path", "")))
            for record in records
        )
    if tool_name == "script_run":
        if target and target in paths:
            return True
        name = os.path.basename(target).lower()
        return any(word in name for word in ("test", "check", "verify", "smoke", "build"))
    if tool_name == "workspace_run":
        program = os.path.basename(str(args.get("program", ""))).lower()
        argv = args.get("args_json", args.get("args", []))
        if isinstance(argv, str):
            try:
                argv = json.loads(argv)
            except (TypeError, ValueError):
                argv = [argv]
        argv_text = [str(item).lower() for item in (argv or [])]
        for item in argv_text:
            if _agent_normalized_path(item) in paths:
                return True
        command_text = " ".join([program, *argv_text])
        known_validator = program in {
            "pytest", "pytest.exe", "ctest", "ctest.exe", "cmake", "cmake.exe",
            "ninja", "ninja.exe", "msbuild", "msbuild.exe", "flutter", "flutter.bat",
            "dart", "dart.exe", "cargo", "cargo.exe", "dotnet", "dotnet.exe",
            "npm", "npm.cmd", "gradle", "gradle.bat", "mvn", "mvn.cmd",
            "python", "python.exe", "py", "py.exe", "node", "node.exe",
            "cl", "cl.exe", "g++", "g++.exe", "clang++", "clang++.exe",
        }
        return known_validator and any(word in command_text for word in (
            "pytest", "unittest", "test", "check", "verify", "smoke", "build",
            "compile", "analyze", "lint", "ctest", "cmake", "ninja", "msbuild",
            "flutter", "dart", "cargo", "dotnet", "npm", "gradle", "mvn",
        ))
    if tool_name == "image_inspect":
        return bool(
            target in paths
            and os.path.splitext(target)[1].lower()
            in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ppm", ".svg"}
        )
    if tool_name in {"file_read", "file_read_range"}:
        return bool(
            target in paths
            and os.path.splitext(target)[1].lower()
            in {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".toml", ".xml"}
        )
    if tool_name in {"workspace_inventory", "directory_tree", "file_find", "text_search"}:
        root = _agent_normalized_path(args.get("root", args.get("path", ".")))
        observed = os.path.normcase(str(observation or ""))
        eligible = [
            record["path"] for record in records
            if record.get("path")
            and (
                (
                    tool_name in {"workspace_inventory", "directory_tree", "file_find"}
                    and record["tool"] == "directory_create"
                )
                or (
                    tool_name == "text_search"
                    and os.path.splitext(record["path"])[1].lower()
                    in {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".toml", ".xml"}
                )
            )
        ]
        return bool(eligible) and all(
            (path.startswith(root + os.sep) or path == root)
            and os.path.basename(path) in observed
            for path in eligible
        )
    # run_code/run_project validate generated snippets or temp projects, not the
    # persistent files just edited. self_heal_check is likewise unrelated.
    return False
_WORK_INSPECTION_TOOLS = frozenset({
    "file_policy", "workspace_inventory", "directory_tree", "file_find", "file_read", "file_read_range",
    "text_search", "script_search", "program_search", "image_inspect",
    "memory_search", "learning_health_status", "memory_quality_report", "memory_privacy_review", "artifact_ground",
    "web_search", "web_fetch", "weather_lookup", "approximate_location_lookup",
    "status", "diagnostics",
})


def _start_agent_checklist(prompt: str, project: str, read_only: bool):
    action = "Perform the requested analysis" if read_only else "Implement the requested changes"
    items = [
        "Inspect relevant folders, files, programs, and context",
        action,
        "Validate results with grounded checks",
        "Produce a concise evidence-backed end report",
    ]
    title = re.sub(r"\s+", " ", prompt or "Agent work").strip()[:100] or "Agent work"
    created = checklist_create(
        title=title,
        items_json=json.dumps(items),
        project=project,
        owner="sonder-agent",
        priority=1,
    )
    if created.startswith("ERROR:"):
        return "", {}
    match = re.search(r"sonder checklist ([0-9a-f]+)", created)
    checklist_id = match.group(1) if match else ""
    states = {}
    if checklist_id:
        checklist_update(checklist_id, "1", "in_progress", "agent started inspection")
        states[1] = "in_progress"
    return checklist_id, states


def _agent_checklist_mark(checklist_id, states, item, status, note):
    if not checklist_id or states.get(item) == status:
        return
    result = checklist_update(checklist_id, str(item), status, note)
    if not result.startswith("ERROR:"):
        states[item] = status


def _agent_checklist_fail(checklist_id, states, reason, item=1):
    """Leave persistent, honest task state when an agent exits early."""
    _agent_checklist_mark(checklist_id, states, item, "blocked", reason)
    _agent_checklist_mark(checklist_id, states, 4, "done", "failure included in end report")


def _agent_impl(
    prompt: str,
    tier: str = "code",
    max_steps: int = 6,
    allow_web: bool = True,
    require_file_evidence: bool = False,
    read_only: bool = False,
    include_evidence: bool = False,
    auto_checklist: bool = False,
    project: str = "",
    required_tool_names=(),
    allow_location: bool = False,
    tool_allowlist=None,
    tool_policy=None,
    return_host_receipt: bool = False,
    system: str | None = None,
) -> str:
    """Run a Claude-like local agent loop that can call tools.

    The model chooses one JSON tool call at a time, receives the observation,
    and continues until it returns {"final": "..."} or max_steps is reached.
    Tools include code execution, memory search, workflows, diagnostics, and
    public web search/fetch/weather when allow_web=True and web tools are on.
    """
    _maybe_live_reload()
    max_steps = _safe_limit(max_steps, 6, 20)
    model, cloud, augment, tier_label = _serve_target(tier, None)
    if tier_label == "cloud-disabled":
        return _cloud_disabled_message()
    if tier_label is None:
        return "ERROR: unknown tier '%s'. Valid: sonder, %s." % (tier, _valid_tier_names())
    if model is None:
        return "ERROR: `sonder:latest` Ollama alias not found."
    system = _build_system(
        system or
        "You are a local tool-using coding agent. Inspect real workspace evidence before making claims. "
        "For action tasks, use tools instead of merely describing commands. Prefer workspace_inventory, directory_tree, "
        "text_search, file_read_range, and program_search for discovery; use guarded file tools for "
        "mutations; validate every mutation with workspace_run, script_run, file_read_range, "
        "image_inspect, artifact_verify, or another path-specific checker before returning final. "
        "After editing a script, run that exact path "
        "with script_run; an equivalent run_code snippet does not validate the on-disk file. "
        "Never invent tool results. "
        "Use web tools for current external information and cite fetched URLs in the final answer. "
        "Your final answer must lead with the outcome, mention changed paths and checks, and disclose failures.",
        False,
        "",
    )
    gen = _make_generate(model, system, 0.1, 1200, SESSION_NUM_CTX, cloud=cloud)
    observations = []
    file_evidence = False
    used_tool = False
    inspected = False
    mutated = False
    validation_attempted = False
    validation_ok = False
    mutations = []
    required_tools = frozenset(str(name) for name in required_tool_names if name)
    allowed_tools = (
        None if tool_allowlist is None
        else frozenset(str(name) for name in tool_allowlist if name)
    )
    used_tool_names = set()
    successful_web_calls = set()
    failed_call_counts = {}
    claim_review_requests = 0
    checklist_id, checklist_states = (
        _start_agent_checklist(prompt, project, read_only)
        if auto_checklist else ("", {})
    )
    transcript = "Task:\n%s\n\n%s" % (prompt, _agent_tool_help(read_only=read_only))
    if allowed_tools is not None:
        transcript += (
            "\n\nHOST TOOL ALLOWLIST (cannot be expanded by the model):\n- %s"
            % "\n- ".join(sorted(allowed_tools))
        )

    def finish_final(final):
        final = str(final or "")
        if auto_checklist:
            _agent_checklist_mark(
                checklist_id, checklist_states, 1, "done", "workspace evidence inspected",
            )
            _agent_checklist_mark(
                checklist_id, checklist_states, 2, "done",
                "requested work completed" if mutated else "analysis completed without file mutation",
            )
            validation_status = "done" if (validation_ok or not mutated) else "blocked"
            _agent_checklist_mark(
                checklist_id, checklist_states, 3, validation_status,
                "grounded validation passed" if validation_ok else (
                    "no mutation required" if not mutated else "validation did not pass"
                ),
            )
            _agent_checklist_mark(
                checklist_id, checklist_states, 4, "done", "end report prepared",
            )
        if auto_checklist and mutated and not validation_ok:
            final = (
                "VALIDATION_FAILED: workspace changes were not successfully validated.\n\n"
                + final
            )
        if include_evidence and observations:
            final += "\n\n=== TOOL EVIDENCE ===\n" + "\n\n".join(observations)
        activity_tracker.set_result_summary(
            final.splitlines()[0] if final else "agent completed"
        )
        if return_host_receipt:
            return autopilot_controller.HostTaskResult(
                output=final,
                tools=tuple(sorted(used_tool_names)),
                mutation_observed=mutated,
                validation_attempted=validation_attempted,
                validation_passed=validation_ok,
            )
        return final

    def run_claim_review_action(review, review_number):
        nonlocal file_evidence, inspected, used_tool
        tool_name = str(review.get("tool") or "")
        tool_args = review.get("args") or {}
        policy_error = ""
        if tool_name not in _AGENT_CLAIM_REVIEW_TOOLS:
            policy_error = "ERROR: HOST CLAIM REVIEW: no approved evidence tool was supplied."
        elif allowed_tools is not None and tool_name not in allowed_tools:
            policy_error = (
                "ERROR: HOST CLAIM REVIEW: tool '%s' is outside this run's allowlist."
                % tool_name
            )
        if not policy_error and tool_policy is not None:
            policy_error = str(tool_policy(tool_name, tool_args) or "")
        if not policy_error:
            policy_error = _repository_read_only_error(tool_name, tool_args)
        if policy_error:
            observation_text = policy_error
        else:
            observation_text = str(_agent_dispatch_observed(
                tool_name,
                tool_args,
                allow_web=False,
                read_only=True,
            ))
        tool_ok = _agent_tool_observation_ok(tool_name, observation)
        if tool_ok:
            used_tool = True
            used_tool_names.add(tool_name)
            file_evidence = True
            inspected = True
            if auto_checklist:
                _agent_checklist_mark(
                    checklist_id,
                    checklist_states,
                    1,
                    "done",
                    "%s completed for negative-claim review" % tool_name,
                )
        return (
            "host claim review %d tool=%s reason=%s\n%s"
            % (
                review_number,
                tool_name or "(missing)",
                review.get("reason", ""),
                observation_text[:6000],
            )
        )

    for step in range(1, max_steps + 1):
        step_prompt = transcript
        if observations:
            step_prompt += "\n\n" + _agent_observation_prompt(observations)
        step_prompt += "\n\nChoose the next tool call or final answer."
        decision, raw, decision_error = _agent_generate_decision(gen, step_prompt)
        if decision is None:
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "model returned an invalid tool decision", 1,
                )
            return "ERROR: could not parse agent decision at step %d: %s\nraw=%s" % (
                step, decision_error, raw[:1000])
        if "final" in decision:
            final = str(decision.get("final") or "")
            if required_tools and not (required_tools & used_tool_names):
                if step < max_steps:
                    observations.append(
                        "HOST REQUIREMENT: use at least one successful tool from: %s."
                        % ", ".join(sorted(required_tools))
                    )
                    continue
                return (
                    "ERROR: agent reached max_steps=%d without using a required "
                    "web tool (%s)." % (
                        max_steps, ", ".join(sorted(required_tools)),
                    )
                )
            if auto_checklist and not used_tool and step < max_steps:
                observations.append(
                    "HOST REQUIREMENT: use at least one relevant inspection or execution tool before final."
                )
                continue
            if auto_checklist and mutated and not validation_ok and step < max_steps:
                _agent_checklist_mark(
                    checklist_id, checklist_states, 2, "done", "mutations completed",
                )
                _agent_checklist_mark(
                    checklist_id, checklist_states, 3, "in_progress", "validation required before final",
                )
                observations.append(
                    "HOST REQUIREMENT: files changed but no grounded validation has passed. "
                    "Run or retry an exact validator now."
                )
                continue
            if _AGENT_NEGATIVE_CLAIM_RE.search(final):
                claim_review = _agent_negative_claim_review(
                    prompt, final, observations, model, cloud=cloud,
                )
                if claim_review["decision"] == "continue":
                    claim_review_requests += 1
                    if claim_review_requests <= 2:
                        observations.append(
                            "HOST CLAIM REVIEW: %s\n%s"
                            % (
                                claim_review["reason"],
                                run_claim_review_action(
                                    claim_review, claim_review_requests,
                                ),
                            )
                        )
                        continue
                    if auto_checklist:
                        _agent_checklist_fail(
                            checklist_id,
                            checklist_states,
                            "negative existence claim lacked exact evidence",
                            1,
                        )
                    return "%s: %s\n\n%s" % (
                        master_orchestrator.EVIDENCE_REQUIRED,
                        claim_review["reason"],
                        "\n\n".join(observations),
                    )
            if require_file_evidence and not file_evidence:
                if auto_checklist:
                    _agent_checklist_fail(
                        checklist_id, checklist_states,
                        "required workspace evidence was not collected", 1,
                    )
                detail = "\n\n" + "\n\n".join(observations) if observations else ""
                return master_orchestrator.EVIDENCE_REQUIRED + detail
            return finish_final(final)
        tool_name = decision.get("tool")
        if not tool_name:
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "model decision omitted both tool and final", 1,
                )
            return "ERROR: agent decision missing 'tool' or 'final': %s" % decision
        tool_args = decision.get("args", {})
        call_signature = (
            str(tool_name),
            json.dumps(tool_args, sort_keys=True, ensure_ascii=False, default=str),
        )
        prior_identical_failures = failed_call_counts.get(call_signature, 0)
        if prior_identical_failures >= 3:
            if auto_checklist:
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "model repeated an unchanged failing tool call", 2,
                )
            return (
                "ERROR: agent repeated the same unsuccessful tool call %d times: %s. "
                "Change the arguments, inspect the error, or choose a recovery tool.\n\n%s"
                % (
                    prior_identical_failures,
                    tool_name,
                    "\n\n".join(observations),
                )
            )
        policy_error = ""
        if allowed_tools is not None and tool_name not in allowed_tools:
            policy_error = (
                "ERROR: HOST POLICY: tool '%s' is outside this autonomous run's allowlist."
                % tool_name
            )
        if not policy_error and tool_policy is not None:
            policy_error = str(tool_policy(tool_name, tool_args) or "")
        if not policy_error and read_only:
            policy_error = _repository_read_only_error(tool_name, tool_args)
        if (
            auto_checklist
            and tool_name in _WORK_MUTATION_TOOLS
            and not inspected
            and not policy_error
        ):
            policy_error = (
                "ERROR: HOST REQUIREMENT: inspect relevant workspace evidence "
                "before making a mutation."
            )
        if prior_identical_failures >= 2:
            observation = (
                "ERROR: HOST NO-PROGRESS: this exact tool call already failed twice. "
                "It was not run again. Change its arguments, inspect/discover the "
                "correct target, or choose a different recovery tool."
            )
        elif tool_name in {
            "web_search", "web_fetch", "weather_lookup",
            "approximate_location_lookup",
        } and call_signature in successful_web_calls:
            observation = (
                "ERROR: HOST REQUIREMENT: this identical web tool call already "
                "succeeded; use its existing observation or choose a different call."
            )
        elif policy_error:
            observation = policy_error
        elif read_only and tool_name in {"command_registry_list", "tool_manifest"}:
            observation = _agent_tool_help(read_only=True)
        else:
            dispatch_options = {
                "allow_web": allow_web,
                "read_only": read_only,
            }
            if allow_location:
                dispatch_options["allow_location"] = True
            observation = _agent_dispatch_observed(
                tool_name, tool_args, **dispatch_options,
            )
        observation_text = str(observation)
        tool_ok = _agent_tool_observation_ok(tool_name, observation)
        if tool_ok:
            failed_call_counts.pop(call_signature, None)
            if tool_name in _WORK_MUTATION_TOOLS:
                # A real state change makes prior validation failures retryable.
                failed_call_counts.clear()
        else:
            failed_call_counts[call_signature] = prior_identical_failures + 1
            recovery = (
                "HOST RECOVERY: do not repeat this exact failed call unchanged. "
                "Inspect the error and change the target, arguments, or tool."
            )
            if tool_name == "script_run":
                recovery += (
                    " Use script_search/file_find to locate a real script, or use "
                    "workspace_run with an approved interpreter and explicit argv."
                )
            observation_text += "\n" + recovery
        used_tool = used_tool or tool_ok
        if tool_ok:
            used_tool_names.add(str(tool_name))
            if tool_name in {
                "web_search", "web_fetch", "weather_lookup",
                "approximate_location_lookup",
            }:
                successful_web_calls.add(call_signature)
        if tool_name in {
            "workspace_inventory", "directory_tree", "file_read", "file_read_range", "file_find",
            "text_search", "script_search", "image_inspect",
        } and tool_ok:
            file_evidence = True
        if auto_checklist and tool_name in _WORK_INSPECTION_TOOLS and tool_ok:
            inspected = True
            _agent_checklist_mark(
                checklist_id, checklist_states, 1, "done", "%s completed" % tool_name,
            )
            _agent_checklist_mark(
                checklist_id, checklist_states, 2, "in_progress", "working from inspected evidence",
            )
        mutation_happened = (
            tool_name in _WORK_MUTATION_TOOLS
            and tool_ok
            and not (tool_name == "file_delete" and tool_args.get("dry_run", True))
            and not (
                tool_name in {
                    "memory_quality_repair", "memory_privacy_repair",
                    "memory_embedding_backfill",
                }
                and not tool_args.get("apply", False)
            )
        )
        if mutation_happened:
            mutated = True
            validation_attempted = False
            validation_ok = False
            record = _agent_mutation_record(tool_name, tool_args)
            if record not in mutations:
                mutations.append(record)
            if auto_checklist and mutated:
                _agent_checklist_mark(
                    checklist_id, checklist_states, 1, "done", "inspection completed before mutation",
                )
                _agent_checklist_mark(
                    checklist_id, checklist_states, 2, "in_progress", "%s changed workspace state" % tool_name,
                )
        if tool_name in _WORK_VALIDATION_TOOLS:
            validation_attempted = True
            validation_covered = tool_ok and _agent_validation_covers(
                tool_name, tool_args, mutations, observation_text,
            )
            # The latest host-observed validator decides current validity. A
            # later failing/bad-coverage check must invalidate an earlier pass.
            validation_ok = validation_covered
            if mutated and tool_ok and not validation_covered:
                observation_text += (
                    "\nHOST VALIDATION: this check did not cover the changed on-disk path(s). "
                    "Run the edited script, a workspace test/build, or a path-specific verifier."
                )
            if auto_checklist and mutated:
                _agent_checklist_mark(
                    checklist_id, checklist_states, 2, "done", "implementation phase complete",
                )
                _agent_checklist_mark(
                    checklist_id, checklist_states, 3,
                    "done" if validation_covered else "blocked",
                    "%s %s" % (
                        tool_name,
                        "passed and covered changed paths"
                        if validation_covered else "did not validate changed paths",
                    ),
                )
        observations.append(
            "step %d tool=%s reason=%s\n%s" % (
                step,
                tool_name,
                decision.get("reason", ""),
                observation_text[:6000],
            )
        )
    final = ""
    while True:
        final_prompt = transcript
        if observations:
            final_prompt += "\n\n" + _agent_observation_prompt(observations)
        final_prompt += (
            "\n\nHOST FINALIZATION ONLY: the tool-step budget is exhausted. Do not call "
            "another tool. Synthesize a concise grounded result from the observations, "
            "disclose unresolved errors or checks, and return exactly "
            '{"final":"answer"}.'
        )
        final_decision, raw, final_error = _agent_generate_decision(
            gen, final_prompt, require_final=True,
        )
        if final_decision is None:
            if auto_checklist:
                active_item = 3 if validation_attempted else 2 if mutated else 1
                _agent_checklist_fail(
                    checklist_id, checklist_states,
                    "agent could not synthesize a final answer after max_steps",
                    active_item,
                )
            return (
                "ERROR: agent reached max_steps=%d and finalization failed: %s\n"
                "raw=%s\n\n%s"
                % (max_steps, final_error, raw[:1000], "\n\n".join(observations))
            )
        final = str(final_decision.get("final") or "")
        if not _AGENT_NEGATIVE_CLAIM_RE.search(final):
            break
        claim_review = _agent_negative_claim_review(
            prompt, final, observations, model, cloud=cloud,
        )
        if claim_review["decision"] == "accept":
            break
        claim_review_requests += 1
        if claim_review_requests <= 2:
            observations.append(
                "HOST CLAIM REVIEW: %s\n%s"
                % (
                    claim_review["reason"],
                    run_claim_review_action(claim_review, claim_review_requests),
                )
            )
            continue
        if auto_checklist:
            _agent_checklist_fail(
                checklist_id,
                checklist_states,
                "negative existence claim lacked exact evidence at finalization",
                1,
            )
        return "%s: %s\n\n%s" % (
            master_orchestrator.EVIDENCE_REQUIRED,
            claim_review["reason"],
            "\n\n".join(observations),
        )
    if required_tools and not (required_tools & used_tool_names):
        return (
            "ERROR: agent reached max_steps=%d without using a required web tool (%s)."
            % (max_steps, ", ".join(sorted(required_tools)))
        )
    if auto_checklist and not used_tool:
        _agent_checklist_fail(
            checklist_id, checklist_states,
            "agent exhausted tool steps without successful evidence", 1,
        )
        return "ERROR: agent reached max_steps=%d without successful tool evidence." % max_steps
    if require_file_evidence and not file_evidence:
        if auto_checklist:
            _agent_checklist_fail(
                checklist_id, checklist_states,
                "required workspace evidence was not collected", 1,
            )
        detail = "\n\n" + "\n\n".join(observations) if observations else ""
        return master_orchestrator.EVIDENCE_REQUIRED + detail
    return finish_final(final)


@mcp.tool()
def agent(
    prompt: str,
    tier: str = "code",
    max_steps: int = 6,
    allow_web: bool = True,
    project: str = "",
    checklist: bool = True,
    allow_location: bool = False,
) -> str:
    """Run a visible local tool-using agent loop with checklist/reporting."""
    nested = activity_tracker.current() is not None
    with activity_tracker.response_span(
        "agent:%s" % (tier or "code"),
        prompt,
        surface="agent",
        model=tier,
        project=project,
    ):
        result = _agent_impl(
            prompt,
            tier=tier,
            max_steps=max_steps,
            allow_web=allow_web,
            auto_checklist=bool(checklist),
            project=project,
            allow_location=bool(allow_location),
        )
    response = activity_tracker.current() if nested else activity_tracker.latest()
    if nested and response:
        response["status"] = "complete"
        response["elapsed_ms"] = int((time.time() - response["started_at"]) * 1000)
    return "%s\n\n%s\n\n%s" % (
        result.rstrip(),
        activity_tracker.format_end_report(response),
        activity_tracker.format_response(response),
    )


@mcp.tool()
def workbench_agent(
    prompt: str,
    tier: str = "auto",
    max_steps: int = 12,
    allow_web: bool = True,
    project: str = "",
    allow_location: bool = False,
) -> str:
    """Execute local work with guarded tools, checklist, validation, and report."""
    _maybe_live_reload()
    tier = _runtime_lane_tier("workbench", tier)
    return agent(
        prompt=prompt,
        tier=tier,
        max_steps=max_steps,
        allow_web=allow_web,
        project=project,
        checklist=True,
        allow_location=allow_location,
    )


_AUTOPILOT_OBSERVE_TOOLS = frozenset({
    "file_policy", "workspace_inventory", "directory_tree", "file_find",
    "file_read", "file_read_range", "text_search", "script_search",
    "program_search", "image_inspect", "memory_search", "web_search",
    "web_fetch", "weather_lookup", "status", "diagnostics",
    "context_health", "learning_health_status", "memory_quality_report", "system_improvement_report", "artifact_ground",
})
_AUTOPILOT_WORKSPACE_TOOLS = _AUTOPILOT_OBSERVE_TOOLS | frozenset({
    "directory_create", "file_write", "file_edit", "workspace_run",
    "script_run", "run_code", "run_project", "ground_artifact", "artifact_ground",
    "artifact_generate", "artifact_verify", "game_reference_suite",
    "game_generate_and_test",
})
_AUTOPILOT_RUNNERS = frozenset({
    "python", "python.exe", "py", "py.exe", "pytest", "pytest.exe",
    "node", "node.exe", "dart", "dart.exe", "flutter", "flutter.bat",
    "cmake", "cmake.exe", "ctest", "ctest.exe", "ninja", "ninja.exe",
    "msbuild", "msbuild.exe", "dotnet", "dotnet.exe", "cl", "cl.exe",
    "g++", "g++.exe", "clang++", "clang++.exe", "cargo", "cargo.exe",
})
_AUTOPILOT_SCRIPT_SUFFIXES = frozenset({".py", ".js", ".dart", ".exe", ".com"})
_AUTOPILOT_MUTATION_EVIDENCE = frozenset({
    "directory_create", "file_write", "file_edit", "artifact_generate",
    "game_generate_and_test",
})


def _autopilot_allowed_tools(run: dict) -> frozenset:
    return (
        _AUTOPILOT_OBSERVE_TOOLS
        if run.get("policy") == "observe"
        else _AUTOPILOT_WORKSPACE_TOOLS
    )


def _autopilot_command_programs(value) -> list[str]:
    if value in (None, ""):
        return []
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return ["(invalid)"]
    if isinstance(payload, dict):
        payload = payload.get("commands") or []
    if not isinstance(payload, list):
        return ["(invalid)"]
    programs = []
    for item in payload:
        command = item.get("cmd") if isinstance(item, dict) else item
        if not isinstance(command, list) or not command:
            return ["(invalid)"]
        programs.append(os.path.basename(str(command[0])).lower())
    return programs


def _autopilot_tool_policy(run: dict):
    """Return an argument-aware policy that models cannot override."""
    def check(tool_name, args):
        args = args if isinstance(args, dict) else {}
        if any(args.get(name) for name in ("token", "approval", "extra_roots")):
            return "ERROR: HOST POLICY: autonomous runs cannot use bypass credentials or extra roots."
        if tool_name == "workspace_run":
            program = os.path.basename(str(args.get("program", ""))).lower()
            if program not in _AUTOPILOT_RUNNERS:
                return (
                    "ERROR: HOST POLICY: executable '%s' is not approved for autonomous runs."
                    % (program or "(missing)")
                )
        if tool_name == "script_run":
            suffix = os.path.splitext(str(args.get("path", "")))[1].lower()
            if suffix not in _AUTOPILOT_SCRIPT_SUFFIXES:
                return (
                    "ERROR: HOST POLICY: autonomous script execution only accepts: %s."
                    % ", ".join(sorted(_AUTOPILOT_SCRIPT_SUFFIXES))
                )
        if tool_name == "run_code":
            language = str(args.get("language", "python")).strip().lower()
            if language not in {"python", "js", "javascript", "cpp", "c++", "csharp", "cs"}:
                return "ERROR: HOST POLICY: this generated-code language is not approved."
        if tool_name == "run_project":
            programs = _autopilot_command_programs(
                args.get("commands_json", args.get("commands", []))
            )
            rejected = [name for name in programs if name not in _AUTOPILOT_RUNNERS]
            if rejected:
                return (
                    "ERROR: HOST POLICY: project command '%s' is not approved."
                    % rejected[0]
                )
        return ""
    return check


def _autopilot_json_model(run: dict, role: str, prompt: str, validator) -> dict:
    run_tier = autopilot_controller.normalize_tier(run.get("tier", "code"))
    if role == "reviewer":
        tier = runtime_policy.route_tier(
            "review", _refresh_runtime_policy(create=True), fallback=run_tier,
        )
    else:
        tier = run_tier
    model, cloud, _augment, tier_label = _serve_target(tier, False)
    if model is None or cloud or tier_label not in autopilot_controller.LOCAL_TIERS:
        raise RuntimeError("autopilot requires an available local model tier")
    system = _build_system(
        "You are Sonder's bounded autonomous %s. Return exactly one JSON "
        "object, with no markdown or private chain-of-thought. Make concrete "
        "decisions from the supplied state. Never expand policy, tools, roots, "
        "budgets, or completion rules." % role,
        False,
        "",
    )
    gen = _make_generate(model, system, 0.05, 1800, SESSION_NUM_CTX, cloud=False)
    correction = ""
    last_error = "invalid JSON"
    for _attempt in range(2):
        raw = gen(prompt + correction)
        try:
            payload = _extract_agent_json(raw)
            validator(payload)
            return payload
        except (TypeError, ValueError) as exc:
            last_error = str(exc)
            correction = (
                "\n\nHOST SCHEMA ERROR: %s\nReturn a corrected JSON object only."
                % last_error
            )
    raise ValueError("%s model failed JSON/schema validation: %s" % (role, last_error))


def _autopilot_plan_model(run: dict) -> dict:
    allowed = sorted(_autopilot_allowed_tools(run))
    max_tasks = int(run.get("max_tasks") or 12)
    reserve = (
        min(int(run.get("max_replans") or 0), max(0, max_tasks - 3))
        if run.get("adaptive", True) else 0
    )
    initial_limit = max(3, min(6, max_tasks - reserve))
    prompt = (
        "Create a short executable plan for this autonomous goal.\n"
        "Objective: {objective}\nProject: {project}\nPolicy: {policy}\n"
        "Web: {web}\nAdaptive checkpoints: {adaptive}\n"
        "Initial task limit: {initial_limit}\nOverall task ledger limit: {max_tasks}\n"
        "Replan budget: {max_replans}\nAllowed tools: {tools}\n\n"
        "Use measurable success criteria. Order inspection before mutation and "
        "always finish with grounded validation. Under observe policy, do not "
        "create implementation tasks. Keep the initial plan within its smaller "
        "limit so adaptive review has room to replace stale pending work. JSON schema:\n"
        '{{"summary":"...","success_criteria":["..."],"tasks":['
        '{{"title":"...","kind":"inspect|research|implement|validate|report",'
        '"instruction":"specific bounded action"}}]}}'
    ).format(
        objective=run.get("objective", ""),
        project=run.get("project") or "default",
        policy=run.get("policy", "workspace"),
        web="on" if run.get("allow_web") else "off",
        adaptive="on" if run.get("adaptive", True) else "off",
        initial_limit=initial_limit,
        max_tasks=max_tasks,
        max_replans=run.get("max_replans", 0),
        tools=", ".join(allowed),
    )

    def validate(payload):
        normalized = autopilot_controller.normalize_plan(
            payload, run.get("objective", ""), max_tasks,
        )
        if len(normalized["tasks"]) > initial_limit:
            raise ValueError(
                "initial plan exceeds the %d-task adaptive planning limit"
                % initial_limit
            )
        if run.get("policy") == "observe" and any(
            task.get("kind") == "implement" for task in normalized["tasks"]
        ):
            raise ValueError("observe policy cannot contain implementation tasks")

    return _autopilot_json_model(run, "planner", prompt, validate)


def _autopilot_review_model(run: dict, issue: str) -> dict:
    ledger = []
    for task in run.get("plan") or []:
        ledger.append({
            "id": task.get("id"),
            "kind": task.get("kind"),
            "title": task.get("title"),
            "instruction": task.get("instruction"),
            "status": task.get("status"),
            "attempts": task.get("attempts"),
            "result": autopilot_controller._first_line(
                task.get("output"), task.get("error", ""),
            ),
            "evidence_actions": autopilot_controller._evidence_actions(
                task.get("output", ""), limit=6,
            ),
        })
    prompt = (
        "Review the bounded run and select the next decision.\n"
        "Objective: %s\nHost gate/issue: %s\nFailures: %s/%s\n"
        "Task budget: %s/%s\nAdaptive checkpoints: %s\nReplans: %s/%s\n"
        "Ledger: %s\n\n"
        "Use complete only when the host gate says all requirements passed. "
        "At an adaptive checkpoint, use continue when the pending plan remains "
        "correct, replan only when new evidence makes it stale, or pause when "
        "operator judgment is genuinely required. Use retry only after a failure. "
        "At every adaptive checkpoint, assess every pending task by ID. A task is "
        "stale when completed evidence contradicts its premise or says its work is "
        "already unnecessary. A stale task forbids continue: choose replan, omit "
        "the contradicted work, and retain necessary validation/reporting. "
        "The host preserves tasks marked keep and supersedes only tasks marked "
        "stale. Every replan must include only necessary new replacement tasks; "
        "tasks may be empty when removing stale work is sufficient and a kept "
        "validation task remains. JSON schema:\n"
        '{"decision":"complete|continue|retry|replan|pause","reason":"...",'
        '"instruction":"corrected retry instruction or empty",'
        '"pending_assessment":[{"id":"task-00","verdict":"keep|stale",'
        '"reason":"evidence comparison"}],'
        '"tasks":[{"title":"...","kind":"inspect|research|implement|validate|report",'
        '"instruction":"..."}]}'
    ) % (
        run.get("objective", ""), issue, run.get("failures", 0),
        run.get("max_failures", 3), len(run.get("plan") or []),
        run.get("max_tasks", 12), run.get("checkpoints", 0),
        run.get("replans", 0), run.get("max_replans", 0),
        json.dumps(ledger, ensure_ascii=False),
    )

    is_checkpoint = str(issue or "").startswith("adaptive checkpoint")
    pending_ids = {
        str(task.get("id"))
        for task in (run.get("plan") or [])
        if task.get("status") == "pending" and task.get("id")
    }

    def validate(payload):
        normalized = autopilot_controller.normalize_review(payload)
        if not is_checkpoint:
            return
        if normalized["decision"] not in {"continue", "replan", "pause"}:
            raise ValueError(
                "adaptive checkpoint decision must be continue, replan, or pause"
            )
        assessments = payload.get("pending_assessment") or []
        if not isinstance(assessments, list):
            raise ValueError("adaptive pending assessment must be a JSON list")
        assessed = {}
        for item in assessments:
            if not isinstance(item, dict):
                raise ValueError("each pending assessment must be a JSON object")
            task_id = str(item.get("id") or "").strip()
            verdict = str(item.get("verdict") or "").strip().lower()
            if task_id in assessed:
                raise ValueError("adaptive review assessed a pending task twice")
            if verdict not in {"keep", "stale"}:
                raise ValueError("pending task verdict must be keep or stale")
            assessed[task_id] = verdict
        unknown = set(assessed) - pending_ids
        if unknown:
            raise ValueError(
                "adaptive review assessed unknown pending tasks: %s"
                % ", ".join(sorted(unknown))
            )
        for task_id in sorted(pending_ids - set(assessed)):
            assessments.append({
                "id": task_id,
                "verdict": "keep",
                "reason": "host default: reviewer did not mark this pending task stale",
            })
            assessed[task_id] = "keep"
        payload["pending_assessment"] = assessments
        stale = {task_id for task_id, verdict in assessed.items() if verdict == "stale"}
        if stale and normalized["decision"] == "continue":
            raise ValueError("continue is invalid while a pending task is stale")
        if normalized["decision"] == "replan":
            if not stale:
                raise ValueError("replan requires at least one stale pending task")

    return _autopilot_json_model(
        run,
        "reviewer",
        prompt,
        validate,
    )


def _autopilot_evidence_has(output: str, tools) -> bool:
    names = {str(name) for name in tools}
    return any(
        match.group(1) in names
        for match in re.finditer(r"\btool=([A-Za-z0-9_]+)", str(output or ""))
    )


def _autopilot_work_model(
    run: dict, task: dict, prior: str
) -> autopilot_controller.HostTaskResult | str:
    allowed = _autopilot_allowed_tools(run)
    prompt = (
        "Autopilot objective: {objective}\n"
        "Current bounded task: {task_id} [{kind}] {title}\n"
        "Instruction: {instruction}\n"
        "Success criteria:\n{criteria}\n"
        "Prior task evidence:\n{prior}\n\n"
        "Complete only this task using host tools. Inspect before mutation, do "
        "not broaden scope, and validate every persistent change. If blocked, "
        "report the exact blocker; do not claim success."
    ).format(
        objective=run.get("objective", ""),
        task_id=task.get("id", ""),
        kind=task.get("kind", ""),
        title=task.get("title", ""),
        instruction=task.get("instruction", ""),
        criteria="\n".join("- " + item for item in (run.get("criteria") or [])),
        prior=prior or "(none yet)",
    )
    output = _agent_impl(
        prompt,
        tier=run.get("tier", "code"),
        max_steps=12,
        allow_web=bool(run.get("allow_web")),
        require_file_evidence=False,
        read_only=run.get("policy") == "observe",
        include_evidence=True,
        auto_checklist=True,
        project=run.get("project", ""),
        allow_location=False,
        tool_allowlist=allowed,
        tool_policy=_autopilot_tool_policy(run),
        return_host_receipt=True,
    )
    return output


def _autopilot_heartbeat(run_id: str, owner_id: str, stop: threading.Event) -> None:
    while not stop.wait(30):
        if not autopilot_store.heartbeat(run_id, owner_id):
            return


def _execute_autopilot(run_id: str, *, max_cycles=12, plan_only=False) -> dict:
    owner_id = "auto-%s-%s" % (os.getpid(), time.time_ns())
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_autopilot_heartbeat,
        args=(run_id, owner_id, stop),
        name="sonder-autopilot-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        return autopilot_controller.execute_run(
            run_id,
            owner_id,
            owner_pid=os.getpid(),
            plan_fn=_autopilot_plan_model,
            work_fn=_autopilot_work_model,
            review_fn=_autopilot_review_model,
            max_cycles=max_cycles,
            plan_only=plan_only,
        )
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def _autopilot_thread_main(run_id: str, max_cycles: int, plan_only: bool) -> None:
    run = autopilot_store.get_run(run_id) or {}
    try:
        with activity_tracker.response_span(
            "autopilot:%s" % run_id,
            run.get("objective", ""),
            surface="autopilot",
            model=run.get("tier", "code"),
            project=run.get("project", ""),
        ):
            result = _execute_autopilot(
                run_id, max_cycles=max_cycles, plan_only=plan_only,
            )
            activity_tracker.set_result_summary(
                "%s: %s" % (result.get("status", "unknown"), result.get("summary", ""))
            )
    except Exception as exc:
        # execute_run persists model/tool failures whenever it owns the run. A
        # claim conflict is observable but must never steal or overwrite state.
        with contextlib.suppress(Exception):
            activity_tracker.set_result_summary("autopilot worker: %s" % exc)
    finally:
        with _AUTOPILOT_THREADS_LOCK:
            current = _AUTOPILOT_THREADS.get(run_id)
            if current is threading.current_thread():
                _AUTOPILOT_THREADS.pop(run_id, None)


def _launch_autopilot(run_id: str, max_cycles=12, plan_only=False) -> bool:
    with _AUTOPILOT_THREADS_LOCK:
        current = _AUTOPILOT_THREADS.get(run_id)
        if current is not None and current.is_alive():
            return False
        thread = threading.Thread(
            target=_autopilot_thread_main,
            args=(run_id, int(max_cycles), bool(plan_only)),
            name="sonder-autopilot-%s" % run_id,
            daemon=True,
        )
        _AUTOPILOT_THREADS[run_id] = thread
        thread.start()
        return True


@mcp.tool()
def autopilot_start(
    objective: str,
    project: str = "",
    tier: str = "auto",
    policy: str = "workspace",
    allow_web: bool = True,
    max_cycles: int = 12,
    max_failures: int = 3,
    max_tasks: int = 12,
    max_replans: int = 2,
    adaptive: bool = True,
    plan_only: bool = False,
    wait: bool = False,
) -> str:
    """Create and start a persistent, locally planned autonomous goal run."""
    _maybe_live_reload()
    try:
        tier = _runtime_lane_tier("autopilot", tier)
        tier = autopilot_controller.normalize_tier(tier)
        policy = autopilot_controller.normalize_policy(policy)
        run = autopilot_store.create_run(
            objective,
            project=project,
            tier=tier,
            policy=policy,
            allow_web=bool(allow_web),
            max_failures=max_failures,
            max_tasks=max_tasks,
            max_replans=max_replans,
            adaptive=bool(adaptive),
        )
        if wait:
            run = _execute_autopilot(
                run["id"], max_cycles=max_cycles, plan_only=plan_only,
            )
            return autopilot_controller.format_run(run)
        launched = _launch_autopilot(
            run["id"], max_cycles=max_cycles, plan_only=plan_only,
        )
    except (OSError, RuntimeError, ValueError, autopilot_controller.AutopilotError) as exc:
        return "ERROR: %s" % exc
    prefix = "autopilot plan started" if plan_only else "autopilot started"
    if not launched:
        prefix = "autopilot already active"
    return "%s\n%s\n  use /autopilot status %s" % (
        prefix, autopilot_controller.format_run(run, include_report=False), run["id"],
    )


@mcp.tool()
def autopilot_resume(
    run_id: str,
    max_cycles: int = 12,
    wait: bool = False,
) -> str:
    """Explicitly resume a paused, blocked, ready, or interrupted run."""
    _maybe_live_reload()
    run = autopilot_store.get_run(run_id)
    if not run:
        return "ERROR: no unambiguous autopilot run matches '%s'." % run_id
    if run.get("status") not in autopilot_store.RESUMABLE_STATUSES:
        return "ERROR: run %s is %s and cannot be resumed." % (run["id"], run.get("status"))
    try:
        if wait:
            return autopilot_controller.format_run(
                _execute_autopilot(run["id"], max_cycles=max_cycles),
            )
        launched = _launch_autopilot(run["id"], max_cycles=max_cycles)
    except (OSError, RuntimeError, ValueError, autopilot_controller.AutopilotError) as exc:
        return "ERROR: %s" % exc
    return "%s\n%s" % (
        "autopilot resumed" if launched else "autopilot already active",
        autopilot_controller.format_run(run, include_report=False),
    )


@mcp.tool()
def autopilot_pause(run_id: str) -> str:
    """Request a cooperative pause at the next host checkpoint."""
    _maybe_live_reload()
    run = autopilot_store.request_pause(run_id)
    return (
        autopilot_controller.format_run(run, include_report=False)
        if run else "ERROR: no unambiguous autopilot run matches '%s'." % run_id
    )


@mcp.tool()
def autopilot_cancel(run_id: str) -> str:
    """Request cancellation; an active task result is discarded."""
    _maybe_live_reload()
    run = autopilot_store.request_cancel(run_id)
    return (
        autopilot_controller.format_run(run, include_report=False)
        if run else "ERROR: no unambiguous autopilot run matches '%s'." % run_id
    )


@mcp.tool()
def autopilot_status(run_id: str = "", include_finished: bool = True) -> str:
    """Inspect one persistent autonomous run or the controller ledger."""
    _maybe_live_reload()
    if run_id.strip():
        return autopilot_controller.format_run(autopilot_store.get_run(run_id))
    return autopilot_controller.format_snapshot(
        autopilot_controller.snapshot(include_finished=include_finished),
    )


def _execution_route_model(prompt: str, project: str = "") -> dict:
    """Let a local model choose only foreground workbench or Autopilot."""
    router_tier = runtime_policy.route_tier(
        "router", _RUNTIME_POLICY or _refresh_runtime_policy(), fallback="fast",
    )
    model, cloud, _augment, tier_label = _serve_target(router_tier, False)
    if model is None or cloud or tier_label not in LOCAL_TIERS:
        raise RuntimeError("local execution router model is unavailable")
    system = _build_system(
        "You are Sonder's execution-mode router. Return exactly one JSON "
        "object and no prose or chain-of-thought. You may choose only workbench "
        "or autopilot and only fast, code, or general local tiers. Workbench is a "
        "foreground task with at most 12 tool steps. "
        "Autopilot is a persistent multi-stage goal with planning, evidence review, "
        "replanning, and validation. Never alter permissions, roots, tier mappings, "
        "or tools.",
        False,
        "",
    )
    route_prompt = (
        "Choose the smallest reliable execution mode for this developer-authorized "
        "work request. Prefer workbench when the task is self-contained and likely "
        "to finish in one bounded tool loop. Prefer autopilot when it has several "
        "dependent phases, needs durable progress, or requires discovery followed "
        "by implementation and independent validation. Choose fast only for tiny "
        "mechanical/read tasks, code for repository/code/tool work, and general for "
        "prose-heavy explanation or review.\n"
        "Project: %s\nRequest: %s\n"
        'JSON schema: {"mode":"workbench|autopilot","tier":"fast|code|general",'
        '"reason":"brief evidence-based reason","confidence":0.0}'
        % (project or "default", str(prompt or "")[:12000])
    )
    gen = _make_generate(model, system, 0.0, 240, 4096, cloud=False)
    correction = ""
    last_error = "invalid route decision"
    for _attempt in range(2):
        raw = gen(route_prompt + correction)
        try:
            payload = _extract_agent_json(raw)
            if not isinstance(payload, dict):
                raise ValueError("route decision must be a JSON object")
            mode = str(payload.get("mode") or "").strip().lower()
            if mode not in {"workbench", "autopilot"}:
                raise ValueError("route mode must be workbench or autopilot")
            selected_tier = str(payload.get("tier") or "").strip().lower()
            if selected_tier not in runtime_policy.LOCAL_TIERS:
                raise ValueError("route tier must be fast, code, or general")
            confidence = float(payload.get("confidence", 0.5))
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("route confidence must be between 0 and 1")
            reason = re.sub(r"\s+", " ", str(payload.get("reason") or "")).strip()
            if not reason:
                raise ValueError("route decision needs a brief reason")
            return {
                "mode": mode,
                "tier": selected_tier,
                "reason": reason[:500],
                "confidence": confidence,
            }
        except (TypeError, ValueError) as exc:
            last_error = str(exc)
            correction = (
                "\n\nHOST SCHEMA ERROR: %s. Return corrected JSON only."
                % last_error
            )
    raise ValueError("execution router model failed schema validation: %s" % last_error)


def _execution_route_header(
    mode: str,
    source: str,
    reason: str,
    confidence=None,
    tier: str = "",
) -> str:
    labels = {
        "workbench": "foreground workbench",
        "autopilot": "persistent Autopilot",
        "fleet": "hardware-bounded fleet",
        "deferred": "Autopilot deferred",
    }
    lines = [
        "sonder execution decision",
        "  mode: %s" % labels.get(mode, mode),
        "  source: %s" % source,
        "  reason: %s" % reason,
    ]
    if tier in runtime_policy.LOCAL_TIERS:
        lines.append("  tier: %s -> %s" % (tier, TIERS.get(tier, "(unmapped)")))
    if confidence is not None:
        lines.append("  confidence: %.0f%%" % (float(confidence) * 100.0))
    lines.append(
        "  boundary: local tiers and existing host permissions, roots, and budgets"
    )
    return "\n".join(lines)


def route_work_request(prompt: str, project: str = "") -> str | None:
    """Transparently route eligible natural work to a bounded execution lane."""
    _maybe_live_reload()
    decision = intents.classify_execution(prompt)
    if not decision:
        return None
    mode = decision["mode"]
    reason = decision["reason"]
    source = "explicit host cue" if mode in {"fleet", "autopilot"} else "host classifier"
    confidence = None
    selected_tier = runtime_policy.route_tier(
        mode if mode in runtime_policy.ROUTING_LANES else "workbench",
        _RUNTIME_POLICY,
        fallback="code",
    )
    if mode == "decide":
        try:
            routed = _execution_route_model(prompt, project=project)
            mode = routed["mode"]
            selected_tier = routed.get("tier") or runtime_policy.route_tier(
                mode, _RUNTIME_POLICY, fallback="code",
            )
            reason = routed["reason"]
            confidence = routed["confidence"]
            source = "bounded local mode model"
        except (OSError, RuntimeError, ValueError) as exc:
            mode = "autopilot"
            selected_tier = runtime_policy.route_tier(
                "autopilot", _RUNTIME_POLICY, fallback="code",
            )
            reason = (
                "compound-work fallback after local mode selection was unavailable: %s"
                % re.sub(r"\s+", " ", str(exc))[:240]
            )
            source = "host fallback"

    resolved_project = _resolve_project(project) or ""
    if mode == "fleet":
        output = master_orchestrate(
            task=prompt, mode="fleet", tier=selected_tier, learn=False,
        )
    elif mode == "workbench":
        output = workbench_agent(
            prompt=prompt,
            tier=selected_tier,
            max_steps=12,
            allow_web=True,
            project=resolved_project,
            allow_location=False,
        )
    else:
        active = []
        with contextlib.suppress(Exception):
            snapshot = autopilot_store.snapshot(include_finished=False, limit=20)
            active = [
                row for row in snapshot.get("runs", [])
                if row.get("status") in autopilot_store.ACTIVE_STATUSES
            ]
        if active:
            current = active[0]
            header = _execution_route_header(
                "deferred",
                source,
                "another Autopilot run is active; automatic routing will not start a concurrent run",
                confidence,
                selected_tier,
            )
            return "%s\n\nactive run: %s [%s] %s\nuse /autopilot status %s" % (
                header,
                current.get("id", "unknown"),
                current.get("status", "unknown"),
                current.get("objective", ""),
                current.get("id", ""),
            )
        output = autopilot_start(
            objective=prompt,
            project=resolved_project,
            tier=selected_tier,
            policy="workspace",
            allow_web=True,
            adaptive=True,
            plan_only=bool(decision.get("plan_only")),
            wait=False,
        )
    return "%s\n\n%s" % (
        _execution_route_header(mode, source, reason, confidence, selected_tier),
        output,
    )


def _runtime_installed_models() -> set[str]:
    payload = _get("/api/tags")
    names = set()
    for item in payload.get("models", []):
        name = str(item.get("name") or item.get("model") or "").strip()
        if name:
            names.add(name)
    return names


def _runtime_model_is_installed(model: str, installed) -> bool:
    requested = str(model or "").strip().casefold()
    available = {str(name or "").strip().casefold() for name in installed}
    if requested in available:
        return True
    # Ollama treats an omitted tag as :latest. Do not accept a different
    # installed tag merely because its repository/base name happens to match.
    if ":" not in requested:
        return "%s:latest" % requested in available
    if requested.endswith(":latest"):
        return requested[:-len(":latest")] in available
    return False


def runtime_policy_data() -> dict:
    policy = _refresh_runtime_policy(create=True)
    data = {
        **policy,
        "local_models": dict(policy["local_models"]),
        "routing": dict(policy["routing"]),
        "missing_models": [],
    }
    try:
        installed = _runtime_installed_models()
        data["missing_models"] = list(dict.fromkeys(
            model for model in data["local_models"].values()
            if not _runtime_model_is_installed(model, installed)
        ))
    except Exception as exc:
        data["inventory_error"] = "%s: %s" % (type(exc).__name__, exc)
    return data


@mcp.tool()
def runtime_policy_status() -> str:
    """Show shared local model mappings and execution-lane tier choices."""
    _maybe_live_reload()
    data = runtime_policy_data()
    output = runtime_policy.format_policy(data)
    if data.get("missing_models"):
        output += "\n  WARNING missing local model(s): %s" % ", ".join(
            sorted(set(data["missing_models"]))
        )
    if data.get("inventory_error"):
        output += "\n  WARNING model inventory unavailable: %s" % data["inventory_error"]
    return output


def _runtime_update_object(value, label):
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be a JSON object: %s" % (label, exc))
    if not isinstance(payload, dict):
        raise ValueError("%s must be a JSON object" % label)
    return payload


@mcp.tool()
def runtime_policy_update(
    local_models_json: str = "",
    routing_json: str = "",
    reset: bool = False,
) -> str:
    """Guarded-edit shared local mappings; cloud configuration is never accepted."""
    _maybe_live_reload()
    try:
        local_models = _runtime_update_object(local_models_json, "local_models_json")
        routing = _runtime_update_object(routing_json, "routing_json")
        if local_models:
            installed = _runtime_installed_models()
            missing = [
                str(model) for model in local_models.values()
                if not _runtime_model_is_installed(model, installed)
            ]
            if missing:
                raise ValueError(
                    "local model(s) are not installed: %s"
                    % ", ".join(sorted(set(missing)))
                )
        runtime_policy.update(
            local_models=local_models,
            routing=routing,
            reset=bool(reset),
            source="runtime_policy_update",
        )
        _refresh_runtime_policy(create=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return "ERROR: %s" % exc
    return runtime_policy_status()


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
    """Run lightweight health checks for the local Sonder Runtime installation."""
    _maybe_live_reload()
    lines = ["sonder diagnostics"]
    lines.append("  live reload: %s" % ("on" if live_reload.enabled() else "off"))
    mcp_state = mcp_runtime_data()
    lines.append(
        "  mcp runtime: %s (%s tools, %s atomic refreshes, list-changed=%s)"
        % (
            mcp_state.get("status", "unknown"),
            mcp_state.get("registered_tools", 0),
            mcp_state.get("refresh_count", 0),
            "on" if mcp_state.get("protocol_list_changed") else "off",
        )
    )
    if mcp_state.get("last_error"):
        lines.append("  mcp refresh ERROR: %s" % mcp_state["last_error"])
    lines.append(
        "  execution routing: host-gated foreground/autopilot/fleet with local ambiguity review"
    )
    policy = _refresh_runtime_policy(create=True)
    lines.append(
        "  runtime policy: revision=%s %s (%s)"
        % (
            policy.get("revision", 0),
            "ERROR %s" % policy["error"] if policy.get("error") else "ok",
            policy.get("path", runtime_policy.policy_path()),
        )
    )
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
            n_preferences = conn.execute(
                "SELECT COUNT(*) FROM preferences WHERE enabled=1"
            ).fetchone()[0]
            n_interactions = memory_store.count_interactions(conn)
        finally:
            conn.close()
        lines.append("  memory db: ok (%s, %d lessons, %d preferences, %d interactions)" % (
            _DB_PATH, n_lessons, n_preferences, n_interactions))
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
        health = learning_health_data()
        quality = health["quality"]
        lines.append(
            "  learning health: %s (%s%% outcome coverage, %s%% positive, yield=%s)"
            % (
                health["status"],
                health["outcome_coverage_percent"],
                health["positive_percent"],
                health["distillation_yield"]
                if health["distillation_yield"] is not None
                else "n/a",
            )
        )
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
        auto = autopilot_store.snapshot(include_finished=False, limit=20)
        lines.append(
            "  autopilot: ok (%s active, %s resumable; %s)"
            % (
                auto.get("active_runs", 0),
                auto.get("resumable_runs", 0),
                auto.get("database", ""),
            )
        )
    except Exception as e:
        lines.append("  autopilot: ERROR %s" % e)
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
    """Report Sonder Runtime's local-model state and current VRAM residency.

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
    try:
        auto = autopilot_store.snapshot(include_finished=False, limit=20)
        lines.append(
            "autopilot: %s active, %s resumable"
            % (auto.get("active_runs", 0), auto.get("resumable_runs", 0))
        )
    except Exception as exc:
        lines.append("autopilot: ERROR %s" % exc)
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


mcp.finish_module_refresh(__name__, __file__, globals())


if __name__ == "__main__" and not globals().get("_MCP_HOT_RELOAD_EXEC"):
    mcp.run()
